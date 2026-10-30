#!/usr/bin/env python3
"""Rewrite packages whose upstream published a release the manifest has not got.

The scheduled audit reports which packages are behind. This turns the ones that
are *safe* to move into a branch, so a human reviews a diff instead of editing
version strings by hand.

Safe is narrower than "a newer release exists". A release only becomes a bump
when all of this holds:

- the tag reads as a version (`v3.1.1`, `2026.10.6`). A tag like `r20260920` or a
  job whose `pkgver` is a LuCI build string (`26.156.15499~38397ed`) means the
  manifest version comes from somewhere other than the tag, and reading it off
  the tag would rename the package backwards;
- the tag regex does not pin a release. `^v2\\.5\\.0$` is a job that follows one
  tag by hand, not one an updater may move;
- every asset regex still matches the new release. `^ddns-go_6\\.9\\.1-r1_` stops
  matching the moment upstream releases — that is a job to rewrite by hand, and
  guessing there produces a package that fails its own build;
- the candidate is strictly newer than the manifest under a numeric comparison,
  so `1.3` never replaces `1.3.0.30`;
- every URL the bump would produce answers 200, expanded for every matrix arch
  and translated through `arch_map`.

Anything failing these is reported with the reason and left alone. A pull
request full of plausible-looking wrong bumps is worse than no pull request.

What moves:

    src/<mgr>/<id>/manifest     pkgver, pkgrel
    ci/<mgr>/<id>/build.json    every occurrence of the old version: the URL of a
                                `tarball` source, `source.version`, the version
                                fallbacks

`http_template` sources are not touched: their version lives in a feed directory
that has to be listed to find the file name, and the audit cannot see them
upstream either.

Usage:
    update_upstream.py [--apply] [--json PATH] [--jobs ci/...]

Without `--apply` nothing is written; the plan is printed and, with `--json`,
saved for the pull request body.
"""

import argparse
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_versions  # noqa: E402  (latest_tag, release_asset_names, normalize_version, pinned_tag)

# A tag that doubles as the package version. `v3.1.1`, `2026.10.6`, `1.104.4`.
VERSION_TAG = re.compile(r"^v?\d+(?:\.\d+)*$")


def read_manifest(path):
    """The manifest's first value per key. Absent keys are left out."""
    fields = {}
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in fields:
                fields[key] = value.strip()
    return fields


def same_version(left, right):
    """`1.8.9`, `1.8-9` and `v1.8.9` are one version; `26.156~abc` keeps its hash."""
    return bool(left) and check_versions.normalize_version(left) == check_versions.normalize_version(right)


def version_key(text):
    """Numeric tuple for "is this newer", ignoring anything that is not a number."""
    parts = []
    for chunk in check_versions.normalize_version(text).split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def set_manifest_version(path, pkgver, pkgrel):
    """Rewrite `pkgver`/`pkgrel` in place, keeping every comment and the order."""
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines(keepends=True)
    seen = set()
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key == "pkgver":
            lines[index] = f"pkgver = {pkgver}\n"
            seen.add("pkgver")
        elif key == "pkgrel" and pkgrel is not None:
            lines[index] = f"pkgrel = {pkgrel}\n"
            seen.add("pkgrel")
    if "pkgver" not in seen:
        raise ValueError(f"{path}: no pkgver line to rewrite")
    if pkgrel is not None and "pkgrel" not in seen:
        lines.append(f"pkgrel = {pkgrel}\n")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("".join(lines))


