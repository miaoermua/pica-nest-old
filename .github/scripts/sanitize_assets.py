#!/usr/bin/env python3
"""Give artifacts the names GitHub will actually store them under.

`pica-pack` derives a filename from `pkgver` verbatim, and the LuCI feed uses a
tilde to separate the build date from the commit (`26.223.24114~fe87581`).
A release asset name cannot carry that tilde: the upload rewrites it to `.`,
so the release ends up holding

    luci-app-homeproxy-26.223.24114.fe87581-1-amd64-x86_64-apk.pkg.tar.gz

while the index written from the local file names the tilde version, and every
client that follows `download_url` gets a 404. The verification step catches it
only once such a package is rebuilt, because entries this run did not build are
probed as warnings rather than required errors.

Renaming before the index is generated keeps the two in step. Nothing else has
to change: `check_versions.normalize_version` already treats `~` and `.` as one
version, and `pica` compares versions by value rather than by spelling.

Usage:
    sanitize_assets.py --root build_output [--dry-run]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from repo_layout import PKG_EXTENSION

# GitHub replaces these with a period when it stores an asset name. A space is
# included because the same rewrite applies to it, even though no package uses
# one today.
UNSAFE = str.maketrans({"~": ".", " ": "."})


def safe_name(filename):
    """The name GitHub will publish this asset under."""
    return filename.translate(UNSAFE)


def index_files(root):
    for directory, _dirs, files in os.walk(root):
        if "index.json" in files:
            yield os.path.join(directory, "index.json")


def rename_artifacts(root, dry_run):
    """Rename every artifact whose name would be rewritten, returning the map."""
    renamed = {}
    for directory, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(PKG_EXTENSION):
                continue
            target = safe_name(name)
            if target == name:
                continue
            source_path = os.path.join(directory, name)
            target_path = os.path.join(directory, target)
            if os.path.exists(target_path):
                print(
                    f"error: {name}: cannot rename to {target!r}, that name already exists",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            print(f"note: renamed {name} -> {target}")
            if not dry_run:
                os.replace(source_path, target_path)
            renamed[name] = target
    return renamed


def rewrite_indexes(root, renamed, dry_run):
    """Point each entry at its artifact's published name."""
    for path in index_files(root):
        with open(path, encoding="utf-8") as handle:
            index = json.load(handle)
        packages = index.get("packages")
        if not isinstance(packages, list):
            continue
        changed = 0
        for pkg in packages:
            current = pkg.get("filename")
            if current in renamed:
                pkg["filename"] = renamed[current]
                changed += 1
        if not changed or dry_run:
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(index, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(f"note: {path}: {changed} filename(s) updated")


def main():
    parser = argparse.ArgumentParser(description="Rename artifacts to the names GitHub publishes them under.")
    parser.add_argument("--root", required=True, help="directory holding the artifacts and their indexes")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without touching files")
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        parser.error(f"{args.root} is not a directory")

    renamed = rename_artifacts(args.root, args.dry_run)
    rewrite_indexes(args.root, renamed, args.dry_run)
    if not renamed:
        print("ok: every artifact name is already publishable")
        return 0
    verb = "would rename" if args.dry_run else "renamed"
    print(f"ok: {verb} {len(renamed)} artifact(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
