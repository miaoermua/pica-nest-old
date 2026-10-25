#!/usr/bin/env python3
"""Tests for asset planning, preflight staging and the two-phase pipeline."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_assets import (  # noqa: E402
    dedupe_requests,
    download_to,
    extract_all_from_zip,
    fetch_plan_file,
    http_template_urls,
    http_template_urls_for_arch,
    plan_all,
    plan_github_assets,
    plan_http_assets,
    plan_tarball_assets,
    verify_plan,
)
from container_metadata import declares_package, version_matches  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_assets.py")


class VersionMatchTest(unittest.TestCase):
    def test_separators_and_revisions_do_not_change_a_version(self):
        self.assertTrue(version_matches("4.1.10", "4.1.10-r1"))
        self.assertTrue(version_matches("1.8.9", "1.8-9"))
        self.assertTrue(version_matches("26.156.15499~38397ed", "26.156.15499.38397ed"))
        self.assertTrue(version_matches("9.1", "nano_9.1-r1_x86_64.ipk"))

    def test_a_different_base_version_is_not_a_match(self):
        self.assertFalse(version_matches("3.9.1", "3.17.2-r1"))
        self.assertFalse(version_matches("4.1.5", "4.1.10-r1"))
        self.assertFalse(version_matches("9.20.18", "9.20.26-r1"))


def make_ipk(path, package, version, arch="all"):
    import io
    import tarfile

    control = f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\n".encode()
    control_tar = io.BytesIO()
    with tarfile.open(fileobj=control_tar, mode="w:gz") as archive:
        info = tarfile.TarInfo("control")
        info.size = len(control)
        archive.addfile(info, io.BytesIO(control))
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("control.tar.gz")
        info.size = len(control_tar.getvalue())
        archive.addfile(info, io.BytesIO(control_tar.getvalue()))


class VerifyPlanVersionTest(unittest.TestCase):
    def plan(self, tmp, pkgver, packages):
        stage = os.path.join(tmp, "stage")
        os.makedirs(stage, exist_ok=True)
        with open(os.path.join(stage, "manifest"), "w", encoding="utf-8") as handle:
            handle.write(
                f"pkgname = demo\npkgver = {pkgver}\npkgrel = 1\n"
                "app = demo\napp = luci-app-demo\n"
            )
        requests = []
        for index, (package, version) in enumerate(packages):
            path = os.path.join(stage, f"{package}.ipk")
            make_ipk(path, package, version)
            requests.append(
                {
                    "ok": True, "kind": "container", "pkg": "demo", "mgr": "opkg",
                    "platform": "all", "arch": "all", "asset": os.path.basename(path),
                    "path": path,
                }
            )
        return {"pkg": "demo", "mgr": "opkg", "stage": stage, "requests": requests}

    def test_matching_container_version_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(
                tmp, "1.0.0",
                [("demo", "1.0.0-r1"), ("luci-app-demo", "26.100.1~abc")],
            )
            errors, _, verified = verify_plan(plan)
            self.assertEqual(errors, [])
            self.assertEqual(verified, 2)

    def test_manifest_version_missing_from_all_containers_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(tmp, "1.0.0", [("demo", "2.0.0-r1")])
            errors, _, _ = verify_plan(plan)
            self.assertTrue(any("no downloaded container carries it" in error["reason"] for error in errors))

    def test_a_bundle_matches_on_its_core_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(
                tmp, "1.0.0",
                [("demo", "1.0.0-r1"), ("luci-app-demo", "26.100.1~abc")],
            )
            errors, _, _ = verify_plan(plan)
            self.assertEqual(errors, [])


class ZipExtractionTest(unittest.TestCase):
    def test_every_matching_ipk_is_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "EasyTier.zip")
            dest = os.path.join(tmp, "dest")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("easytier_2.5.0_x86_64.ipk", b"core")
                archive.writestr("luci-app-easytier_2.5.0_all.ipk", b"luci")
                archive.writestr("luci-i18n-easytier-zh-cn_git-1_all.ipk", b"i18n")
                archive.writestr("easytier-noweb_2.8.0_x86_64.ipk", b"noweb")
                archive.writestr("README.txt", b"not a package")
            regex = r"^(?:easytier(?!-noweb)|luci-app-easytier|luci-i18n-easytier).*\.ipk$"
            files = extract_all_from_zip(zip_path, regex, dest)
            self.assertEqual(
                [os.path.basename(path) for path in files],
                [
                    "easytier_2.5.0_x86_64.ipk",
                    "luci-app-easytier_2.5.0_all.ipk",
                    "luci-i18n-easytier-zh-cn_git-1_all.ipk",
                ],
            )
            self.assertFalse(os.path.exists(os.path.join(dest, "easytier-noweb_2.8.0_x86_64.ipk")))


class ServerFixture:
    def __init__(self, directory, failures=0):
        self.directory = directory
        self.failures = failures
        self.gets = 0
        self.heads = 0

        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _path(self):
                return self.path.split("?", 1)[0].lstrip("/")

            def do_HEAD(self):
                fixture.heads += 1
                path = os.path.join(fixture.directory, self._path())
                if not os.path.isfile(path):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(os.path.getsize(path)))
                self.end_headers()

            def do_GET(self):
                fixture.gets += 1
                if fixture.gets <= fixture.failures:
                    self.send_error(500)
                    return
                path = os.path.join(fixture.directory, self._path())
                if not os.path.isfile(path):
                    self.send_error(404)
                    return
                data = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class PlanFetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        self.asset_dir = self.root / "assets"
        self.asset_dir.mkdir()
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.close()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def make_repo(self, url, matrix=None, filename="demo_1.0.0-r1_x86_64.ipk", ipk_arch="x86_64"):
        (self.root / "src/opkg/demo").mkdir(parents=True)
        (self.root / "ci/opkg/demo").mkdir(parents=True)
        (self.root / "src/opkg/demo/manifest").write_text(
            "pkgname = demo\nappname = demo\npkgver = 1.0.0\npkgrel = 1\n"
            "os = openwrt\nplatform = all\narch = all\ntargets = opkg\napp = demo\n",
            encoding="utf-8",
        )
        job = {
            "schema": "pica-ci-job-v1", "mgr": "opkg", "id": "demo", "enabled": True,
            "manifest": "src/opkg/demo/manifest",
            "source": {"provider": "http_template", "url_templates": [url]},
            "versioning": {"pkgver": {"from": "manifest", "fallback": "1.0.0"}},
            "build": {
                "matrix": matrix or [{"platform": "amd64", "arch": "x86_64"}],
                "include_dirs": ["scripts", "root", "pkgs"],
            },
            "publish": {"registry_db": "r/opkg/demo/db.json"},
        }
        (self.root / "ci/opkg/demo/build.json").write_text(json.dumps(job), encoding="utf-8")
        make_ipk(str(self.asset_dir / filename), "demo", "1.0.0-r1", ipk_arch)

    def start_server(self, failures=0):
        self.server = ServerFixture(str(self.asset_dir), failures=failures)
        return self.server

    def test_plan_does_not_download_and_fetch_consumes_the_plan(self):
        server = self.start_server()
        self.make_repo(f"{server.url}/demo_1.0.0-r1_x86_64.ipk")
        plan_path = "plan.json"
        status = plan_all(["src/opkg/demo"], "stage", plan_path)
        self.assertEqual(status, 0)
        self.assertEqual(server.gets, 0)
        self.assertFalse(Path("stage/opkg/demo/pkgs").exists())

        status = fetch_plan_file(plan_path)
        self.assertEqual(status, 0)
        self.assertEqual(server.gets, 1)
        self.assertTrue(
            Path("stage/opkg/demo/pkgs/opkg/amd64/x86_64/demo_1.0.0-r1_x86_64.ipk").is_file()
        )

    def test_probe_blocks_a_missing_resource_before_get(self):
        server = self.start_server()
        self.make_repo(f"{server.url}/missing.ipk")
        status = plan_all(["src/opkg/demo"], "stage", "plan.json", probe=True)
        self.assertEqual(status, 1)
        self.assertEqual(server.gets, 0)
        self.assertGreaterEqual(server.heads, 1)
        self.assertFalse(Path("stage/opkg/demo/pkgs").exists())

    def test_fetch_deduplicates_the_same_url(self):
        server = self.start_server()
        matrix = [
            {"platform": "amd64", "arch": "x86_64"},
            {"platform": "arm64", "arch": "aarch64_generic"},
        ]
        self.make_repo(
            f"{server.url}/demo_1.0.0-r1_x86_64.ipk", matrix=matrix, ipk_arch="all"
        )
        self.assertEqual(plan_all(["src/opkg/demo"], "stage", "plan.json"), 0)
        self.assertEqual(fetch_plan_file("plan.json"), 0)
        self.assertEqual(server.gets, 1)
        self.assertTrue(
            Path("stage/opkg/demo/pkgs/opkg/arm64/aarch64_generic/demo_1.0.0-r1_x86_64.ipk").is_file()
        )

    def test_asset_cache_avoids_a_second_get(self):
        server = self.start_server()
        self.make_repo(f"{server.url}/demo_1.0.0-r1_x86_64.ipk")
        cache = str(self.root / "cache")
        self.assertEqual(plan_all(["src/opkg/demo"], "stage1", "plan1.json"), 0)
        self.assertEqual(fetch_plan_file("plan1.json", cache_dir=cache), 0)
        self.assertEqual(server.gets, 1)
        self.assertEqual(plan_all(["src/opkg/demo"], "stage2", "plan2.json"), 0)
        self.assertEqual(fetch_plan_file("plan2.json", cache_dir=cache), 0)
        self.assertEqual(server.gets, 1)

    def test_transient_500_is_retried(self):
        server = self.start_server(failures=2)
        (self.asset_dir / "demo_1.0.0-r1_x86_64.ipk").write_bytes(b"asset bytes")
        path = str(self.root / "asset.bin")
        with mock.patch("fetch_assets._sleep_before_retry"):
            ok, size, error = download_to(f"{server.url}/demo_1.0.0-r1_x86_64.ipk", path, attempts=3)
        self.assertTrue(ok, error)
        self.assertEqual(server.gets, 3)
        self.assertEqual(size, Path(path).stat().st_size)


class HttpTemplateUrlTest(unittest.TestCase):
    def test_common_urls_apply_to_every_arch(self):
        source = {"url_templates": ["https://example/{arch}/a", "https://example/{arch}/b"]}
        for arch in ("x86_64", "aarch64_cortex-a53"):
            self.assertEqual(http_template_urls_for_arch(source, arch), source["url_templates"])

    def test_an_arch_entry_replaces_the_common_list(self):
        source = {
            "url_templates": ["https://example/{arch}/old"],
            "arch_url_templates": {
                "aarch64_cortex-a53": ["https://example/{arch}/new"],
            },
        }
        self.assertEqual(
            http_template_urls_for_arch(source, "aarch64_cortex-a53"),
            ["https://example/{arch}/new"],
        )
        self.assertEqual(
            http_template_urls_for_arch(source, "x86_64"),
            ["https://example/{arch}/old"],
        )

    def test_the_singular_template_form_still_works(self):
        source = {"url_template": "https://example/{arch}/one"}
        self.assertEqual(http_template_urls(source), ["https://example/{arch}/one"])
        self.assertEqual(
            http_template_urls_for_arch(source, "x86_64"),
            ["https://example/{arch}/one"],
        )

    def test_an_arch_only_source_has_no_urls_for_other_arches(self):
        source = {"arch_url_templates": {"x86_64": ["https://example/x86"]}}
        self.assertEqual(http_template_urls_for_arch(source, "x86_64"), ["https://example/x86"])
        self.assertEqual(http_template_urls_for_arch(source, "aarch64_generic"), [])


def http_config(urls, matrix=None, overrides=None):
    source = {"provider": "http_template", "url_templates": urls}
    if overrides:
        source["arch_url_templates"] = overrides
    return {
        "mgr": "opkg",
        "source": source,
        "build": {"matrix": matrix or [{"platform": "all", "arch": "all"}]},
    }


class PlanTest(unittest.TestCase):
    def test_http_override_replaces_common_for_one_arch(self):
        matrix = [
            {"platform": "amd64", "arch": "x86_64"},
            {"platform": "arm64", "arch": "aarch64_cortex-a53"},
        ]
        config = http_config(
            ["https://example/{arch}/old.ipk"],
            matrix,
            {"aarch64_cortex-a53": ["https://example/{arch}/new.ipk"]},
        )
        requests, errors, _, _ = plan_http_assets(config, "/stage", "demo")
        self.assertEqual(errors, [])
        urls = {request["arch"]: request["url"] for request in requests}
        self.assertEqual(urls["aarch64_cortex-a53"], "https://example/aarch64_cortex-a53/new.ipk")
        self.assertEqual(urls["x86_64"], "https://example/x86_64/old.ipk")

    def test_http_plan_requires_a_url_for_every_matrix_arch(self):
        matrix = [
            {"platform": "amd64", "arch": "x86_64"},
            {"platform": "arm64", "arch": "aarch64_cortex-a53"},
        ]
        config = http_config([], matrix, {"x86_64": ["https://example/x86.ipk"]})
        requests, errors, _, _ = plan_http_assets(config, "/stage", "demo")
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("aarch64_cortex-a53", errors[0]["reason"])

    def test_http_plan_reports_two_resources_writing_one_path(self):
        config = http_config(["https://a.example/same.ipk", "https://b.example/same.ipk"])
        requests, errors, _, _ = plan_http_assets(config, "/stage", "demo")
        requests, dedupe_errors = dedupe_requests(requests, "demo", "opkg")
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(dedupe_errors), 1)
        self.assertIn("same path", dedupe_errors[0]["reason"])

    def test_github_plan_requires_each_matrix_arch_and_skips_extras(self):
        release = {
            "tag_name": "v1",
            "assets": [
                {"name": "demo-1.0-r1-x86_64.apk", "browser_download_url": "https://x/x86.apk", "size": 10},
                {"name": "demo-1.0-r1-aarch64_cortex-a76.apk", "browser_download_url": "https://x/a76.apk", "size": 10},
            ],
        }
        config = {
            "mgr": "apk",
            "source": {
                "release": {
                    "assets": [
                        {"name": "demo", "regex": r"^demo-[0-9].*-r[0-9]+-(.+?)\.apk$",
                         "capture_to": {"arch": 1}}
                    ]
                }
            },
            "build": {
                "matrix": [
                    {"platform": "amd64", "arch": "x86_64"},
                    {"platform": "arm64", "arch": "aarch64_cortex-a53"},
                ]
            },
        }
        requests, errors, _, skipped = plan_github_assets(release, config, "/stage", "demo")
        self.assertEqual([request["arch"] for request in requests], ["x86_64"])
        self.assertEqual(len(errors), 1)
        self.assertIn("aarch64_cortex-a53", errors[0]["reason"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("aarch64_cortex-a76", skipped[0]["asset"])

    def test_github_noarch_asset_lands_in_every_matrix_arch(self):
        """An `all` container is a part of every package, not a package of its own.

        Written once under `all/all` it is built as a second artifact: one
        holding the interface and no binary, beside one per architecture holding
        the binary and no interface. Both carry the same name, version and
        manifest, so an install takes one and is missing half the app.
        """
        release = {
            "tag_name": "v1",
            "assets": [{"name": "demo_1.0_all.ipk", "browser_download_url": "https://x/all.ipk", "size": 10}],
        }
        config = {
            "mgr": "opkg",
            "source": {"release": {"assets": [
                {"name": "demo", "regex": r"^demo_.*_all\.ipk$",
                 "target": {"platform": "all", "arch": "all"}}
            ]}},
            "build": {"matrix": [
                {"platform": "amd64", "arch": "x86_64"},
                {"platform": "arm64", "arch": "aarch64_generic"},
            ]},
        }
        requests, errors, _, _ = plan_github_assets(release, config, "/stage", "demo")
        self.assertEqual([request["arch"] for request in requests], ["x86_64", "aarch64_generic"])
        self.assertEqual([request["platform"] for request in requests], ["amd64", "arm64"])
        for request in requests:
            self.assertTrue(request["dest"].endswith("demo_1.0_all.ipk"))
            self.assertNotIn("all/all", request["dest"].replace("\\", "/"))
        self.assertEqual(errors, [])

    def test_github_noarch_asset_stays_single_for_an_all_matrix(self):
        """A job whose matrix is `all/all` has one package, so one destination."""
        release = {
            "tag_name": "v1",
            "assets": [{"name": "demo_1.0_all.ipk", "browser_download_url": "https://x/all.ipk", "size": 10}],
        }
        config = {
            "mgr": "opkg",
            "source": {"release": {"assets": [
                {"name": "demo", "regex": r"^demo_.*_all\.ipk$",
                 "target": {"platform": "all", "arch": "all"}}
            ]}},
            "build": {"matrix": [{"platform": "all", "arch": "all"}]},
        }
        requests, errors, _, _ = plan_github_assets(release, config, "/stage", "demo")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["arch"], "all")
        self.assertEqual(errors, [])

    def test_github_optional_missing_is_not_an_error(self):
        config = {
            "mgr": "opkg",
            "source": {"release": {"assets": [
                {"name": "extra", "regex": r"^extra_.*\.ipk$", "required": False}
            ]}},
            "build": {"matrix": [{"platform": "all", "arch": "all"}]},
        }
        requests, errors, optional, _ = plan_github_assets({"tag_name": "v1", "assets": []}, config, "/stage", "demo")
        self.assertEqual(requests, [])
        self.assertEqual(errors, [])
        self.assertEqual(len(optional), 1)

    def test_github_arch_map_maps_a_captured_arch_to_the_matrix_arch(self):
        release = {
            "tag_name": "v1",
            "assets": [{"name": "demo-1.0-r1-aarch64.ipk", "browser_download_url": "https://x/a.ipk", "size": 10}],
        }
        config = {
            "mgr": "opkg",
            "source": {
                "arch_map": {"aarch64": "aarch64_generic"},
                "release": {"assets": [
                    {"name": "demo", "regex": r"^demo-.*-r[0-9]+-(.+?)\.ipk$",
                     "capture_to": {"arch": 1}}
                ]},
            },
            "build": {"matrix": [{"platform": "arm64", "arch": "aarch64_generic"}]},
        }
        requests, errors, _, _ = plan_github_assets(release, config, "/stage", "demo")
        self.assertEqual(errors, [])
        self.assertEqual([request["arch"] for request in requests], ["aarch64_generic"])
        self.assertTrue(requests[0]["dest"].endswith("/arm64/aarch64_generic/demo-1.0-r1-aarch64.ipk"))


class DeclaredPackageTest(unittest.TestCase):
    def test_app_i18n_template_matches_a_language_package(self):
        declared = {
            "app": {"zerotier"},
            "base": set(),
            "kmod": set(),
            "app_i18n": {"luci-i18n-zerotier-{lang}"},
        }
        self.assertTrue(declares_package(declared, "luci-i18n-zerotier-zh-cn"))
        self.assertFalse(declares_package(declared, "luci-i18n-other-zh-cn"))

    def test_tarball_plan_rejects_multiple_matrix_arches(self):
        config = {
            "mgr": "pica",
            "source": {"provider": "tarball", "url_template": "https://x/{arch}.bin"},
            "build": {"matrix": [
                {"platform": "amd64", "arch": "x86_64"},
                {"platform": "arm64", "arch": "aarch64_generic"},
            ]},
        }
        requests, errors, _, _ = plan_tarball_assets(config, "/stage", "demo")
        self.assertEqual(requests, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot represent", errors[0]["reason"])


def package_files(root, name, urls):
    (root / "src" / "opkg" / name).mkdir(parents=True, exist_ok=True)
    (root / "ci" / "opkg" / name).mkdir(parents=True, exist_ok=True)
    (root / "src" / "opkg" / name / "manifest").write_text(
        f"pkgname = {name}\nappname = {name}\npkgver = 1.0.0\npkgrel = 1\n"
        "os = openwrt\ntargets = opkg\n",
        encoding="utf-8",
    )
    config = {
        "schema": "pica-ci-job-v1",
        "mgr": "opkg",
        "id": name,
        "manifest": f"src/opkg/{name}/manifest",
        "source": {"provider": "http_template", "url_templates": urls},
        "build": {
            "matrix": [{"platform": "all", "arch": "all"}],
            "include_dirs": ["scripts", "root", "pkgs"],
            "pack": {"tool": "pica-pack", "format": "pkg.tar.gz"},
        },
    }
    (root / "ci" / "opkg" / name / "build.json").write_text(json.dumps(config), encoding="utf-8")


class PreflightIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pica-preflight-test-")
        self.root = Path(self.temp.name)
        (self.root / "assets").mkdir()
        (self.root / "bin").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_preflight(self, packages, stage="stage"):
        return subprocess.run(
            [sys.executable, SCRIPT, "--preflight", "--stage-root", str(self.root / stage), *packages],
            cwd=self.root,
            capture_output=True,
            text=True,
        )

    def fake_pack(self, exit_code=0):
        path = self.root / "bin" / "pica-pack"
        path.write_text(
            "#!/bin/sh\n"
            'echo "$@" >> "$CALLS"\n'
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def pipeline(self, packages, stage="stage", pack_exit=0):
        """The workflow's order: preflight, then pack only if it succeeded."""
        preflight = self.run_preflight(packages, stage)
        calls = self.root / "calls.log"
        release = self.root / "release.log"
        if preflight.returncode != 0:
            return preflight, calls, release
        pack = self.fake_pack(pack_exit)
        env = dict(os.environ, CALLS=str(calls))
        for source in packages:
            relative = Path(source).relative_to(self.root)
            stage_dir = self.root / stage / relative.parts[1] / relative.parts[2]
            result = subprocess.run(
                [str(pack), "build", str(stage_dir), "--outdir", str(self.root / "out")],
                cwd=self.root,
                env=env,
            )
            if result.returncode != 0:
                return preflight, calls, release
        release.write_text("release\n", encoding="utf-8")
        return preflight, calls, release

    def test_missing_second_package_blocks_before_any_pack(self):
        (self.root / "assets" / "foo.ipk").write_bytes(b"fake foo")
        package_files(self.root, "foo", [f"file://{self.root}/assets/foo.ipk"])
        package_files(self.root, "bar", [f"file://{self.root}/assets/missing.ipk"])
        packages = [str(self.root / "src/opkg/foo"), str(self.root / "src/opkg/bar")]
        preflight, calls, release = self.pipeline(packages)
        self.assertNotEqual(preflight.returncode, 0)
        self.assertFalse(calls.exists(), "pica-pack must not run when preflight fails")
        self.assertFalse(release.exists(), "release must not run when preflight fails")
        self.assertIn("BUILD PHASE NOT STARTED", preflight.stdout)

    def test_multiple_package_errors_are_aggregated(self):
        package_files(self.root, "foo", [f"file://{self.root}/assets/foo-missing.ipk"])
        package_files(self.root, "bar", [f"file://{self.root}/assets/bar-missing.ipk"])
        result = self.run_preflight([str(self.root / "src/opkg/foo"), str(self.root / "src/opkg/bar")])
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foo/opkg", combined)
        self.assertIn("bar/opkg", combined)
        self.assertIn("Validation failures: 2", combined)

    def test_one_of_two_required_assets_missing_blocks(self):
        (self.root / "assets" / "present.ipk").write_bytes(b"present")
        package_files(
            self.root,
            "foo",
            [f"file://{self.root}/assets/present.ipk", f"file://{self.root}/assets/missing.ipk"],
        )
        result = self.run_preflight([str(self.root / "src/opkg/foo")])
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing.ipk", combined)
        self.assertIn("BUILD PHASE NOT STARTED", combined)

    def test_stale_stage_file_cannot_satisfy_a_missing_asset(self):
        package_files(self.root, "foo", [f"file://{self.root}/assets/missing.ipk"])
        stale = self.root / "stage/opkg/foo/pkgs/opkg/all/all/foo.ipk"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")
        result = self.run_preflight([str(self.root / "src/opkg/foo")])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(stale.exists(), "the preflight must clear the stage before checking")

    def test_success_stages_assets_once_and_pack_consumes_them(self):
        (self.root / "assets" / "foo.ipk").write_bytes(b"foo")
        (self.root / "assets" / "bar.ipk").write_bytes(b"bar")
        package_files(self.root, "foo", [f"file://{self.root}/assets/foo.ipk"])
        package_files(self.root, "bar", [f"file://{self.root}/assets/bar.ipk"])
        packages = [str(self.root / "src/opkg/foo"), str(self.root / "src/opkg/bar")]
        preflight, calls, release = self.pipeline(packages)
        self.assertEqual(preflight.returncode, 0, preflight.stdout + preflight.stderr)
        self.assertEqual(preflight.stdout.count("Downloading "), 2, "one download per asset")
        self.assertTrue((self.root / "stage/opkg/foo/pkgs/opkg/all/all/foo.ipk").is_file())
        self.assertTrue((self.root / "stage/opkg/bar/pkgs/opkg/all/all/bar.ipk").is_file())
        self.assertEqual(len(calls.read_text().splitlines()), 2)
        self.assertTrue(release.exists())

    def test_pack_failure_prevents_release(self):
        (self.root / "assets" / "foo.ipk").write_bytes(b"foo")
        package_files(self.root, "foo", [f"file://{self.root}/assets/foo.ipk"])
        preflight, calls, release = self.pipeline(
            [str(self.root / "src/opkg/foo")], pack_exit=1
        )
        self.assertEqual(preflight.returncode, 0)
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        self.assertFalse(release.exists(), "a failed pack must not reach the release phase")

    def test_empty_selection_is_a_no_op(self):
        result = self.run_preflight([])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Packages checked: 0", result.stdout)


