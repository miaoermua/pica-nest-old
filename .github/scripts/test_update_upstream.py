#!/usr/bin/env python3
"""Tests for the upstream bumper: what it moves, and what it refuses to."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_versions  # noqa: E402
import update_upstream  # noqa: E402


class UpstreamBumpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self._saved = (
            check_versions.latest_tag,
            check_versions.release_asset_names,
            check_versions.fetch_text,
            update_upstream.head_ok,
        )
        update_upstream.head_ok = lambda url: True

    def tearDown(self):
        (
            check_versions.latest_tag,
            check_versions.release_asset_names,
            check_versions.fetch_text,
            update_upstream.head_ok,
        ) = self._saved
        self.tmp.cleanup()

    def write_job(self, source, pkgver="1.0.0", pkgrel=1, matrix=None):
        os.makedirs(os.path.join(self.root, "src/opkg/demo"), exist_ok=True)
        with open(os.path.join(self.root, "src/opkg/demo/manifest"), "w", encoding="utf-8") as handle:
            handle.write(f"pkgname = demo\n# keep me\npkgver = {pkgver}\npkgrel = {pkgrel}\n")
        os.makedirs(os.path.join(self.root, "ci/opkg/demo"), exist_ok=True)
        job = {
            "mgr": "opkg",
            "id": "demo",
            "manifest": "src/opkg/demo/manifest",
            "source": source,
            "versioning": {"pkgver": {"from": "manifest", "fallback": pkgver},
                           "pkgrel": {"from": "manifest", "fallback": pkgrel}},
            "build": {"matrix": matrix or [{"platform": "amd64", "arch": "x86_64"}]},
        }
        with open(os.path.join(self.root, "ci/opkg/demo/build.json"), "w", encoding="utf-8") as handle:
            json.dump(job, handle, indent=2)

    def run_plan(self):
        return update_upstream.plan(self.root)

    def apply_all(self, updates):
        for update in updates:
            update_upstream.apply(self.root, update)

    def manifest(self):
        with open(os.path.join(self.root, "src/opkg/demo/manifest"), encoding="utf-8") as handle:
            return handle.read()

    def job(self):
        with open(os.path.join(self.root, "ci/opkg/demo/build.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_a_name_shaped_regex_moves_the_version(self):
        self.write_job(
            {"provider": "github", "repo": "owner/demo",
             "release": {"tag": {"regex": r"^v[0-9.]+$"},
                         "assets": [{"name": "app", "regex": r"^demo-[0-9].*\.ipk$"}]}},
            pkgver="1.0.0",
        )
        check_versions.latest_tag = lambda repo: "v1.2.0"
        check_versions.release_asset_names = lambda repo, tag: ["demo-1.2.0-1_all.ipk"]

        updates, skipped = self.run_plan()
        self.assertEqual(skipped, [])
        self.assertEqual(len(updates), 1)
        self.apply_all(updates)

        self.assertIn("pkgver = 1.2.0", self.manifest())
        self.assertIn("pkgrel = 1", self.manifest())
        self.assertIn("# keep me", self.manifest(), "comments must survive the rewrite")
        self.assertEqual(self.job()["versioning"]["pkgver"]["fallback"], "1.2.0")

    def test_a_pinned_tag_regex_is_left_to_a_human(self):
        self.write_job(
            {"provider": "github", "repo": "owner/demo",
             "release": {"tag": {"regex": r"^v1\.0\.0$"},
                         "assets": [{"name": "app", "regex": r"^demo-[0-9].*\.ipk$"}]}},
        )
        check_versions.latest_tag = lambda repo: "v1.4.0"
        check_versions.release_asset_names = lambda repo, tag: ["demo-1.4.0-1_all.ipk"]

        updates, skipped = self.run_plan()
        self.assertEqual(updates, [])
        self.assertIn("pins v1.0.0", skipped[0][1])

    def test_an_asset_regex_carrying_the_version_is_not_a_bump(self):
        self.write_job(
            {"provider": "github", "repo": "owner/demo",
             "release": {"tag": {"regex": r"^v[0-9.]+$"},
                         "assets": [{"name": "app", "regex": r"^demo_1\.0\.0-r1_", "required": True}]}},
        )
        check_versions.latest_tag = lambda repo: "v1.4.0"
        check_versions.release_asset_names = lambda repo, tag: ["demo_1.4.0-r1_all.ipk"]

        updates, skipped = self.run_plan()
        self.assertEqual(updates, [])
        self.assertIn("no asset matches", skipped[0][1])

    def test_a_lower_version_never_replaces_a_four_part_one(self):
        self.write_job(
            {"provider": "github", "repo": "owner/demo",
             "release": {"tag": {"regex": r"^v[0-9.]+$"},
                         "assets": [{"name": "app", "regex": r"^demo-[0-9].*\.ipk$"}]}},
            pkgver="1.3.0.30",
        )
        check_versions.latest_tag = lambda repo: "v1.3"
        check_versions.release_asset_names = lambda repo, tag: ["demo-1.3-1_all.ipk"]

        updates, skipped = self.run_plan()
        self.assertEqual(updates, [])
        self.assertIn("not newer", skipped[0][1])

    def test_a_tag_that_is_not_a_version_is_not_a_bump(self):
        self.write_job(
            {"provider": "github", "repo": "owner/demo",
             "release": {"assets": [{"name": "app", "regex": r"^demo-[0-9].*\.ipk$"}]}},
            pkgver="26.156.15499~38397ed",
        )
        check_versions.latest_tag = lambda repo: "r20260920"
        check_versions.release_asset_names = lambda repo, tag: ["demo-3.0.0-1_all.ipk"]

        updates, skipped = self.run_plan()
        self.assertEqual(updates, [])
        self.assertIn("is not a version", skipped[0][1])

    def test_a_tarball_url_moves_with_the_tag(self):
        self.write_job(
            {"provider": "tarball", "arch_map": {"x86_64": "amd64"},
             "url_template": "https://github.com/owner/demo/releases/download/v1.0.0/demo-linux-{arch}.tar.gz",
             "strip_components": 0},
            pkgver="1.0.0",
        )
        check_versions.latest_tag = lambda repo: "v1.2.0"
        check_versions.release_asset_names = lambda repo, tag: ["demo-linux-amd64.tar.gz"]

        updates, _ = self.run_plan()
        self.assertEqual(len(updates), 1)
        self.apply_all(updates)
        template = self.job()["source"]["url_template"]
        self.assertEqual(
            template,
            "https://github.com/owner/demo/releases/download/v1.2.0/demo-linux-{arch}.tar.gz",
        )
        self.assertIn("pkgver = 1.2.0", self.manifest())

    def test_a_tarball_with_no_artifact_behind_the_new_tag_is_skipped(self):
        self.write_job(
            {"provider": "tarball",
             "url_template": "https://github.com/owner/demo/releases/download/v1.0.0/demo-linux-{arch}.tar.gz"},
            pkgver="1.0.0",
        )
        check_versions.latest_tag = lambda repo: "v1.2.0"
        check_versions.release_asset_names = lambda repo, tag: ["demo-linux-amd64.tar.gz"]
        update_upstream.head_ok = lambda url: False

        updates, skipped = self.run_plan()
        self.assertEqual(updates, [])
        self.assertIn("no artifact at", skipped[0][1])
        self.assertIn("pkgver = 1.0.0", self.manifest(), "nothing is written when a URL is dead")


if __name__ == "__main__":
    unittest.main()
