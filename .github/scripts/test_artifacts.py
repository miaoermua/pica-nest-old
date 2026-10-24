#!/usr/bin/env python3
"""Tests for artifact/index verification and the release-file list."""
import functools
import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def make_artifact(path, manifest, payload):
    with tarfile.open(path, "w:gz") as archive:
        data = manifest.encode()
        info = tarfile.TarInfo("manifest")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        for name, content in payload.items():
            data = content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


BASE = "https://example.invalid/releases/download/latest"


class ArtifactVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Artifacts live in the build staging directory, never in a Git-tracked
        # path: `r/packages/` is exactly what this change removes.
        self.packages = self.root / "build/opkg"
        self.packages.mkdir(parents=True)
        self.index = self.root / "r/repo.json"
        self.index.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_index(self, filename, pkgmgr="pica", platform="all", arch="x86_64", url=True):
        artifact = self.packages / filename
        entry = {
            "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1", "pkgmgr": pkgmgr,
            "platform": platform, "arch": arch, "filename": filename,
            "sha256": sha256(artifact), "csize": artifact.stat().st_size,
        }
        if url:
            entry["download_url"] = f"{BASE}/{filename}"
        self.index.write_text(json.dumps({"schema": 2, "packages": [entry]}), encoding="utf-8")
        return artifact

    def run_verify(self, *extra, download_base=BASE):
        command = [
            sys.executable, str(SCRIPTS / "verify_artifacts.py"),
            "--index", str(self.index), "--artifacts-dir", str(self.packages),
        ]
        if download_base:
            command += ["--download-base", download_base]
        return subprocess.run(
            command + list(extra), cwd=self.root, capture_output=True, text=True,
        )

    def test_a_script_installed_native_package_passes(self):
        """A native package may install itself through scripts instead of a container.

        cattools does this: `targets = opkg, pica`, its runtime dependencies
        handed to opkg as `base@opkg = bash`, and its payload delivered by
        `scripts/install`. There is deliberately no `.ipk` for it to carry, and
        demanding one would report an artifact the packer built as empty.
        """
        filename = "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = opkg\n",
            {"scripts/install": b"#!/bin/sh\n", "root/usr/bin/demo": b"binary"},
        )
        self.write_index(filename, pkgmgr="opkg")
        result = self.run_verify("--strict-all")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_artifact_with_nothing_installable_fails(self):
        """A manifest alone installs nothing, whatever the backend."""
        filename = "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = opkg\n",
            {},
        )
        self.write_index(filename, pkgmgr="opkg")
        result = self.run_verify("--strict-all")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no payload", result.stderr)

    def test_a_matching_artifact_passes(self):
        filename = "demo-1.0.0-1-x86_64-pica.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = pica\n",
            {"root/usr/bin/demo": b"binary"},
        )
        self.write_index(filename)
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_hash_mismatch_fails(self):
        filename = "demo-1.0.0-1-x86_64-pica.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = pica\n",
            {"root/usr/bin/demo": b"binary"},
        )
        self.write_index(filename)
        index = json.loads(self.index.read_text())
        index["packages"][0]["sha256"] = "0" * 64
        self.index.write_text(json.dumps(index))
        result = self.run_verify("--strict-all")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sha256", result.stderr)

    def test_a_required_artifact_must_have_an_index_entry(self):
        filename = "demo-1.0.0-1-x86_64-pica.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = pica\n",
            {"root/usr/bin/demo": b"binary"},
        )
        make_artifact(
            self.packages / "other.pkg.tar.gz",
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = pica\n",
            {"root/usr/bin/other": b"binary"},
        )
        self.write_index(filename)
        required = self.root / "required.txt"
        required.write_text(f"build/opkg/{filename}\nbuild/opkg/other.pkg.tar.gz\n", encoding="utf-8")
        result = self.run_verify("--require-file", str(required))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no index entry", result.stderr)

    def test_a_null_download_url_fails_a_required_entry(self):
        """A null URL sends clients to <repo>/packages/<filename>, the path that
        put 1.2 GB of archives into Git. It must not reach a commit."""
        filename = "demo-1.0.0-1-x86_64-pica.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = pica\n",
            {"root/usr/bin/demo": b"binary"},
        )
        self.write_index(filename, url=False)
        required = self.root / "required.txt"
        required.write_text(f"build/opkg/{filename}\n", encoding="utf-8")
        result = self.run_verify("--require-file", str(required))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no download_url", result.stderr)

    def test_a_download_url_outside_the_base_fails(self):
        filename = "demo-1.0.0-1-x86_64-pica.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = pica\n",
            {"root/usr/bin/demo": b"binary"},
        )
        self.write_index(filename)
        index = json.loads(self.index.read_text())
        index["packages"][0]["download_url"] = f"{BASE}/wrong-name.pkg.tar.gz"
        self.index.write_text(json.dumps(index))
        result = self.run_verify("--strict-all")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not", result.stderr)


