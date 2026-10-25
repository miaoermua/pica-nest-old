#!/usr/bin/env python3
"""Merge the index files that `pica-pack` emits into one repo index.

`pica-pack` writes the index next to each artifact, built from
`pica_pkg_core::manifest::Manifest`. That output is the authority on what
went into a package: it carries the required `pkgmgr`, keeps repeated manifest
fields such as `base`/`app` as arrays, and derives the filename suffix from
`pkgmgr`. Re-parsing manifests here would undo all of that, so this script only
merges, validates and rewrites URLs.

The packer changed the shape of that file in 0.4.0, and both shapes are
accepted so a source can move between packer versions without a coordinated
change:

* 0.3.x wrote `repo.json` — schema 2, one file holding every package of one
  build, the backend named by each entry's `pkgmgr`.
* 0.4.0 writes `index.json` per backend — schema 1, a `mgr` field naming the
  backend, and the same package entries underneath.

Only the envelope differs. The entries are identical, which is why the merge
below does not care which one it was handed.

Artifacts are **not** copied anywhere. The distribution design keeps binaries
in GitHub Releases; the repository tracks metadata only. The client resolves a
package in one of two ways (`pica-pkg-cli`):

* `download_url`, when present and a supported URL;
* otherwise `<repo>/packages/<filename>`.

The second form is why a `r/packages/` tree once appeared — the index left
`download_url` null, so the only way to satisfy the fallback was to commit the
archives. `--download-base` removes that pressure: every entry is stamped with
an absolute asset URL, so no client ever asks the repository for a binary.

Packages are keyed by `(pkgmgr, pkgname, platform, arch)`. All four are part of
a package's identity:

* `pkgmgr` — the same application may be published once per backend.
* `platform`/`arch` — one job builds a package for several architectures, and
  each produces its own artifact with its own filename. Keying without them
  collapses five artifacts onto one entry, so four of them have no entry naming
  them and the survivors overwrite each other in matrix order — which is an
  arbitrary architecture winning, not a choice.

The merged index is also the source of the `r/` layout: `r/index.json` and one
`r/<mgr>/index.json` per backend are written from it in the same call (see
`repo_layout.py`). They are views of this file, never a second place to edit —
which is why they are written here rather than by a step of their own, where
they could drift.

Usage:
    gen_repo_index.py --download-base URL [--repo REPO_JSON] <index.json>...
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import repo_layout  # noqa: E402  (path set above so the script runs from anywhere)

# Mirrors REPO_SCHEMA in crates/pica-pkg-core/src/repo.rs.
REPO_SCHEMA = 2
# Mirrors BACKEND_INDEX_SCHEMA in the same file: the per-backend index 0.4.0
# writes instead of a single repo.json.
BACKEND_INDEX_SCHEMA = 1
PKG_EXTENSION = ".pkg.tar.gz"
# Mirrors SUPPORTED_PKGMGRS in the same file.
SUPPORTED_PKGMGRS = ("opkg", "apk", "pica")
DEFAULT_REPO_JSON = "r/repo.json"


def load_pack_repo(path):
    """Load one index that `pica-pack` emitted, verifying it is a schema we expect.

    Two envelopes are accepted: the 0.3.x `repo.json` (schema 2) and the 0.4.0
    per-backend `index.json` (schema 1 with a `mgr` field). The backend of a
    backend index is named once, at the top, so its entries are checked against
    it — the same check `parse_backend_index` makes in the client, because a
    file that disagrees with itself is a build that mislabelled an artifact.
    """
    with open(path, "r", encoding="utf-8") as handle:
        index = json.load(handle)

    schema = index.get("schema")
    mgr = index.get("mgr")
    if mgr is None:
        if schema != REPO_SCHEMA:
            raise ValueError(f"{path}: schema must be {REPO_SCHEMA}, got {schema!r}")
    else:
        if schema != BACKEND_INDEX_SCHEMA:
            raise ValueError(
                f"{path}: a backend index must have schema {BACKEND_INDEX_SCHEMA},"
                f" got {schema!r}"
            )
        if mgr not in SUPPORTED_PKGMGRS:
            raise ValueError(f"{path}: unsupported backend {mgr!r}")
    if not isinstance(index.get("packages"), list):
        raise ValueError(f"{path}: packages must be an array")

    for pkg in index["packages"]:
        label = pkg.get("pkgname", "<unnamed>")
        for field in ("pkgname", "pkgver", "pkgrel", "pkgmgr", "filename", "sha256"):
            if not pkg.get(field):
                raise ValueError(f"{path}: package {label} missing {field}")
        if pkg["pkgmgr"] not in SUPPORTED_PKGMGRS:
            raise ValueError(f"{path}: package {label} has unsupported pkgmgr {pkg['pkgmgr']!r}")
        if mgr is not None and pkg["pkgmgr"] != mgr:
            raise ValueError(
                f"{path}: package {label} says pkgmgr {pkg['pkgmgr']!r}"
                f" but the index declares {mgr!r}"
            )
        if not pkg["filename"].endswith(PKG_EXTENSION):
            raise ValueError(f"{path}: {label} filename must end with {PKG_EXTENSION}")
    return index


def entry_key(pkg):
    """Identity of one index entry: one artifact, not one package name.

    Two entries that share these four fields name the same file, so one replaces
    the other. Anything less and a multi-architecture build loses entries.
    """
    return (
        pkg.get("pkgmgr", ""),
        pkg.get("pkgname", ""),
        pkg.get("platform", ""),
        pkg.get("arch", ""),
    )


def describe(key):
    """Name an entry the way `pica query` would, including where it runs."""
    pkgmgr, pkgname, platform, arch = key
    if platform or arch:
        return f"{pkgname} ({pkgmgr}, {platform}/{arch})"
    return f"{pkgname} ({pkgmgr})"


def asset_url(base, filename):
    """The Release asset URL a client should fetch this artifact from."""
    return f"{base.rstrip('/')}/{filename}"


def locate_artifact(source_dir, filename, extra_dirs):
    """Find an artifact, preferring the directory its index sits in.

    `pica-pack` writes both together. The publish job restores them separately
    (`dist/build_output/<backend>/index.json`, `dist/packages/<file>`), so the
    artifact directories are passed in as well.
    """
    for directory in (source_dir, *extra_dirs):
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return candidate
    return None


def main():
    parser = argparse.ArgumentParser(description="Merge the per-backend indexes pica-pack writes into one index.")
    parser.add_argument(
        "inputs",
        nargs="*",
        help=(
            "index files written by pica-pack — a backend index.json, or the"
            " 0.3.x repo.json; none is accepted with --download-base to"
            " re-stamp an existing index in place"
        ),
    )
    parser.add_argument("--repo", default=DEFAULT_REPO_JSON, help="merged index to write")
    parser.add_argument(
        "--index-root",
        help=(
            "directory for index.json and the per-backend indexes;"
            " defaults to the directory holding --repo"
        ),
    )
    parser.add_argument(
        "--download-base",
        help=(
            "base URL that artifacts are published under; every entry gets"
            " download_url=<base>/<filename> so clients never fall back to"
            " <repo>/packages/<filename>"
        ),
    )
    parser.add_argument(
        "--artifacts-dir",
        action="append",
        default=[],
        help=(
            "extra directory to look for artifacts in, searched after the"
            " index's own directory; the publish job stages artifacts and"
            " index metadata in separate directories"
        ),
    )
    parser.add_argument(
        "--release-list",
        help="write the artifacts produced by this run, one repository-relative path per line",
    )
    args = parser.parse_args()

    if not args.inputs and not args.download_base:
        parser.error("give at least one index file, or --download-base to re-stamp the index in place")

    if not os.path.isfile(args.repo):
        print(
            f"error: {args.repo} does not exist. Create a schema {REPO_SCHEMA} index first"
            " (the generator no longer invents one, because it cannot know the fields).",
            file=sys.stderr,
        )
        return 1

    with open(args.repo, "r", encoding="utf-8") as handle:
        existing = json.load(handle)
    previous_packages = existing.get("packages", [])
    if existing.get("schema") != REPO_SCHEMA:
        print(
            f"error: {args.repo} is schema {existing.get('schema')!r} but {REPO_SCHEMA} is required."
            " Rewrite it to schema 2 or delete it to regenerate.",
            file=sys.stderr,
        )
        return 1

    merged = {entry_key(pkg): pkg for pkg in existing.get("packages", [])}
    released = []
    rebuilt = []

    for path in args.inputs:
        try:
            index = load_pack_repo(path)
        except (OSError, ValueError, json.JSONDecodeError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 1

        # pica-pack writes repo.json next to the artifact it describes.
        source_dir = os.path.dirname(os.path.abspath(path))
        for pkg in index["packages"]:
            artifact = locate_artifact(source_dir, pkg["filename"], args.artifacts_dir)
            if artifact is None:
                # Publishing an entry whose asset does not exist would hand every
                # client a URL that 404s. Refuse before the index is rewritten.
                print(
                    f"error: {pkg['pkgname']}: artifact is missing: {pkg['filename']}"
                    f" (looked in {source_dir} and {args.artifacts_dir or 'nowhere else'})",
                    file=sys.stderr,
                )
                return 1
            # realpath on both sides: getcwd() returns the physical path, so on
            # hosts where the temp dir sits behind a symlink (macOS /var ->
            # /private/var) a plain relpath produces a bogus ../../.. chain.
            released.append(
                os.path.relpath(os.path.realpath(artifact), os.path.realpath(os.getcwd()))
            )
            key = entry_key(pkg)
            previous = merged.get(key)
            if previous and previous.get("pkgver") != pkg["pkgver"]:
                print(f"note: {describe(key)}: {previous.get('pkgver')} -> {pkg['pkgver']}")
            elif previous and previous.get("sha256") and previous["sha256"] != pkg.get("sha256"):
                # Same filename, different bytes: the rolling release asset is
                # overwritten before the index describing it is committed, so for
                # a moment the published index and the published file disagree.
                # That is a legitimate way to repair a package — it must not fail
                # the run — but it should be visible instead of being inferred
                # later from a hash that no longer matches anything.
                rebuilt.append(key)
                print(
                    f"note: {describe(key)}: rebuilt at the same version {pkg['pkgver']} —"
                    f" asset bytes change {previous['sha256'][:12]} ->"
                    f" {str(pkg.get('sha256', ''))[:12]}"
                )
            merged[key] = pkg

    if rebuilt:
        print(
            f"note: {len(rebuilt)} package(s) rebuilt under an unchanged version: their release"
            " assets are overwritten before the index that describes them is committed"
        )

    if args.download_base:
        # Applied to every entry, not just this run's: a historical entry with a
        # null download_url would still push clients onto the /packages/ path,
        # and its asset lives in the same rolling release.
        for pkg in merged.values():
            pkg["download_url"] = asset_url(args.download_base, pkg["filename"])

    packages = sorted(merged.values(), key=lambda pkg: (pkg["pkgname"], pkg["pkgmgr"], pkg["platform"], pkg["arch"]))
    existing["packages"] = packages
    # Only timestamp a real change. Bumping it unconditionally makes every
    # rebuild differ, so the publish job can never tell an identical rebuild
    # from a new version — and commits/releases pile up with no content change.
    if previous_packages != packages or "generated_at" not in existing:
        existing["generated_at"] = (
            datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        )
    # Repo-level metadata the operator owns.
    existing.setdefault("repo_name", "Pica 官方社区软件源")
    existing.setdefault("repo_desc", "Pica Official Community Software Source")

    with open(args.repo, "w", encoding="utf-8") as handle:
        json.dump(existing, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    # The layout is derived from what was just written, not from this run's
    # inputs, so a backend whose packages are all historical still gets an index
    # and one whose last package was dropped loses it.
    index_root = args.index_root or os.path.dirname(os.path.abspath(args.repo))
    try:
        _, written, pruned = repo_layout.write_layout(
            index_root,
            packages,
            existing["repo_name"],
            existing.get("repo_desc"),
            existing.get("generated_at"),
        )
    except repo_layout.LayoutError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    for relative in written:
        print(f"wrote {relative} under {index_root}")
    for relative in pruned:
        print(f"removed {relative}: no packages left for that manager")
    counts = {}
    for pkg in packages:
        counts[pkg["pkgmgr"]] = counts.get(pkg["pkgmgr"], 0) + 1
    print("managers: " + (", ".join(f"{mgr}={counts[mgr]}" for mgr in sorted(counts)) or "none"))

    if args.release_list:
        os.makedirs(os.path.dirname(os.path.abspath(args.release_list)) or ".", exist_ok=True)
        with open(args.release_list, "w", encoding="utf-8") as handle:
            for artifact in sorted(released):
                handle.write(artifact + "\n")

    print(f"updated {args.repo} with {len(packages)} package(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
