#!/usr/bin/env python3
"""Verify that the published index describes artifacts that really exist.

`gen_repo_index.py` merges and validates JSON shape. That proves the index is
well formed, not that each entry names a real, downloadable artifact. This
script checks the three things a client depends on:

1. **The artifact** — open the archive, compare its embedded `manifest` with the
   index entry, recompute SHA-256 and size, and confirm it carries a payload.
2. **The URL** — every entry must have a `download_url` under the configured
   base. A null `download_url` makes `pica` fall back to
   `<repo>/packages/<filename>`, which is the path that put binaries in Git.
3. **The target** — with `--probe`, HEAD each `download_url` so a broken Release
   upload is caught before the index is committed; with `--verify-published`,
   download the entries this run built and compare the bytes with the index.

A HEAD proves a URL answers, not that the bytes behind it are the ones the index
describes: an upload that overwrote a filename with a rebuilt archive still
answers 200, under a hash that now belongs to nothing. `--verify-published` is
the only check that looks at what a client would actually receive.

Artifacts are located by filename across one or more `--artifacts-dir`
directories (`pica-pack` writes `repo.json` next to the artifact it describes).
Entries named in `--require-file` — the files built by this run — are strict: a
missing file, a missing index entry, a URL mismatch or a failed download fails
the run. Entries from earlier runs are not in this checkout, so they get the
URL check and a best-effort probe, and a warning never blocks a new build.
"""
import argparse
import hashlib
import json
import os
import sys
import tarfile
import urllib.error
import urllib.request

SUFFIXES = {"opkg": "-opkg", "apk": "-apk", "pica": "-pica"}
PROBE_TIMEOUT = 20
# A published package is a few megabytes; the timeout covers a slow runner
# reading it back, not a stalled connection.
PUBLISHED_TIMEOUT = 120
USER_AGENT = "pica-pkg-repo-ci/1.0 (+https://github.com/miaoermua/pica-pkg-repo)"
GZIP_MAGIC = b"\x1f\x8b"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(archive):
    with tarfile.open(archive, "r:gz") as tar:
        member = next((item for item in tar.getmembers() if item.name == "manifest"), None)
        if member is None:
            return None
        return tar.extractfile(member).read().decode("utf-8", "replace")


def manifest_fields(text):
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields.setdefault(key.strip(), value.strip())
    return fields


def payload_paths(archive):
    with tarfile.open(archive, "r:gz") as tar:
        return [item.name for item in tar.getmembers()]


def expected_filename(pkg):
    suffix = SUFFIXES.get(pkg.get("pkgmgr"))
    if not suffix:
        return None
    platform = pkg.get("platform", "all")
    stem = (
        f"{pkg['pkgname']}-{pkg['pkgver']}-{pkg['pkgrel']}-{pkg['arch']}"
        if platform == "all"
        else f"{pkg['pkgname']}-{pkg['pkgver']}-{pkg['pkgrel']}-{platform}-{pkg['arch']}"
    )
    return stem + suffix + ".pkg.tar.gz"


def load_required(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def find_artifact(filename, directories):
    """Locate one artifact by basename; pica-pack keeps repo.json next to it."""
    for directory in directories:
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return candidate
    return None


def probe(url):
    """HEAD a published URL. Returns (reason, content-length); reason is None on success."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            if response.status != 200:
                return f"HTTP {response.status}", None
            length = response.headers.get("Content-Length")
            return None, int(length) if length and length.isdigit() else None
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}", None
    except (urllib.error.URLError, OSError, ValueError) as error:
        return f"{type(error).__name__}: {error}", None


def fetch_published(url):
    """GET a published asset. Returns (bytes, None) or (None, reason)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=PUBLISHED_TIMEOUT) as response:
            if response.status != 200:
                return None, f"HTTP {response.status}"
            body = response.read()
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        return None, f"{type(error).__name__}: {error}"

    # A captive portal or an error page can arrive with a 200. A package archive
    # is a gzip stream; anything else is not the file the index named, whatever
    # the status said.
    if not body.startswith(GZIP_MAGIC):
        head = body[:40].decode("utf-8", "replace").replace("\n", " ")
        return None, f"not a gzip archive ({len(body)} bytes: {head!r})"
    return body, None


def check_published(pkg, url, errors):
    """Compare the bytes a client would download with the index entry."""
    filename = pkg["filename"]
    body, reason = fetch_published(url)
    if reason:
        errors.append(f"{filename}: published asset is unusable: {reason}")
        return

    digest = hashlib.sha256(body).hexdigest()
    expected = str(pkg.get("sha256", "")).lower()
    if digest != expected:
        errors.append(f"{filename}: published sha256 {digest} does not match index {expected}")
    csize = pkg.get("csize")
    if isinstance(csize, int) and csize > 0 and csize != len(body):
        errors.append(f"{filename}: published size {len(body)} does not match index csize {csize}")


def check_url(pkg, download_base, strict, errors, warnings):
    """The URL is the whole point of the fix: clients must never need /packages/."""
    filename = pkg["filename"]
    url = pkg.get("download_url")
    problem = errors if strict else warnings
    if not url:
        problem.append(
            f"{filename}: no download_url, so clients fall back to <repo>/packages/{filename}"
        )
        return None
    if download_base:
        expected = f"{download_base.rstrip('/')}/{filename}"
        if url != expected:
            problem.append(f"{filename}: download_url {url!r} is not {expected!r}")
    return url