class ReleaseListTest(unittest.TestCase):
    def test_new_artifacts_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            (root / "r/repo.json").write_text(json.dumps({"schema": 2, "packages": []}))
            output = root / "build/opkg"
            output.mkdir(parents=True)
            filename = "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
            make_artifact(
                output / filename,
                "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = opkg\n",
                {"pkgs/demo.ipk": b"ipk"},
            )
            (output / "repo.json").write_text(json.dumps({
                "schema": 2,
                "packages": [{
                    "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1",
                    "pkgmgr": "opkg", "platform": "all", "arch": "x86_64",
                    "filename": filename, "sha256": sha256(output / filename),
                }],
            }))
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "gen_repo_index.py"), str(output / "repo.json"),
                 "--repo", str(root / "r/repo.json"), "--release-list", str(root / "r/release-files.txt")],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = (root / "r/release-files.txt").read_text().splitlines()
            # The list names the build-staging artifact so the publish job can
            # upload it; it no longer names a Git-tracked r/packages/ copy.
            self.assertEqual(lines, [f"build/opkg/{filename}"])

    def test_a_rebuild_under_an_unchanged_version_is_reported(self):
        """Same filename, new bytes.

        The rolling release asset is overwritten before the index that describes
        it is committed, so the run has to say so: a silent replacement is how a
        published index ends up holding a hash no published file has.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            (root / "r/repo.json").write_text(json.dumps({"schema": 2, "packages": []}))
            output = root / "build/opkg"
            output.mkdir(parents=True)
            filename = "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
            manifest = (
                "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\n"
                "platform = all\narch = x86_64\npkgmgr = opkg\n"
            )

            def build_and_index(payload):
                make_artifact(output / filename, manifest, {"pkgs/demo.ipk": payload})
                (output / "repo.json").write_text(json.dumps({
                    "schema": 2,
                    "packages": [{
                        "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1",
                        "pkgmgr": "opkg", "platform": "all", "arch": "x86_64",
                        "filename": filename, "sha256": sha256(output / filename),
                    }],
                }))
                return subprocess.run(
                    [sys.executable, str(SCRIPTS / "gen_repo_index.py"),
                     str(output / "repo.json"), "--repo", str(root / "r/repo.json")],
                    cwd=root, capture_output=True, text=True,
                )

            first = build_and_index(b"first")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertNotIn("rebuilt under an unchanged version", first.stdout)

            second = build_and_index(b"second")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("rebuilt at the same version", second.stdout)
            self.assertIn("rebuilt under an unchanged version", second.stdout)

    def test_download_url_is_stamped_for_every_entry(self):
        """Historical entries need a URL too: their asset sits in the same
        rolling release, and leaving them null re-creates the /packages/ pull."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            stale = {
                "pkgname": "old", "pkgver": "0.9", "pkgrel": "1", "pkgmgr": "opkg",
                "platform": "all", "arch": "x86_64",
                "filename": "old-0.9-1-x86_64-opkg.pkg.tar.gz", "sha256": "b" * 64,
                "download_url": None,
            }
            (root / "r/repo.json").write_text(json.dumps({"schema": 2, "packages": [stale]}))
            output = root / "build/opkg"
            output.mkdir(parents=True)
            filename = "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
            make_artifact(
                output / filename,
                "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = opkg\n",
                {"pkgs/demo.ipk": b"ipk"},
            )
            (output / "repo.json").write_text(json.dumps({
                "schema": 2,
                "packages": [{
                    "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1",
                    "pkgmgr": "opkg", "platform": "all", "arch": "x86_64",
                    "filename": filename, "sha256": sha256(output / filename),
                }],
            }))
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "gen_repo_index.py"), str(output / "repo.json"),
                 "--repo", str(root / "r/repo.json"), "--download-base", BASE],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            merged = json.loads((root / "r/repo.json").read_text())["packages"]
            self.assertEqual(len(merged), 2)
            for pkg in merged:
                self.assertEqual(pkg["download_url"], f"{BASE}/{pkg['filename']}")

    def test_artifacts_may_be_staged_apart_from_the_index_metadata(self):
        """The publish job restores dist/build_output/<backend>/repo.json and
        dist/packages/* separately, so lookup cannot rely on co-location."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            (root / "r/repo.json").write_text(json.dumps({"schema": 2, "packages": []}))
            metadata = root / "dist/build_output/opkg"
            metadata.mkdir(parents=True)
            staged = root / "dist/packages"
            staged.mkdir(parents=True)
            filename = "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
            make_artifact(
                staged / filename,
                "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = opkg\n",
                {"pkgs/demo.ipk": b"ipk"},
            )
            (metadata / "repo.json").write_text(json.dumps({
                "schema": 2,
                "packages": [{
                    "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1",
                    "pkgmgr": "opkg", "platform": "all", "arch": "x86_64",
                    "filename": filename, "sha256": sha256(staged / filename),
                }],
            }))
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "gen_repo_index.py"), str(metadata / "repo.json"),
                 "--repo", str(root / "r/repo.json"), "--download-base", BASE,
                 "--artifacts-dir", str(staged)],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            merged = json.loads((root / "r/repo.json").read_text())["packages"]
            self.assertEqual(merged[0]["download_url"], f"{BASE}/{filename}")

    def test_a_missing_artifact_blocks_the_index_update(self):
        """Never publish an entry whose asset was not produced."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            (root / "r/repo.json").write_text(json.dumps({"schema": 2, "packages": []}))
            output = root / "build/opkg"
            output.mkdir(parents=True)
            (output / "repo.json").write_text(json.dumps({
                "schema": 2,
                "packages": [{
                    "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1",
                    "pkgmgr": "opkg", "platform": "all", "arch": "x86_64",
                    "filename": "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz", "sha256": "a" * 64,
                }],
            }))
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "gen_repo_index.py"), str(output / "repo.json"),
                 "--repo", str(root / "r/repo.json"), "--download-base", BASE],
                cwd=root, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("artifact is missing", result.stderr)


