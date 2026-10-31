#!/usr/bin/env python3
"""Map changed repository paths to the packages a CI run must rebuild.

The workflow triggers on `ci/**` as well as `src/**`, but the old change filter
only looked at `src/**` directories: editing `ci/<mgr>/<pkg>/build.json` woke
the workflow and then selected nothing, so the package was never rebuilt.

This script classifies every changed path, including both sides of a rename and
paths that only exist in the old tree, and reports:

* `packages`        - `src/<mgr>/<pkg>` directories to rebuild
* `removed`         - packages whose source and job config are both gone
* `full_validation` - a shared input changed, so every job must be checked
* `full_rebuild`    - no reliable diff range, so every package must be rebuilt

A touched package whose `manifest` or `build.json` is missing is an error, not
a silent skip: that pair is the repository's layout invariant.

Usage:
    changed_packages.py --base <sha> --head <sha> [--github-output PATH]
    changed_packages.py --force-rebuild-all [--github-output PATH]
    changed_packages.py --full-validation-only [--github-output PATH]
"""
import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

# Mirrors SUPPORTED_PKGMGRS in validate_repo.py and gen_repo_index.py.
BACKENDS = ("opkg", "apk", "pica")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ZERO_SHA = "0" * 40


@dataclass(frozen=True)
class Change:
    """One `git diff --name-status` entry."""

    status: str
    old: str
    new: str = None

    def paths(self):
        """Both sides of a rename; the single path of everything else."""
        return (self.old,) if self.new is None else (self.old, self.new)


@dataclass(frozen=True)
class Classification:
    kind: str  # "package", "global" or "ignore"
    mgr: str = ""
    pkg: str = ""
    reason: str = ""


@dataclass
class Plan:
    packages: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    full_validation: bool = False
    full_rebuild: bool = False
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def classify(path):
    """Decide what one repository-relative path belongs to."""
    parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts:
        return Classification("ignore", reason="empty path")

    top = parts[0]
    if top in ("src", "ci"):
        if len(parts) >= 4 and parts[1] in BACKENDS:
            return Classification("package", mgr=parts[1], pkg=parts[2])
        if any(part.lower().endswith(".md") for part in parts):
            return Classification("ignore", reason="documentation")
        return Classification("global", reason=f"{top}/ carries shared build inputs")

    if top == "schema":
        return Classification("global", reason="schema/ defines the job and index contracts")

    if top == ".github":
        if len(parts) >= 3 and parts[1] == "workflows":
            return Classification("global", reason="workflow")
        if len(parts) == 3 and parts[1] == "scripts" and parts[2].endswith(".py"):
            return Classification("global", reason="CI script")
        return Classification("ignore", reason="repository metadata")

    if top in ("r", "docs", "worker", "state"):
        return Classification("ignore", reason="generated output, docs or unrelated tooling")

    if len(parts) == 1 and top in ("README.md", "LICENSE", ".gitignore", "wrangler.toml"):
        return Classification("ignore", reason="documentation or deployment config")

    return Classification("global", reason=f"unrecognised path {path!r}")


def package_paths(mgr, pkg):
    return f"src/{mgr}/{pkg}", f"ci/{mgr}/{pkg}/build.json"


def plan(changes, exists):
    """Map parsed changes to a build plan.

    `exists(path)` is injected so the mapping is testable without a checkout.
    It must answer for the tree at the new commit: a deleted file is absent,
    even though the old path is what selected its package.
    """
    result = Plan()
    packages, removed, errors, notes = set(), set(), set(), set()

    for change in changes:
        for path in change.paths():
            classified = classify(path)
            if classified.kind == "global":
                result.full_validation = True
                notes.add(f"{path}: {classified.reason}")
            elif classified.kind == "package":
                src, job = package_paths(classified.mgr, classified.pkg)
                if exists(f"{src}/manifest"):
                    if exists(job):
                        packages.add(src)
                    else:
                        errors.add(f"{src}: has a manifest but {job} is missing")
                elif exists(job):
                    errors.add(f"{job}: exists but {src}/manifest is missing")
                else:
                    removed.add(src)

    result.packages = sorted(packages)
    result.removed = sorted(removed)
    result.errors = sorted(errors)
    result.notes = sorted(notes)
    return result


def full_plan(root="."):
    """Every package under `src/` with its `build.json` paired."""
    result = Plan(full_validation=True, full_rebuild=True)
    packages, errors = set(), set()

    src_root = os.path.join(root, "src")
    for mgr in sorted(os.listdir(src_root)) if os.path.isdir(src_root) else []:
        if mgr not in BACKENDS:
            continue
        mgr_dir = os.path.join(src_root, mgr)
        if not os.path.isdir(mgr_dir):
            continue
        for pkg in sorted(os.listdir(mgr_dir)):
            if not os.path.isfile(os.path.join(mgr_dir, pkg, "manifest")):
                continue
            src, job = package_paths(mgr, pkg)
            if os.path.isfile(os.path.join(root, job)):
                packages.add(src)
            else:
                errors.add(f"{src}: has a manifest but {job} is missing")

    result.packages = sorted(packages)
    result.errors = sorted(errors)
    return result


def select_packages(value, exists):
    """Validate an explicit `src/<mgr>/<pkg>` list for a manual run."""
    result = Plan()
    packages = set()
    for raw in value.replace(",", " ").split():
        path = raw.strip().rstrip("/")
        parts = path.replace("\\", "/").split("/")
        if len(parts) != 3 or parts[0] != "src" or parts[1] not in BACKENDS:
            result.errors.append(f"{raw!r}: expected src/<mgr>/<pkg>")
            continue
        src = "/".join(parts)
        job = f"ci/{parts[1]}/{parts[2]}/build.json"
        if not exists(f"{src}/manifest"):
            result.errors.append(f"{src}: manifest is missing")
        elif not exists(job):
            result.errors.append(f"{src}: {job} is missing")
        else:
            packages.add(src)
    result.packages = sorted(packages)
    if not result.packages and not result.errors:
        result.errors.append("--packages selected no package")
    return result


