#!/usr/bin/env python3
"""Check that each job builds the version its manifest advertises.

A build can download a newer asset than the manifest names, or keep an old URL
long after the manifest moved. Neither is visible in the artifact itself: the
manifest is rewritten by pica-pack from the source manifest, and the downloaded
container keeps whatever version upstream shipped. The result is an index that
advertises one version while installing another.

This check is deliberately split in two:

* Offline (the default) compares the manifest with the versions written into
  the job's URLs and with a pinned release tag. It needs no network, so it runs
  on every build.
* `--online` also asks the upstream release page which assets the latest stable
  release carries, and reports jobs whose manifest version appears in none of
  them. That is a warning rather than a failure: an upstream release can change
  package layout, and a human has to decide whether the update is compatible.
  Feed packages are checked against their configured feed; the feed branch, not
  the project's newest release, is their upstream.

A per-architecture difference is accepted only when the job declares it in
`versioning.arch_versions`. A URL whose path names a different architecture
than the matrix entry is always an error, because that is how an x86_64 APK
ended up in every architecture's artifact.
"""
import argparse
import glob
import json
import os
import re
import sys
import urllib.request

# Architectures that appear as literal path segments in feed URLs. A path that
# names one of these while the matrix entry names another is a copy/paste bug.
KNOWN_ARCHES = {
    "x86_64",
    "i386_pentium4",
    "aarch64_generic",
    "aarch64_cortex-a53",
    "aarch64_cortex-a72",
    "aarch64_cortex-a76",
    "arm_cortex-a7",
    "arm_cortex-a7_neon-vfpv4",
    "arm_cortex-a9",
    "arm_cortex-a9_vfpv3-d16",
    "mipsel_24kc",
    "mips_24kc",
}


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def manifest_fields(path):
    fields = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields.setdefault(key.strip(), value.strip())
    return fields


def normalize_version(value):
    """Collapse the separators upstream uses interchangeably.

    `1.8-9`, `1.8.9` and `1.8_9` are one version; `26.156.15499~38397ed` and
    the filename's `26.156.15499.38397ed` are one package build. Hyphens that
    introduce a revision (`-r1`) collapse too, which is intentional: the
    manifest carries the upstream version, not the package revision.
    """
    return re.sub(r"[._~-]+", ".", value.strip().lower().lstrip("v"))


def version_in(text, version):
    """Whether `text` names `version` as a whole version token."""
    needle = normalize_version(version)
    haystack = normalize_version(text)
    if not needle:
        return False
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        before = haystack[index - 1] if index else ""
        after = haystack[index + len(needle)] if index + len(needle) < len(haystack) else ""
        if not before.isdigit() and not after.isdigit():
            return True
        start = index + 1


def matrix_entries(build):
    return build.get("build", {}).get("matrix", []) or []


def http_templates(source, arch):
    arch_templates = source.get("arch_url_templates") or {}
    if arch in arch_templates:
        return arch_templates[arch]
    return source.get("url_templates") or (
        [source["url_template"]] if source.get("url_template") else []
    )


def resolve_template(template, platform, arch, version=""):
    return (
        template.replace("{platform}", str(platform))
        .replace("{arch}", str(arch))
        .replace("{version}", str(version))
    )


def path_arch_error(url, arch):
    """The literal architecture in a URL path, when it contradicts the matrix."""
    path = url.split("?", 1)[0]
    for segment in path.split("/"):
        if segment in KNOWN_ARCHES and segment != arch:
            return segment
    return None


def pinned_tag(regex):
    r"""The literal version of a `^v1\.2\.3$`-shaped tag regex, or None."""
    if not regex or not regex.startswith("^") or not regex.endswith("$"):
        return None
    body = regex[1:-1].replace("\\.", ".")
    if re.search(r"[\[\](){}*+?|^$\\]", body):
        return None
    return body.lstrip("v")


def check_http(job, source, manifest, errors, rows):
    entries = matrix_entries(job)
    if not entries:
        errors.append("build.matrix is empty")
        return
    arch_versions = job.get("versioning", {}).get("arch_versions") or {}
    seen = []
    for entry in entries:
        platform = entry.get("platform", "all")
        arch = entry.get("arch")
        if not arch:
            errors.append("build.matrix entry is missing arch")
            continue
        templates = http_templates(source, arch)
        if not templates:
            errors.append(f"{platform}/{arch}: no URL template")
            continue
        urls = [resolve_template(t, platform, arch, source.get("version", "")) for t in templates]
        expected = arch_versions.get(arch, manifest.get("pkgver", ""))
        if not expected:
            errors.append(f"{platform}/{arch}: manifest has no pkgver")
            continue
        if not any(version_in(url, expected) for url in urls):
            errors.append(
                f"{platform}/{arch}: no URL carries version {expected!r}: " + ", ".join(urls)
            )
        for url in urls:
            wrong = path_arch_error(url, arch)
            if wrong:
                errors.append(
                    f"{platform}/{arch}: URL path names {wrong!r}, not the matrix arch: {url}"
                )
        seen.append(expected)
    if len(set(normalize_version(v) for v in seen)) > 1 and not arch_versions:
        errors.append(
            "architectures build different versions without versioning.arch_versions: "
            + ", ".join(sorted(set(seen)))
        )
    rows.append(("ok" if not errors else "mismatch", ", ".join(sorted(set(seen))) or "-"))