class MultiArchIndexTest(unittest.TestCase):
    """One job building several architectures must produce one entry each.

    Keying the merge on `(pkgmgr, pkgname)` alone collapses them onto a single
    entry: four artifacts reach `r/packages/` with nothing in the index naming
    them, and `verify_artifacts` reports each as "built artifact has no index
    entry". Which entry survives is then decided by matrix order rather than by
    anything the repository chose.
    """

    ARCHES = [
        ("amd64", "x86_64"),
        ("arm64", "aarch64_generic"),
        ("arm64", "aarch64_cortex-a53"),
        ("arm64", "aarch64_cortex-a72"),
        ("mipsel", "mipsel_24kc"),
    ]

    def test_every_architecture_keeps_its_own_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            (root / "r/repo.json").write_text(json.dumps({"schema": 2, "packages": []}))
            output = root / "build/opkg"
            output.mkdir(parents=True)

            entries = []
            for platform, arch in self.ARCHES:
                filename = f"demo-1.0.0-1-{platform}-{arch}-opkg.pkg.tar.gz"
                make_artifact(
                    output / filename,
                    f"pkgname = demo\npkgver = 1.0.0\npkgrel = 1\n"
                    f"platform = {platform}\narch = {arch}\npkgmgr = opkg\n",
                    {"pkgs/demo.ipk": b"ipk"},
                )
                entries.append({
                    "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1",
                    "pkgmgr": "opkg", "platform": platform, "arch": arch,
                    "filename": filename, "sha256": sha256(output / filename),
                })
            (output / "repo.json").write_text(json.dumps({"schema": 2, "packages": entries}))

            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "gen_repo_index.py"),
                 str(output / "repo.json"),
                 "--repo", str(root / "r/repo.json"),
                 "--release-list", str(root / "r/release-files.txt")],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            merged = json.loads((root / "r/repo.json").read_text())["packages"]
            self.assertEqual(len(merged), len(self.ARCHES), "one entry per architecture")
            self.assertEqual(
                {(pkg["platform"], pkg["arch"]) for pkg in merged},
                set(self.ARCHES),
            )
            released = (root / "r/release-files.txt").read_text().splitlines()
            self.assertEqual(len(released), len(self.ARCHES))

    def test_an_older_entry_for_one_architecture_is_replaced_not_kept(self):
        """A rebuild of one arch updates that entry and leaves the others alone."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            stale = {
                "pkgname": "demo", "pkgver": "0.9", "pkgrel": "1", "pkgmgr": "opkg",
                "platform": "amd64", "arch": "x86_64",
                "filename": "demo-0.9-1-amd64-x86_64-opkg.pkg.tar.gz", "sha256": "b" * 64,
            }
            keep = {
                "pkgname": "demo", "pkgver": "0.9", "pkgrel": "1", "pkgmgr": "opkg",
                "platform": "mipsel", "arch": "mipsel_24kc",
                "filename": "demo-0.9-1-mipsel-mipsel_24kc-opkg.pkg.tar.gz", "sha256": "c" * 64,
            }
            (root / "r/repo.json").write_text(
                json.dumps({"schema": 2, "packages": [stale, keep]})
            )

            output = root / "build/opkg"
            output.mkdir(parents=True)
            filename = "demo-1.0.0-1-amd64-x86_64-opkg.pkg.tar.gz"
            make_artifact(
                output / filename,
                "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\n"
                "platform = amd64\narch = x86_64\npkgmgr = opkg\n",
                {"pkgs/demo.ipk": b"ipk"},
            )
            (output / "repo.json").write_text(json.dumps({
                "schema": 2,
                "packages": [{
                    "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1",
                    "pkgmgr": "opkg", "platform": "amd64", "arch": "x86_64",
                    "filename": filename, "sha256": sha256(output / filename),
                }],
            }))

            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "gen_repo_index.py"),
                 str(output / "repo.json"), "--repo", str(root / "r/repo.json")],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            merged = json.loads((root / "r/repo.json").read_text())["packages"]
            by_arch = {pkg["arch"]: pkg["pkgver"] for pkg in merged}
            self.assertEqual(by_arch["x86_64"], "1.0.0", "the rebuilt arch is updated")
            self.assertEqual(by_arch["mipsel_24kc"], "0.9", "an untouched arch is left alone")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """The tests assert on the script's output, not on the server's request log."""

    def log_message(self, *args):
        pass


class PublishedAssetVerificationTest(unittest.TestCase):
    """`--verify-published` checks the bytes a client would receive.

    A local artifact matching the index says nothing about what was uploaded:
    the publish job keeps its own copy, and a filename overwritten with a
    rebuilt archive still answers 200 under the old hash. These tests serve the
    "published" bytes over a local HTTP server, so the check runs against a real
    response rather than a stub.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.packages = self.root / "build/opkg"
        self.packages.mkdir(parents=True)
        self.published = self.root / "published"
        self.published.mkdir()
        self.index = self.root / "r/repo.json"
        self.index.parent.mkdir(parents=True)

        handler = functools.partial(QuietHandler, directory=str(self.published))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def entry(self, path, payload=None):
        """An index entry for an artifact built into the staging directory."""
        manifest = (
            "pkgname=demo\npkgver=1.0.0\npkgrel=1\n"
            "platform=all\narch=x86_64\npkgmgr=opkg\n"
        )
        make_artifact(path, manifest, payload or {"root/etc/demo": b"x"})
        return {
            "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1", "pkgmgr": "opkg",
            "platform": "all", "arch": "x86_64", "filename": path.name,
            "sha256": sha256(path), "csize": path.stat().st_size,
            "download_url": f"{self.base}/{path.name}",
        }

    def write_index(self, entries):
        self.index.write_text(json.dumps({"schema": 2, "packages": entries}), encoding="utf-8")

    def run_verify(self, *extra, required=()):
        command = [
            sys.executable, str(SCRIPTS / "verify_artifacts.py"),
            "--index", str(self.index), "--artifacts-dir", str(self.packages),
            "--download-base", self.base,
        ]
        if required:
            listing = self.root / "required.txt"
            listing.write_text("\n".join(str(path) for path in required) + "\n", encoding="utf-8")
            command += ["--require-file", str(listing)]
        return subprocess.run(command + list(extra), cwd=self.root, capture_output=True, text=True)

    def test_the_published_bytes_are_the_ones_the_index_describes(self):
        artifact = self.packages / "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
        self.write_index([self.entry(artifact)])
        shutil.copy(artifact, self.published / artifact.name)

        result = self.run_verify("--verify-published", required=[artifact])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verified 1 index entries (1 required).", result.stdout)

    def test_a_republished_filename_under_new_bytes_fails(self):
        artifact = self.packages / "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
        self.write_index([self.entry(artifact)])
        # Same name, different archive: exactly what a second build uploaded
        # over the first one looks like to a HEAD.
        make_artifact(
            self.published / artifact.name,
            "pkgname=demo\npkgver=1.0.0\npkgrel=1\nplatform=all\narch=x86_64\npkgmgr=opkg\n",
            {"root/etc/demo": b"rebuilt"},
        )

        result = self.run_verify("--verify-published", required=[artifact])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("published sha256", result.stderr)
        self.assertIn("does not match index csize", result.stderr)

    def test_an_error_page_is_not_mistaken_for_a_package(self):
        artifact = self.packages / "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
        self.write_index([self.entry(artifact)])
        (self.published / artifact.name).write_text("<html>rate limited</html>", encoding="utf-8")

        result = self.run_verify("--verify-published", required=[artifact])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a gzip archive", result.stderr)

    def test_a_missing_published_asset_fails(self):
        artifact = self.packages / "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
        self.write_index([self.entry(artifact)])

        result = self.run_verify("--verify-published", required=[artifact])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP 404", result.stderr)

    def test_a_historical_entry_is_probed_not_downloaded(self):
        """One bad historical asset must not block an unrelated publish."""
        artifact = self.packages / "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
        built = self.entry(artifact)
        historical = dict(built, filename="other-0.9-1-x86_64-opkg.pkg.tar.gz")
        historical["download_url"] = f"{self.base}/{historical['filename']}"
        self.write_index([built, historical])
        shutil.copy(artifact, self.published / artifact.name)
        # If this were downloaded, the gzip check would reject it.
        (self.published / historical["filename"]).write_text("<html>gone</html>", encoding="utf-8")

        result = self.run_verify("--verify-published", "--probe", required=[artifact])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("published size", result.stderr, "the HEAD still reports the size drift")
        self.assertNotIn("not a gzip archive", result.stderr, "it is never read as an archive")


if __name__ == "__main__":
    unittest.main()