def rewrite_json(path, pairs):
    """Swap literal strings in a build.json without reformatting the file.

    A load/dump round trip would reindent everything into the diff. The
    replacements are version tokens and URL segments, so a literal swap is both
    narrower and easier to read; the result is parsed afterwards to prove it is
    still JSON.
    """
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    for old, new in pairs:
        if old and new and old != new:
            text = text.replace(old, new)
    json.loads(text)  # fail here rather than at build time
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def set_version_fields(path, current, candidate):
    """Point `source.version` and `versioning.pkgver.fallback` at the new version.

    Both are inert when the manifest declares the field — which it always does —
    but a fallback left on the old version is a trap for whoever removes the
    manifest line later. Anchored on the key so a same-shaped string elsewhere
    (an arch, a tag) is not rewritten by accident.
    """
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    before = text
    text = re.sub(
        r'("versioning"\s*:\s*\{[^{}]*?"pkgver"\s*:\s*\{[^{}]*?"fallback"\s*:\s*")[^"]*(")',
        rf"\g<1>{candidate}\g<2>",
        text,
        count=1,
        flags=re.S,
    )
    text = re.sub(
        r'("source"\s*:\s*\{[^{}]*?"version"\s*:\s*")[^"]*(")',
        rf"\g<1>{candidate}\g<2>",
        text,
        count=1,
        flags=re.S,
    )
    if text == before:
        return
    json.loads(text)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def templates_of(source):
    return source.get("url_templates") or (
        [source["url_template"]] if source.get("url_template") else []
    )


def substitute(text, pairs):
    for old, new in pairs:
        if old and new:
            text = text.replace(old, new)
    return text


def expand(templates, source, pairs):
    """Every template expanded for every matrix arch, as the fetcher would."""
    arch_map = source.get("arch_map") or {}
    out = []
    for template in templates:
        local = substitute(template, pairs)
        for arch in sorted({entry.get("arch") for entry in source["_matrix"] if entry.get("arch")}):
            url = local.replace("{arch}", arch_map.get(arch, arch)).replace("{version}", "")
            if "{" not in url:
                out.append(url)
    return out


def head_ok(url):
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Pica-CI-Update"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return 200 <= response.status < 400
    except urllib.error.HTTPError as error:
        return 200 <= error.code < 400
    except Exception:
        return False


def newer(candidate, current):
    """Whether `candidate` is strictly newer than `current`, numerically."""
    if not candidate or not current:
        return False
    return version_key(candidate) > version_key(current)


def decide_github(source, manifest):
    """(new_version, None) when the release satisfies every declared regex."""
    release = source.get("release") or {}
    tag_regex = (release.get("tag") or {}).get("regex")

    pinned = check_versions.pinned_tag(tag_regex)
    if pinned:
        return None, f"tag regex pins v{pinned}; unpin it before automating"

    repo = source.get("repo", "")
    tag = check_versions.latest_tag(repo)
    if not tag:
        return None, "no stable release tag"
    if not VERSION_TAG.match(tag):
        return None, f"tag {tag} is not a version; pkgver comes from elsewhere"
    if tag_regex and not re.match(tag_regex, tag):
        return None, f"tag regex does not match {tag}"

    candidate = tag.lstrip("v")
    current = manifest.get("pkgver", "")
    if same_version(candidate, current):
        return None, "up to date"
    if not newer(candidate, current):
        return None, f"{candidate} is not newer than {current}"

    assets = check_versions.release_asset_names(repo, tag)
    for meta in release.get("assets") or []:
        pattern = meta.get("regex")
        if pattern and not any(re.match(pattern, name) for name in assets):
            return None, f"no asset matches {pattern} in {tag}"

    return candidate, None