def check_artifact(pkg, archive, strict, errors, warnings):
    filename = pkg["filename"]
    problem = errors if strict else warnings
    size = os.path.getsize(archive)
    digest = sha256_file(archive)
    if digest != str(pkg.get("sha256", "")).lower():
        problem.append(f"{filename}: sha256 {digest} does not match index {pkg.get('sha256')}")
    csize = pkg.get("csize")
    if isinstance(csize, int) and csize > 0 and csize != size:
        problem.append(f"{filename}: size {size} does not match index csize {csize}")

    try:
        manifest_text = read_manifest(archive)
        members = payload_paths(archive)
    except (OSError, tarfile.TarError) as error:
        problem.append(f"{filename}: cannot read archive: {error}")
        return
    if manifest_text is None:
        problem.append(f"{filename}: archive has no manifest")
        return

    fields = manifest_fields(manifest_text)
    for key in ("pkgname", "pkgver", "pkgrel", "platform", "arch", "pkgmgr"):
        if fields.get(key) != str(pkg.get(key, "")):
            problem.append(
                f"{filename}: manifest {key}={fields.get(key)!r} does not match index "
                f"{pkg.get(key)!r}"
            )

    manager = pkg.get("pkgmgr")
    # Ask the packer's question, not a stricter one: can this artifact install
    # anything? `pica-pack` accepts a package that ships containers for its
    # backend, `root/` files, or `scripts/` that install it, and refuses only
    # when none of the three is present.
    #
    # Requiring `pkgs/` from every native backend gets that wrong. cattools
    # declares `targets = opkg, pica` with its runtime dependencies handed to
    # opkg (`base@opkg = bash` …) and its payload delivered by `scripts/install`
    # — there is deliberately no `.ipk` for it to carry. The packer built it;
    # this check called it empty.
    has_payload = any(name.startswith(("pkgs/", "root/", "scripts/")) for name in members)
    if not has_payload:
        problem.append(f"{filename}: no payload (none of pkgs/, root/ or scripts/) for {manager}")

    expected = expected_filename(pkg)
    if expected and expected != filename:
        # Historical artifacts predate the backend suffix; only fail a strict
        # entry, where the current packer was expected to produce the name.
        (errors if strict else warnings).append(
            f"{filename}: filename does not match the current convention ({expected})"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default="r/repo.json")
    parser.add_argument(
        "--artifacts-dir",
        action="append",
        default=[],
        help="directory holding this run's artifacts; repeatable",
    )
    parser.add_argument(
        "--packages-dir",
        action="append",
        default=[],
        help="alias for --artifacts-dir, kept for older callers",
    )
    parser.add_argument(
        "--download-base",
        help="expected download_url prefix; every entry must match it exactly",
    )
    parser.add_argument(
        "--require-file",
        help="newline-separated artifact paths built by this run; missing or mismatched entries fail",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="HEAD every download_url; required entries must download",
    )
    parser.add_argument(
        "--verify-published",
        action="store_true",
        help=(
            "download every required download_url and compare its bytes with the index;"
            " a HEAD only proves the URL answers, not which bytes it answers with"
        ),
    )
    parser.add_argument("--strict-all", action="store_true", help="treat historical artifacts strictly too")
    args = parser.parse_args()

    directories = args.artifacts_dir + args.packages_dir

    with open(args.index, "r", encoding="utf-8") as handle:
        index = json.load(handle)
    packages = index.get("packages", [])
    required_paths = load_required(args.require_file)
    required = {os.path.basename(path) for path in required_paths}
    if args.strict_all:
        required |= {pkg["filename"] for pkg in packages}

    errors = []
    warnings = []

    # A built artifact with no index entry means the merge dropped it: the file
    # would be uploaded and never referenced, or worse never uploaded at all.
    indexed = {pkg["filename"] for pkg in packages}
    for path in required_paths:
        if not os.path.isfile(path):
            errors.append(f"{path}: built artifact is missing")
        elif os.path.basename(path) not in indexed:
            errors.append(f"{path}: built artifact has no index entry")

    probed = set()
    for pkg in packages:
        filename = pkg["filename"]
        strict = filename in required
        archive = find_artifact(filename, directories)

        if archive is None:
            if strict:
                errors.append(
                    f"{filename}: required artifact not found in {directories or 'no --artifacts-dir'}"
                )
        else:
            check_artifact(pkg, archive, strict, errors, warnings)

        url = check_url(pkg, args.download_base, strict, errors, warnings)
        if args.verify_published and url and strict:
            # The local artifact above is what this run built; this is what a
            # client would receive. Both have to agree with the index.
            check_published(pkg, url, errors)
        elif (args.probe or args.verify_published) and url and url not in probed:
            probed.add(url)
            reason, length = probe(url)
            if reason:
                (errors if strict else warnings).append(f"{filename}: cannot download {url}: {reason}")
            elif (
                length is not None
                and isinstance(pkg.get("csize"), int)
                and pkg["csize"] > 0
                and length != pkg["csize"]
            ):
                # Cheap size check on entries this run did not build: it catches
                # a republished file and a missing one without downloading every
                # historical artifact on every publish.
                (errors if strict else warnings).append(
                    f"{filename}: published size {length} does not match index csize {pkg['csize']}"
                )

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    print(f"Verified {len(packages)} index entries ({len(required)} required).")
    if errors:
        print(f"{len(errors)} artifact error(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
