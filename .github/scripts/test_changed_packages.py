#!/usr/bin/env python3
"""Tests for changed_packages.py. Run with `python3 .github/scripts/test_changed_packages.py`.

Uses the standard library only: the repository has no Python test framework and
this keeps the change-detection logic runnable on a bare runner.
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from changed_packages import (  # noqa: E402
    Change,
    classify,
    diff_changes,
    full_plan,
    is_zero_sha,
    parse_name_status,
    plan,
    select_packages,
)


def plan_for(changes, existing=()):
    existing = set(existing)
    return plan(changes, lambda path: path in existing)


def pair(mgr, pkg):
    return {f"src/{mgr}/{pkg}/manifest", f"ci/{mgr}/{pkg}/build.json"}


class ClassifyTest(unittest.TestCase):
    def test_src_file_maps_to_its_package(self):
        got = classify("src/opkg/hello/manifest")
        self.assertEqual((got.kind, got.mgr, got.pkg), ("package", "opkg", "hello"))

    def test_ci_build_json_maps_to_its_package(self):
        got = classify("ci/apk/demo/build.json")
        self.assertEqual((got.kind, got.mgr, got.pkg), ("package", "apk", "demo"))

    def test_ci_readme_is_documentation(self):
        self.assertEqual(classify("ci/README.md").kind, "ignore")

    def test_schema_is_global(self):
        self.assertEqual(classify("schema/ci-job-v1.schema.json").kind, "global")

    def test_workflow_is_global(self):
        self.assertEqual(classify(".github/workflows/build-and-release.yml").kind, "global")

    def test_top_level_script_is_global(self):
        self.assertEqual(classify(".github/scripts/fetch_assets.py").kind, "global")

    def test_fixtures_are_ignored(self):
        self.assertEqual(classify(".github/scripts/fixtures/dir-mismatch/ci/opkg/demo/build.json").kind, "ignore")

    def test_docs_are_ignored(self):
        for path in ("docs/README.md", "README.md", ".github/scripts/README.md"):
            self.assertEqual(classify(path).kind, "ignore", path)

    def test_generated_index_is_ignored(self):
        self.assertEqual(classify("r/repo.json").kind, "ignore")

    def test_unknown_top_level_is_global(self):
        self.assertEqual(classify("tools/new-helper.sh").kind, "global")


class PlanTest(unittest.TestCase):
    def test_src_only_selects_one_package(self):
        result = plan_for([Change("M", "src/opkg/hello/manifest")], pair("opkg", "hello"))
        self.assertEqual(result.packages, ["src/opkg/hello"])
        self.assertFalse(result.full_validation)
        self.assertEqual(result.errors, [])

    def test_ci_only_selects_one_package(self):
        result = plan_for([Change("M", "ci/opkg/hello/build.json")], pair("opkg", "hello"))
        self.assertEqual(result.packages, ["src/opkg/hello"])
        self.assertFalse(result.full_validation)

    def test_multiple_packages_are_all_selected_without_duplicates(self):
        changes = [
            Change("M", "src/opkg/hello/manifest"),
            Change("M", "ci/opkg/hello/build.json"),
            Change("M", "src/apk/demo/root/usr/bin/demo"),
            Change("M", "ci/apk/demo/build.json"),
        ]
        result = plan_for(changes, pair("opkg", "hello") | pair("apk", "demo"))
        self.assertEqual(result.packages, ["src/apk/demo", "src/opkg/hello"])

    def test_added_build_json_selects_the_package(self):
        result = plan_for([Change("A", "ci/opkg/new/build.json")], pair("opkg", "new"))
        self.assertEqual(result.packages, ["src/opkg/new"])

    def test_deleted_build_json_with_source_is_an_error(self):
        result = plan_for(
            [Change("D", "ci/opkg/hello/build.json")],
            {"src/opkg/hello/manifest"},
        )
        self.assertEqual(result.packages, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn("ci/opkg/hello/build.json is missing", result.errors[0])

    def test_deleted_source_with_job_is_an_error(self):
        result = plan_for(
            [Change("D", "src/opkg/hello/manifest")],
            {"ci/opkg/hello/build.json"},
        )
        self.assertEqual(result.packages, [])
        self.assertIn("src/opkg/hello/manifest is missing", result.errors[0])

    def test_deleted_package_is_reported_not_built(self):
        result = plan_for(
            [
                Change("D", "src/opkg/hello/manifest"),
                Change("D", "ci/opkg/hello/build.json"),
            ],
            (),
        )
        self.assertEqual(result.packages, [])
        self.assertEqual(result.removed, ["src/opkg/hello"])
        self.assertEqual(result.errors, [])

    def test_renamed_package_builds_new_and_reports_old(self):
        changes = [
            Change("R100", "src/opkg/old/manifest", "src/opkg/new/manifest"),
            Change("R100", "ci/opkg/old/build.json", "ci/opkg/new/build.json"),
        ]
        result = plan_for(changes, pair("opkg", "new"))
        self.assertEqual(result.packages, ["src/opkg/new"])
        self.assertEqual(result.removed, ["src/opkg/old"])
        self.assertEqual(result.errors, [])

    def test_global_file_requests_full_validation_without_a_rebuild(self):
        result = plan_for([Change("M", "schema/ci-job-v1.schema.json")], pair("opkg", "hello"))
        self.assertTrue(result.full_validation)
        self.assertFalse(result.full_rebuild)
        self.assertEqual(result.packages, [])

    def test_ignored_change_selects_nothing(self):
        result = plan_for([Change("M", "docs/README.md")], pair("opkg", "hello"))
        self.assertEqual(result.packages, [])
        self.assertFalse(result.full_validation)
        self.assertEqual(result.removed, [])


class ParseNameStatusTest(unittest.TestCase):
    def test_modified_deleted_and_renamed_entries(self):
        data = (
            b"M\0src/opkg/a/manifest\0"
            b"D\0src/opkg/b/manifest\0"
            b"R100\0ci/opkg/a/build.json\0ci/opkg/c/build.json\0"
        )
        changes = parse_name_status(data)
        self.assertEqual(
            [(change.status, change.old, change.new) for change in changes],
            [
                ("M", "src/opkg/a/manifest", None),
                ("D", "src/opkg/b/manifest", None),
                ("R100", "ci/opkg/a/build.json", "ci/opkg/c/build.json"),
            ],
        )

    def test_truncated_rename_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_name_status(b"R100\0only-one-path\0")


class FullPlanTest(unittest.TestCase):
    def test_scans_every_package_and_flags_a_missing_job(self):
        with tempfile.TemporaryDirectory() as root:
            for relative in (
                "src/opkg/hello/manifest",
                "ci/opkg/hello/build.json",
                "src/apk/demo/manifest",
            ):
                path = os.path.join(root, relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w", encoding="utf-8").close()

            result = full_plan(root)
            self.assertEqual(result.packages, ["src/opkg/hello"])
            self.assertTrue(result.full_validation)
            self.assertTrue(result.full_rebuild)
            self.assertEqual(len(result.errors), 1)
            self.assertIn("ci/apk/demo/build.json is missing", result.errors[0])


class SelectPackagesTest(unittest.TestCase):
    def test_explicit_packages_are_validated(self):
        existing = pair("opkg", "hello") | pair("pica", "demo")
        result = select_packages("src/opkg/hello, src/pica/demo", lambda path: path in existing)
        self.assertEqual(result.packages, ["src/opkg/hello", "src/pica/demo"])
        self.assertEqual(result.errors, [])

    def test_bad_or_incomplete_paths_are_errors(self):
        result = select_packages("src/opkg/missing ops/opkg/hello", lambda _path: False)
        self.assertEqual(result.packages, [])
        self.assertEqual(len(result.errors), 2)

    def test_empty_selection_is_an_error(self):
        result = select_packages("", lambda _path: True)
        self.assertEqual(result.packages, [])
        self.assertEqual(len(result.errors), 1)


class RangeTest(unittest.TestCase):
    def test_zero_sha_is_the_only_unknown_range_marker(self):
        self.assertTrue(is_zero_sha("0" * 40))
        self.assertFalse(is_zero_sha("1" * 40))
        self.assertFalse(is_zero_sha(""))


class DiffAcrossUnrelatedRootsTest(unittest.TestCase):
    """The diff still answers when the two commits share no history.

    A shallow clone fetches the base commit on its own, with no ancestor in
    common, and `git diff base...head` needs a merge base it cannot compute.
    Falling back to the two-dot form is what keeps a shallow build from
    refusing to work out what changed.
    """

    def git(self, cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_two_roots_still_produce_a_change_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.git(tmp, "init", "-q")
            self.git(tmp, "config", "user.email", "ci@example.com")
            self.git(tmp, "config", "user.name", "ci")

            os.makedirs(os.path.join(tmp, "ci", "opkg", "demo"))
            with open(os.path.join(tmp, "ci", "opkg", "demo", "build.json"), "w") as handle:
                handle.write("{}\n")
            self.git(tmp, "add", "-A")
            self.git(tmp, "commit", "-q", "-m", "first root")
            base = self.git(tmp, "rev-parse", "HEAD")

            # A second root: nothing above is an ancestor of it.
            self.git(tmp, "checkout", "-q", "--orphan", "other")
            self.git(tmp, "rm", "-rq", "--cached", ".")
            for name in os.listdir(tmp):
                if name != ".git":
                    path = os.path.join(tmp, name)
                    if os.path.isdir(path):
                        import shutil

                        shutil.rmtree(path)
                    else:
                        os.remove(path)
            os.makedirs(os.path.join(tmp, "ci", "opkg", "other"))
            with open(os.path.join(tmp, "ci", "opkg", "other", "build.json"), "w") as handle:
                handle.write("{}\n")
            self.git(tmp, "add", "-A")
            self.git(tmp, "commit", "-q", "-m", "second root")
            head = self.git(tmp, "rev-parse", "HEAD")

            previous = os.getcwd()
            os.chdir(tmp)
            try:
                changes = diff_changes(base, head)
            finally:
                os.chdir(previous)

            paths = {path for change in changes for path in change.paths()}
            self.assertIn("ci/opkg/other/build.json", paths)


if __name__ == "__main__":
    unittest.main(verbosity=2)
