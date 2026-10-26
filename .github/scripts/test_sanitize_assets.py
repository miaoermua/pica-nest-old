#!/usr/bin/env python3
"""Tests for renaming artifacts to the names GitHub publishes them under."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "sanitize_assets.py"


def run(*arguments):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def write_artifact(root, name, payload=b"payload"):
    path = Path(root) / name
    path.write_bytes(payload)
    return path


def write_index(root, names, subdir="apk"):
    directory = Path(root) / subdir
    directory.mkdir(parents=True, exist_ok=True)
    index = {
        "schema": 1,
        "mgr": subdir,
        "packages": [
            {"pkgname": f"pkg{position}", "pkgver": "1", "pkgrel": "1",
             "pkgmgr": subdir, "filename": name, "sha256": "0" * 64}
            for position, name in enumerate(names)
        ],
    }
    path = directory / "index.json"
    path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return path


class SanitizeAssetsTest(unittest.TestCase):
    def test_tilde_version_is_renamed_everywhere(self):
        with tempfile.TemporaryDirectory() as root:
            old = "luci-app-homeproxy-26.223.24114~fe87581-1-amd64-x86_64-apk.pkg.tar.gz"
            new = "luci-app-homeproxy-26.223.24114.fe87581-1-amd64-x86_64-apk.pkg.tar.gz"
            write_artifact(root, old, b"bytes")
            index_path = write_index(root, [old])

            result = run("--root", root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(root) / old).exists())
            self.assertEqual((Path(root) / new).read_bytes(), b"bytes")
            packages = json.loads(index_path.read_text(encoding="utf-8"))["packages"]
            self.assertEqual(packages[0]["filename"], new)
            # Nothing but the name moves: the digest still describes the bytes.
            self.assertEqual(packages[0]["sha256"], "0" * 64)

    def test_publishable_names_are_left_alone(self):
        with tempfile.TemporaryDirectory() as root:
            name = "fastfetch-2.70.0-1-amd64-pica.pkg.tar.gz"
            write_artifact(root, name)
            index_path = write_index(root, [name])
            before = index_path.read_bytes()

            result = run("--root", root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already publishable", result.stdout)
            self.assertTrue((Path(root) / name).exists())
            self.assertEqual(index_path.read_bytes(), before)

    def test_a_name_taken_by_another_artifact_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            write_artifact(root, "demo-1.0~a.pkg.tar.gz", b"first")
            write_artifact(root, "demo-1.0.a.pkg.tar.gz", b"second")

            result = run("--root", root)

            self.assertEqual(result.returncode, 1)
            self.assertIn("already exists", result.stderr)
            # Neither file is touched when the rename cannot be completed.
            self.assertTrue((Path(root) / "demo-1.0~a.pkg.tar.gz").exists())
            self.assertEqual((Path(root) / "demo-1.0.a.pkg.tar.gz").read_bytes(), b"second")

    def test_dry_run_reports_without_touching_files(self):
        with tempfile.TemporaryDirectory() as root:
            name = "demo-1.0~a.pkg.tar.gz"
            write_artifact(root, name)
            index_path = write_index(root, [name])
            before = index_path.read_bytes()

            result = run("--root", root, "--dry-run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("would rename", result.stdout)
            self.assertTrue((Path(root) / name).exists())
            self.assertEqual(index_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