def check_tarball(job, source, manifest, errors, rows):
    templates = source.get("url_templates") or (
        [source["url_template"]] if source.get("url_template") else []
    )
    if not templates:
        errors.append("tarball source has no URL templates")
        return
    version = str(source.get("version", ""))
    if re.fullmatch(r"[0-9a-f]{40}", version):
        rows.append(("pinned commit", version[:12]))
        return
    entries = matrix_entries(job) or [{"platform": "all", "arch": "all"}]
    arch_map = source.get("arch_map") or {}
    expected = manifest.get("pkgver", "")
    matched = []
    for entry in entries:
        platform = entry.get("platform", "all")
        arch = entry.get("arch", "all")
        mapped = arch_map.get(arch, arch)
        urls = [resolve_template(t, platform, mapped, version) for t in templates]
        if not any(version_in(url, expected) for url in urls):
            errors.append(f"{platform}/{arch}: no URL carries version {expected!r}: " + ", ".join(urls))
        matched.append(expected)
    rows.append(("ok" if not errors else "mismatch", ", ".join(sorted(set(matched))) or "-"))


def check_github(job, source, manifest, errors, rows):
    tag = (source.get("release", {}) or {}).get("tag", {}) or {}
    regex = tag.get("regex", "")
    pinned = pinned_tag(regex)
    expected = manifest.get("pkgver", "")
    if pinned:
        if not version_in(pinned, expected):
            errors.append(f"release tag {pinned!r} does not carry manifest version {expected!r}")
        rows.append(("pinned tag" if not errors else "mismatch", pinned))
        return
    rows.append(("dynamic", regex or "-"))


def component_warnings(job, source, manifest):
    """Warn when a LuCI/i18n declaration has no matching planned resource.

    The plan names assets by filename (HTTP) or by the asset regex (GitHub), so
    a declaration that appears in neither will never be fetched. A core-only
    job that still advertises its LuCI app is the common case.
    """
    declared_apps = []
    declared_i18n = []
    manifest_path = job.get("manifest")
    if manifest_path and os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("app = "):
                    declared_apps.append(stripped.split("=", 1)[1].strip())
                elif stripped.startswith("app_i18n = "):
                    declared_i18n.append(stripped.split("=", 1)[1].strip())
    provider = source.get("provider")
    texts = []
    if provider == "http_template":
        texts = list(source.get("url_templates") or (
            [source["url_template"]] if source.get("url_template") else []
        ))
        for templates in (source.get("arch_url_templates") or {}).values():
            texts.extend(templates)
    elif provider == "github":
        texts = []
        for asset in source.get("release", {}).get("assets", []):
            texts.append(asset.get("regex", ""))
            if asset.get("inner_regex"):
                texts.append(asset["inner_regex"])
    else:
        return []
    warnings = []
    for app in declared_apps:
        if app.startswith("luci-app-") and not any(app in text for text in texts):
            warnings.append(f"manifest declares {app}, but no source asset matches it")
    for template in declared_i18n:
        prefix = template.split("{lang}", 1)[0].rstrip("-")
        if prefix and not any(prefix in text for text in texts):
            warnings.append(f"manifest declares {template}, but no source asset matches it")
    return warnings


def check_job(path, errors_by_job, rows_by_job, warnings=None):
    parts = path.replace("\\", "/").split("/")
    if len(parts) < 4:
        return
    mgr, pkg = parts[-3], parts[-2]
    job = load_json(path)
    manifest_path = job.get("manifest") or f"src/{mgr}/{pkg}/manifest"
    if not os.path.isfile(manifest_path):
        errors_by_job[path].append(f"manifest not found: {manifest_path}")
        return
    manifest = manifest_fields(manifest_path)
    source = job.get("source", {})
    provider = source.get("provider")
    errors = errors_by_job[path]
    rows = rows_by_job[path]
    if warnings is not None:
        warnings.extend(component_warnings(job, source, manifest))
    if provider == "http_template":
        check_http(job, source, manifest, errors, rows)
    elif provider == "github":
        check_github(job, source, manifest, errors, rows)
    elif provider == "tarball":
        check_tarball(job, source, manifest, errors, rows)
    else:
        errors.append(f"unknown provider {provider!r}")