def parse_name_status(data):
    """Parse `git diff --name-status -z` output."""
    tokens = data.split(b"\0")
    changes = []
    index = 0

    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        status = token.decode("utf-8", "surrogateescape")
        code = status[:1]
        if code in ("R", "C"):
            if index + 1 >= len(tokens):
                raise ValueError(f"truncated rename entry: {status!r}")
            old = tokens[index].decode("utf-8", "surrogateescape")
            new = tokens[index + 1].decode("utf-8", "surrogateescape")
            index += 2
            if not old or not new:
                raise ValueError(f"truncated rename entry: {status!r}")
            changes.append(Change(status, old, new))
        else:
            if index >= len(tokens) or not tokens[index]:
                raise ValueError(f"truncated entry: {status!r}")
            changes.append(Change(status, tokens[index].decode("utf-8", "surrogateescape")))
            index += 1

    return changes


def is_zero_sha(value):
    return value == ZERO_SHA


def _git(*args):
    return subprocess.run(["git", *args], check=False, capture_output=True)


def commit_exists(sha):
    return SHA_RE.match(sha) is not None and _git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def path_exists_at(commit, path):
    """Whether `path` exists in `commit`, the tree the plan describes."""
    return _git("cat-file", "-e", f"{commit}:{path}").returncode == 0


def diff_changes(base, head):
    """Changed paths between two commits, tolerating a shallow checkout.

    `base...head` is the right range when the two share history: it reports
    what this push did, not everything upstream changed meanwhile. A shallow
    clone has no merge base to compute it from — the fetched base sits on its
    own root — so the two-dot form is used instead. That form lists whatever
    differs between the two trees: identical for a fast-forward push, and a
    superset for anything else. Over-reporting costs a rebuild; under-reporting
    would skip a package, so the trade is the right way round.
    """
    detail = ""
    for spec in (f"{base}...{head}", f"{base}..{head}"):
        proc = _git("diff", "--name-status", "-z", "-M", "--diff-filter=ACMRDT", spec)
        if proc.returncode == 0:
            return parse_name_status(proc.stdout)
        detail = proc.stderr.decode("utf-8", "replace").strip() or detail
    raise RuntimeError(detail or "git diff failed")


def write_outputs(path, plan_result, mode):
    values = {
        "packages": " ".join(plan_result.packages),
        "removed": " ".join(plan_result.removed),
        "full_validation": "true" if plan_result.full_validation else "false",
        "full_rebuild": "true" if plan_result.full_rebuild else "false",
        "mode": mode,
    }
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def report(plan_result, mode):
    if plan_result.errors:
        for error in plan_result.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"mode: {mode}")
    for note in plan_result.notes:
        print(f"note: {note}")
    print(f"packages to build ({len(plan_result.packages)}): {' '.join(plan_result.packages) or '(none)'}")
    if plan_result.removed:
        print(f"removed packages ({len(plan_result.removed)}): {' '.join(plan_result.removed)}")
        print("note: a removed package keeps its previous r/repo.json entry; retire it there if intended")
    print(f"full validation: {'yes' if plan_result.full_validation else 'no'}")
    print(f"full rebuild: {'yes' if plan_result.full_rebuild else 'no'}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--base", help="commit before the change (github.event.before)")
    mode_group.add_argument("--force-rebuild-all", action="store_true", help="rebuild every package")
    mode_group.add_argument(
        "--full-validation-only",
        action="store_true",
        help="validate globally without rebuilding (manual run without a diff range)",
    )
    mode_group.add_argument(
        "--packages",
        help="explicit space/comma-separated src/<mgr>/<pkg> list for a manual run",
    )
    parser.add_argument("--head", help="commit after the change (github.sha)")
    parser.add_argument("--github-output", help="append step outputs to this file")
    args = parser.parse_args()

    if args.force_rebuild_all:
        result, mode = full_plan(), "full-rebuild"
        result.notes.append("rebuild forced")
    elif args.full_validation_only:
        result, mode = Plan(full_validation=True), "full-validation"
        result.notes.append("manual run without a diff range; no package was selected")
    elif args.packages is not None:
        result, mode = select_packages(args.packages, os.path.isfile), "manual-selection"
        result.notes.append("packages selected explicitly")
    else:
        if not args.head:
            print("error: --head is required with --base", file=sys.stderr)
            return 2
        if not SHA_RE.match(args.base) or not SHA_RE.match(args.head):
            print("error: --base and --head must be full 40-character commit SHAs", file=sys.stderr)
            return 2
        if is_zero_sha(args.base):
            # A new branch has no parent to diff against. Rebuilding everything
            # is the safe answer; reporting nothing would skip the build.
            result, mode = full_plan(), "full-rebuild"
            result.notes.append("base is the zero SHA (new branch); rebuilding every package")
        else:
            if not commit_exists(args.base) or not commit_exists(args.head):
                print(
                    "error: cannot resolve --base/--head; refusing to guess the change range "
                    "(is the checkout deep enough?)",
                    file=sys.stderr,
                )
                return 2
            try:
                changes = diff_changes(args.base, args.head)
            except (RuntimeError, ValueError) as err:
                print(f"error: cannot determine the changed files: {err}", file=sys.stderr)
                return 2
            result = plan(changes, lambda path: path_exists_at(args.head, path))
            mode = "diff"

    status = report(result, mode)
    if status == 0 and args.github_output:
        write_outputs(args.github_output, result, mode)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