class UpstreamErrorClassificationTest(unittest.TestCase):
    """An unreachable upstream must not be reported as a missing resource.

    A rate limit says nothing about whether a release exists. Reporting it as a
    missing resource sends an operator looking for a deleted package that is
    still there, and on a scheduled run it paints the repository red for a
    condition that resolves itself.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pica-upstream-test-")
        self.root = Path(self.temp.name)
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(self.temp.cleanup)

    def github_package(self, name="ghost", arch="x86_64"):
        (self.root / "src" / "opkg" / name).mkdir(parents=True, exist_ok=True)
        (self.root / "ci" / "opkg" / name).mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "opkg" / name / "manifest").write_text(
            f"pkgname = {name}\nappname = {name}\npkgver = 1.0.0\npkgrel = 1\n"
            "os = openwrt\ntargets = opkg\n",
            encoding="utf-8",
        )
        config = {
            "schema": "pica-ci-job-v1",
            "mgr": "opkg",
            "id": name,
            "manifest": f"src/opkg/{name}/manifest",
            "source": {
                "provider": "github",
                "repo": "owner/repo",
                "release": {
                    "tag": {"mode": "regex", "regex": "^v[0-9.]+$"},
                    "assets": [
                        {
                            "name": "app",
                            "role": "app",
                            "regex": "^app_[0-9.]+_(.*)\\.ipk$",
                            "required": True,
                            "capture_to": {"arch": 1},
                        }
                    ],
                },
            },
            "build": {
                "matrix": [{"platform": "amd64", "arch": arch}],
                "include_dirs": ["scripts", "root", "pkgs"],
                "pack": {"tool": "pica-pack", "format": "pkg.tar.gz"},
            },
        }
        (self.root / "ci" / "opkg" / name / "build.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        return str(self.root / "src" / "opkg" / name)

    def run_plan(self, packages, **kwargs):
        out = self.root / "plan.json"
        code = plan_all(packages, str(self.root / "stage"), str(out), **kwargs)
        plan = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
        return code, plan

    def test_offline_plan_never_asks_for_a_release_list(self):
        package = self.github_package()
        with mock.patch("fetch_assets.fetch_json") as fetch:
            code, plan = self.run_plan([package], offline=True)
        fetch.assert_not_called()
        self.assertEqual(code, 0, "an offline plan must not fail on a github job")
        reasons = [e["reason"] for e in plan["errors"]]
        self.assertTrue(
            any("offline" in reason for reason in reasons),
            "an offline run must say the listing was not consulted",
        )

    def test_a_rate_limit_is_unreachable_rather_than_missing(self):
        package = self.github_package()
        limit = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo/releases", 403, "rate limit exceeded", {}, None
        )
        with mock.patch("fetch_assets.fetch_json", side_effect=limit):
            code, plan = self.run_plan([package], allow_unreachable=True)
        statuses = {e["status"] for e in plan["errors"]}
        self.assertIn("unreachable", statuses)
        self.assertNotIn("missing", statuses, "a 403 is not evidence the release is gone")
        self.assertEqual(
            [e for e in plan["errors"] if e["blocks"]], [],
            "unreachable must not block when the caller allows it",
        )
        self.assertEqual(code, 0)

    def test_a_rate_limit_still_blocks_a_build(self):
        package = self.github_package()
        limit = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo/releases", 403, "rate limit exceeded", {}, None
        )
        with mock.patch("fetch_assets.fetch_json", side_effect=limit):
            code, plan = self.run_plan([package])
        self.assertEqual(code, 1, "a build cannot proceed on an unread asset list")
        self.assertTrue([e for e in plan["errors"] if e["blocks"]])

    def test_an_absent_release_is_missing(self):
        package = self.github_package()
        gone = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo/releases", 404, "Not Found", {}, None
        )
        with mock.patch("fetch_assets.fetch_json", side_effect=gone):
            code, plan = self.run_plan([package], allow_unreachable=True)
        statuses = {e["status"] for e in plan["errors"]}
        self.assertIn("missing", statuses)
        self.assertNotIn("unreachable", statuses)
        self.assertEqual(code, 1, "a missing release blocks even when unreachable is allowed")

    def test_unreachable_does_not_also_report_an_empty_resource_list(self):
        package = self.github_package()
        limit = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo/releases", 403, "rate limit exceeded", {}, None
        )
        with mock.patch("fetch_assets.fetch_json", side_effect=limit):
            _, plan = self.run_plan([package], allow_unreachable=True)
        reasons = [e["reason"] for e in plan["errors"]]
        self.assertFalse(
            [r for r in reasons if "no required resource was planned" in r],
            "the empty request list is a consequence of the limit, not a second fault",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