def decide_tarball(source, manifest):
    """(new_version, pairs, None) for a URL that carries the version itself."""
    current = manifest.get("pkgver", "")
    templates = templates_of(source)

    for template in templates:
        github = re.search(r"github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/", template)
        if github:
            repo, old_tag = github.group(1), github.group(2)
            tag = check_versions.latest_tag(repo)
            if not tag:
                return None, [], "no stable release tag"
            if not VERSION_TAG.match(tag):
                return None, [], f"tag {tag} is not a version; pkgver comes from elsewhere"
            candidate = tag.lstrip("v")
            if same_version(candidate, current):
                return None, [], "up to date"
            if not newer(candidate, current):
                return None, [], f"{candidate} is not newer than {current}"
            pairs = [(f"/releases/download/{old_tag}/", f"/releases/download/{tag}/")]
            if current and current != candidate:
                pairs.append((current, candidate))
            urls = expand(templates, source, pairs)
            dead = [url for url in urls if not head_ok(url)]
            if dead:
                return None, [], f"no artifact at {dead[0]}"
            return candidate, pairs, None

        if "pkgs.tailscale.com/stable/" in template:
            page = check_versions.fetch_text("https://pkgs.tailscale.com/stable/")
            versions = {value for value in re.findall(r"tailscale_([0-9.]+)_", page)}
            latest = max(versions, key=version_key) if versions else None
            if not latest:
                return None, [], "no stable version on the page"
            if same_version(latest, current):
                return None, [], "up to date"
            if not newer(latest, current):
                return None, [], f"{latest} is not newer than {current}"
            pairs = [(f"tailscale_{current}_", f"tailscale_{latest}_"), (current, latest)]
            urls = expand(templates, source, pairs)
            dead = [url for url in urls if not head_ok(url)]
            if dead:
                return None, [], f"no artifact at {dead[0]}"
            return latest, pairs, None

    return None, [], "no version source for this URL"


def plan(root, jobs_filter=None):
    """What would change, and why the rest would not."""
    updates, skipped = [], []
    for build_path in sorted(glob.glob(os.path.join(root, "ci", "*", "*", "build.json"))):
        relative = os.path.relpath(build_path, root).replace(os.sep, "/")
        if jobs_filter and not any(relative.startswith(prefix) for prefix in jobs_filter):
            continue
        with open(build_path, "r", encoding="utf-8") as handle:
            job = json.load(handle)
        if job.get("enabled") is False:
            continue
        source = job.get("source", {})
        source["_matrix"] = job.get("build", {}).get("matrix") or []
        manifest_path = os.path.join(root, job.get("manifest") or "")
        if not os.path.isfile(manifest_path):
            skipped.append((relative, "manifest is missing"))
            continue
        manifest = read_manifest(manifest_path)
        provider = source.get("provider")

        if provider == "github":
            candidate, reason = decide_github(source, manifest)
            pairs = []
        elif provider == "tarball":
            candidate, pairs, reason = decide_tarball(source, manifest)
        else:
            candidate, reason, pairs = None, f"{provider} source is not checked upstream", []

        if not candidate:
            if reason != "up to date":
                skipped.append((relative, reason))
            continue

        updates.append(
            {
                "job": relative,
                "manifest": os.path.relpath(manifest_path, root).replace(os.sep, "/"),
                "from": manifest.get("pkgver", ""),
                "to": candidate,
                "pairs": pairs,
            }
        )

    return updates, skipped


def apply(root, update):
    set_manifest_version(os.path.join(root, update["manifest"]), update["to"], 1)
    job_path = os.path.join(root, update["job"])
    if update["pairs"]:
        rewrite_json(job_path, update["pairs"])
    set_version_fields(job_path, update["from"], update["to"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--json", dest="json_path", help="write the plan for the pull request body")
    parser.add_argument("--jobs", nargs="*", default=None, help="ci/... prefixes to limit to")
    args = parser.parse_args()

    updates, skipped = plan(args.root, args.jobs)

    if updates:
        print(f"{'package':<44}{'from':<16}{'to'}")
        for update in updates:
            print(f"{update['job']:<44}{update['from']:<16}{update['to']}")
        if args.apply:
            for update in updates:
                apply(args.root, update)
            print(f"\nupdated {len(updates)} package(s)")
        else:
            print(f"\n{len(updates)} package(s) would be updated (dry run)")
    else:
        print("Nothing to update.")

    for job, reason in skipped:
        print(f"note: {job}: {reason}", file=sys.stderr)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump({"updates": updates, "skipped": skipped}, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