def fetch_text(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": "Pica-CI-Version-Audit"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        request.add_header("Authorization", f"token {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def latest_tag(repo):
    url = f"https://github.com/{repo}/releases/latest"
    request = urllib.request.Request(url, headers={"User-Agent": "Pica-CI-Version-Audit"})
    with urllib.request.urlopen(request, timeout=30) as response:
        effective = response.geturl()
    marker = "/releases/tag/"
    return effective.split(marker, 1)[1] if marker in effective else None


def release_asset_names(repo, tag):
    url = f"https://github.com/{repo}/releases/expanded_assets/{tag}"
    html = fetch_text(url)
    names = re.findall(r"/releases/download/[^\" ]+/([^\"<>/]+)", html)
    return sorted(set(names))


def online_github(repo, manifest, warnings):
    try:
        tag = latest_tag(repo)
        if not tag:
            warnings.append(f"{repo}: no latest release tag")
            return "unverified"
        assets = release_asset_names(repo, tag)
        expected = manifest.get("pkgver", "")
        if any(version_in(name, expected) for name in assets):
            return f"latest {tag}"
        sample = ", ".join(assets[:4]) if assets else "(no assets)"
        warnings.append(
            f"{repo}: latest stable {tag} carries no asset for manifest {expected!r}; "
            f"first assets: {sample}"
        )
        return f"behind {tag}"
    except Exception as err:
        warnings.append(f"{repo}: could not verify latest release: {err}")
        return "unverified"


def online_tarball(source, manifest, warnings):
    urls = source.get("url_templates") or (
        [source["url_template"]] if source.get("url_template") else []
    )
    expected = manifest.get("pkgver", "")
    for url in urls:
        match = re.search(r"github\.com/([^/]+/[^/]+)/releases/download/", url)
        if match:
            repo = match.group(1)
            try:
                tag = latest_tag(repo)
                if tag and version_in(tag, expected):
                    return f"latest {tag}"
                warnings.append(f"{repo}: latest stable is {tag!r}, manifest is {expected!r}")
                return f"behind {tag}"
            except Exception as err:
                warnings.append(f"{repo}: could not verify latest release: {err}")
                return "unverified"
        if "pkgs.tailscale.com/stable/" in url:
            try:
                html = fetch_text("https://pkgs.tailscale.com/stable/")
                versions = sorted(
                    set(re.findall(r"tailscale_([0-9.]+)_", html)),
                    key=lambda value: [int(part) for part in value.split(".")],
                )
                latest = versions[-1] if versions else None
                if latest and version_in(latest, expected):
                    return f"latest {latest}"
                warnings.append(f"tailscale stable is {latest!r}, manifest is {expected!r}")
                return f"behind {latest}"
            except Exception as err:
                warnings.append(f"tailscale: could not verify stable version: {err}")
                return "unverified"
    return "skipped"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", nargs="*", default=None)
    parser.add_argument("--online", action="store_true", help="also query upstream releases")
    parser.add_argument("--strict-online", action="store_true", help="make online lag a failure")
    args = parser.parse_args()

    jobs = args.jobs or sorted(glob.glob("ci/*/*/build.json"))
    if not jobs:
        print("error: no build.json files found", file=sys.stderr)
        return 1

    errors_by_job = {path: [] for path in jobs}
    rows_by_job = {path: [] for path in jobs}
    static_warnings = []
    for path in jobs:
        check_job(path, errors_by_job, rows_by_job, static_warnings)

    warnings = []
    online_rows = {}
    if args.online:
        for path in jobs:
            try:
                job = load_json(path)
                parts = path.replace("\\", "/").split("/")
                mgr, pkg = parts[-3], parts[-2]
                manifest = manifest_fields(
                    job.get("manifest") or f"src/{mgr}/{pkg}/manifest"
                )
                source = job.get("source", {})
                if source.get("provider") == "github":
                    online_rows[path] = online_github(source.get("repo", ""), manifest, warnings)
                elif source.get("provider") == "tarball":
                    online_rows[path] = online_tarball(source, manifest, warnings)
            except Exception as err:
                warnings.append(f"{path}: online audit failed: {err}")

    print("Version metadata audit:")
    print(f"{'package':38} {'mgr':5} {'manifest':18} {'source':24} {'status'}")
    failures = 0
    for path in jobs:
        job = load_json(path)
        parts = path.replace("\\", "/").split("/")
        mgr, pkg = parts[-3], parts[-2]
        manifest = manifest_fields(job.get("manifest") or f"src/{mgr}/{pkg}/manifest")
        rows = rows_by_job[path]
        status, source_version = rows[0] if rows else ("skipped", "-")
        if errors_by_job[path]:
            status = "MISMATCH"
        if path in online_rows:
            source_version = f"{source_version}; {online_rows[path]}"
        print(f"{pkg:38} {mgr:5} {manifest.get('pkgver',''):18} {source_version:24} {status}")
        for error in errors_by_job[path]:
            print(f"  error: {error}", file=sys.stderr)
            failures += 1

    for warning in static_warnings + warnings:
        print(f"warning: {warning}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning::{warning}")

    if failures:
        print(f"\n{failures} version mismatch(es)", file=sys.stderr)
        return 1
    if args.strict_online and warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
