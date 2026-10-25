import concurrent.futures
import hashlib
import os
import sys
import json
import re
import time
import urllib.error
import urllib.request
import tarfile
import zipfile
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from container_metadata import (
    check_downloaded,
    container_metadata,
    declares_package,
    manifest_dependencies,
    manifest_pkgver,
    version_matches,
)
from validate_repo import check_job_fallback, job_layout_errors, try_jsonschema

_JSON_CACHE = {}


def _retryable(error):
    """Whether a failed request is worth retrying.

    A 404 is evidence that the resource is not there; retrying it wastes a
    request and hides the real problem. 429 and 5xx are transient, and a
    timeout says nothing about the resource.
    """
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 429, 500, 502, 503, 504)
    return isinstance(error, (urllib.error.URLError, TimeoutError, OSError))


def _sleep_before_retry(attempt):
    time.sleep(min(2 ** attempt, 8))


def fetch_json(url, attempts=3):
    """Fetch JSON, deduplicating identical URLs within one process.

    Several jobs share a GitHub repository (ua3f, ua-mask, the themes), so the
    same release list would otherwise be requested once per job. The cache is
    only for one run: a release list is metadata, not a build input.
    """
    if url in _JSON_CACHE:
        return _JSON_CACHE[url]
    print(f"Fetching {url}...")
    headers = {
        'User-Agent': 'Pica-CI-Asset-Fetcher',
        'Accept': 'application/vnd.github.v3+json'
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"

    last_error = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                value = json.loads(response.read().decode())
            _JSON_CACHE[url] = value
            return value
        except Exception as error:
            last_error = error
            if not _retryable(error) or attempt + 1 >= attempts:
                raise
            _sleep_before_retry(attempt)
    raise last_error


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_paths(cache_dir, url):
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, key + ".bin"), os.path.join(cache_dir, key + ".json")


def _cache_load(cache_dir, url, path, expected_size, expected_sha256):
    """Copy a verified cache entry to `path`, or return False.

    The metadata is checked before the bytes are trusted: a file whose name
    happens to match is not evidence that its content is the requested asset.
    """
    data_path, meta_path = _cache_paths(cache_dir, url)
    if not (os.path.isfile(data_path) and os.path.isfile(meta_path)):
        return False
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    if meta.get("url") != url:
        return False
    if expected_size is not None and meta.get("size") != expected_size:
        return False
    if expected_sha256 and meta.get("sha256") != expected_sha256:
        return False
    if sha256_file(data_path) != meta.get("sha256"):
        return False
    shutil.copyfile(data_path, path)
    print(f"Cache hit: {url}")
    return True


def _cache_remove(cache_dir, url):
    if not cache_dir:
        return
    data_path, meta_path = _cache_paths(cache_dir, url)
    for path in (data_path, meta_path):
        try:
            os.remove(path)
        except OSError:
            pass


def _cache_store(cache_dir, url, path, size, digest):
    os.makedirs(cache_dir, exist_ok=True)
    data_path, meta_path = _cache_paths(cache_dir, url)
    tmp_data = data_path + ".tmp"
    tmp_meta = meta_path + ".tmp"
    shutil.copyfile(path, tmp_data)
    with open(tmp_meta, "w", encoding="utf-8") as handle:
        json.dump({"url": url, "size": size, "sha256": digest}, handle)
    os.replace(tmp_data, data_path)
    os.replace(tmp_meta, meta_path)


def download_to(url, path, expected_size=None, expected_sha256=None, cache_dir=None, attempts=3):
    """Download `url` to `path`, returning (ok, bytes_written, error).

    The file is removed on failure, so a partial download can never be
    mistaken for a resource. A declared Content-Length (or a size from the
    GitHub release metadata) is checked before the download is accepted.
    Network errors and 429/5xx are retried; 404 is not. A cache entry is
    trusted only after its URL, size and SHA-256 have been checked.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if cache_dir:
        try:
            if _cache_load(cache_dir, url, path, expected_size, expected_sha256):
                return True, os.path.getsize(path), ""
        except OSError as error:
            print(f"warning: asset cache read failed: {error}")

    headers = {'User-Agent': 'Mozilla/5.0 (Pica-CI-Asset-Fetcher)'}
    token = os.environ.get("GITHUB_TOKEN")
    if "github.com" in url and token:
        headers["Authorization"] = f"token {token}"

    last_error = ""
    for attempt in range(attempts):
        print(f"Downloading {url} to {path}...")
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as response:
                declared = response.headers.get("Content-Length")
                written = 0
                with open(path, 'wb') as out_file:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        out_file.write(chunk)
                        written += len(chunk)
            if declared is not None:
                try:
                    expected_header = int(declared)
                except ValueError:
                    expected_header = None
                if expected_header is not None and written != expected_header:
                    os.remove(path)
                    last_error = f"truncated download: {written} of {expected_header} bytes"
                    if attempt + 1 < attempts:
                        _sleep_before_retry(attempt)
                        continue
                    return False, written, last_error
            if expected_size is not None and written != expected_size:
                os.remove(path)
                return False, written, f"size mismatch: got {written}, expected {expected_size}"
            digest = sha256_file(path)
            if expected_sha256 and digest != expected_sha256:
                os.remove(path)
                last_error = f"sha256 mismatch: got {digest}, expected {expected_sha256}"
                if attempt + 1 < attempts:
                    _sleep_before_retry(attempt)
                    continue
                return False, written, last_error
            if cache_dir:
                try:
                    _cache_store(cache_dir, url, path, written, digest)
                except OSError as error:
                    print(f"warning: asset cache write failed: {error}")
            return True, written, ""
        except Exception as error:
            last_error = str(error)
            try:
                os.remove(path)
            except OSError:
                pass
            if not _retryable(error) or attempt + 1 >= attempts:
                return False, 0, last_error
            _sleep_before_retry(attempt)
    return False, 0, last_error


def download_file(url, path, **kwargs):
    ok, _, error = download_to(url, path, **kwargs)
    if not ok:
        print(f"Failed to download from {url}: {error}")
    return ok

def extract_all_from_zip(zip_path, inner_regex, dest_dir):
    """
    Extract every file inside a zip that matches inner_regex into dest_dir.

    A release zip can carry several IPKs that belong to one architecture: the
    core package, the LuCI app and its translations. Extracting only the first
    match silently drops the rest, which is how an EasyTier artifact ended up
    with the 27 KB LuCI app and neither the 14 MB core nor the translation.
    """
    print(f"Extracting all files matching '{inner_regex}' from {zip_path}...")
    written = []
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(tmp_dir)
                os.makedirs(dest_dir, exist_ok=True)
                for root, _, files in os.walk(tmp_dir):
                    for f in files:
                        if not re.search(inner_regex, f):
                            continue
                        src = os.path.join(root, f)
                        target = os.path.join(dest_dir, os.path.basename(f))
                        shutil.move(src, target)
                        written.append(target)
                        print(f"Successfully extracted to {target}")
    except Exception as e:
        print(f"Extraction failed: {e}")
        return None
    return sorted(written)

def fetch_github_assets(pkg_dir, build_config):
    source = build_config.get("source", {})
    repo = source.get("repo")
    release_cfg = source.get("release", {})
    tag_cfg = release_cfg.get("tag", {})
    tag_regex = tag_cfg.get("regex")
    allow_prerelease = release_cfg.get("allow_prerelease", False)

    api_url = f"https://api.github.com/repos/{repo}/releases"
    try:
        releases = fetch_json(api_url)
    except Exception as e:
        print(f"Error fetching releases from {repo}: {e}")
        return False

    target_release = None
    for r in releases:
        if r.get("prerelease") and not allow_prerelease:
            continue
        if tag_regex:
            if not re.match(tag_regex, r.get("tag_name", "")):
                continue
        target_release = r
        break

    if not target_release:
        print(f"No matching GitHub release found for {repo}")
        return False

    print(f"Matched GitHub release: {target_release['tag_name']}")
    
    assets_cfg = release_cfg.get("assets", [])
    matrix = build_config.get("build", {}).get("matrix", [])
    found_any = False

    for asset_meta in assets_cfg:
        asset_regex = asset_meta.get("regex")
        inner_regex = asset_meta.get("inner_regex") # For zip extraction
        capture_to = asset_meta.get("capture_to", {})
        fixed_target = asset_meta.get("target", {})
        required = asset_meta.get("required", True)

        found_this_asset_meta = False
        for a in target_release.get("assets", []):
            match = re.match(asset_regex, a["name"])
            if match:
                target_platform = fixed_target.get("platform")
                target_arch = fixed_target.get("arch")

                if capture_to:
                    for key, group_idx in capture_to.items():
                        val = match.group(int(group_idx))
                        if key == "arch":
                            target_arch = (source.get("arch_map") or {}).get(val, val)
                        elif key == "platform": target_platform = val

                # If we have an arch but no platform, look it up in the matrix
                if target_arch and not target_platform:
                    in_matrix = False
                    for m in matrix:
                        if m.get("arch") == target_arch:
                            target_platform = m.get("platform")
                            in_matrix = True
                            break
                    # STRICT FILTERING: If capture_to was used, we only download if it's in our matrix
                    if not in_matrix:
                        print(f"Skipping {a['name']}: Architecture {target_arch} not in matrix.")
                        continue
                
                if not target_platform: target_platform = "all"
                if not target_arch: target_arch = "all"

                # Pica 包布局：`pkgs/<backend>/<platform>/<arch>/`。后端取自
                # build.json 的 `mgr`，与 manifest 的 `targets` 必须一致，否则
                # pica-pack 会以"pkgs/<backend>/ present but <backend> is not
                # in targets"拒绝构建。
                #
                # An arch-independent container goes into every matrix row, for
                # the reason given in `plan_github_assets`: one copy under
                # `all/all` would be a package of its own instead of a part of
                # each architecture's package.
                matrix_arches = [m.get("arch") for m in matrix if m.get("arch")]
                platform_by_arch = {m.get("arch"): m.get("platform", "all") for m in matrix if m.get("arch")}
                if target_arch == "all" and matrix_arches and "all" not in matrix_arches:
                    destinations = [(platform_by_arch[a], a) for a in matrix_arches]
                else:
                    destinations = [(target_platform, target_arch)]

                backend = build_config.get("mgr", "opkg")
                for dest_platform, dest_arch in destinations:
                    dest_dir = os.path.join(pkg_dir, "pkgs", backend, dest_platform, dest_arch)

                    # If we need to extract from zip, we download to a temporary location first
                    if inner_regex and a["name"].endswith(".zip"):
                        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_zip:
                            zip_tmp_path = tmp_zip.name

                        if download_file(a["browser_download_url"], zip_tmp_path):
                            extracted = extract_all_from_zip(zip_tmp_path, inner_regex, dest_dir)
                            if extracted:
                                found_any = True
                                found_this_asset_meta = True
                            os.remove(zip_tmp_path)
                    else:
                        dest_path = os.path.join(dest_dir, a["name"])
                        if download_file(a["browser_download_url"], dest_path):
                            found_any = True
                            found_this_asset_meta = True
        
        if not found_this_asset_meta and required:
            print(f"Required GitHub asset '{asset_regex}' not found.")
            return False

    return found_any

def infer_role(filename):
    """Which part of a bundle a container belongs to, from its filename."""
    lowered = filename.lower()
    if "luci-i18n" in lowered:
        return "i18n"
    if "luci-app" in lowered or "luci-theme" in lowered:
        return "luci"
    return "core"


def http_template_urls(source):
    """The common URL templates for the http_template provider."""
    urls = []
    if "url_template" in source:
        urls.append(source["url_template"])
    if "url_templates" in source:
        urls.extend(source["url_templates"])
    return urls


def http_template_urls_for_arch(source, arch):
    """The templates one architecture uses.

    An `arch_url_templates` entry replaces the common list for that arch, so a
    single architecture can fetch a different version without that URL being
    downloaded into every other architecture's directory. Arches without an
    entry keep the common list.
    """
    arch_templates = source.get("arch_url_templates") or {}
    if arch in arch_templates:
        return arch_templates[arch]
    return http_template_urls(source)


def fetch_http_template_assets(pkg_dir, build_config):
    source = build_config.get("source", {})
    url_templates = http_template_urls(source)
    arch_templates = source.get("arch_url_templates") or {}

    if not url_templates and not arch_templates:
        print(
            "Error: 'url_template', 'url_templates' or 'arch_url_templates' "
            "missing for http_template provider."
        )
        return False

    version = source.get("version", "latest")
    matrix = build_config.get("build", {}).get("matrix", [])
    if not matrix:
        print("Warning: No matrix defined for http_template fetch.")
        return False

    matrix_arches = {m.get("arch") for m in matrix}
    for arch in arch_templates:
        if arch not in matrix_arches:
            print(
                f"warning: arch_url_templates names {arch!r}, which is not in build.matrix",
                file=sys.stderr,
            )

    found_any = False
    for m in matrix:
        arch = m.get("arch")
        platform = m.get("platform", "all")
        if not arch: continue

        for url_template in http_template_urls_for_arch(source, arch):
            url = url_template.replace("{arch}", arch).replace("{version}", version).replace("{platform}", platform)
            filename = url.split("/")[-1]
            backend = build_config.get("mgr", "opkg")
            dest_dir = os.path.join(pkg_dir, "pkgs", backend, platform, arch)
            dest_path = os.path.join(dest_dir, filename)

            if download_file(url, dest_path):
                found_any = True
            else:
                print(f"Could not download asset for {arch} from {url}")

    return found_any

def extract_tarball(pkg_dir, build_config):
    """
    Download a release archive and unpack it into the package's root/.

    A source that ships no package of its own — a bare upstream tarball — has
    nowhere else to go: the artifacts a `pica` backend package carries are
    plain files under `root/`, and there is no container to put the download in.
    Unpacking it here is what makes that shape buildable.

    `strip_components` drops that many leading path components, because these
    archives wrap everything in one directory whose name is tied to the release
    (`fastfetch-musl-amd64/...`). Leaving it in place would put the binary one
    level too deep, and the command would not be where the manifest says it is.

    `include` narrows the unpack to the paths a device actually needs: a full
    archive also carries shell completions and man pages, which are dead weight
    on a router.
    """
    source = build_config["source"]
    url_template = source.get("url_template")
    url_templates = source.get("url_templates") or ([url_template] if url_template else [])
    if not url_templates:
        print("error: tarball provider needs url_template or url_templates", file=sys.stderr)
        return False

    strip = source.get("strip_components", 0)
    includes = source.get("include") or []
    matrix = build_config.get("build", {}).get("matrix", [{}])
    root_dir = os.path.join(pkg_dir, "root")

    ok = True
    for entry in matrix:
        platform = entry.get("platform", "all")
        arch = entry.get("arch", "all")
        matched = False

        for template in url_templates:
            # A source may name architectures its own way. A Go project calls
            # x86_64 `amd64` and aarch64 `arm64`, and a URL carrying that name
            # cannot be built by substituting the manifest's arch into it —
            # arch_map is how a package says which of the two it means.
            mapped = (source.get("arch_map") or {}).get(arch, arch)
            url = (template
                   .replace("{platform}", str(platform))
                   .replace("{arch}", str(mapped))
                   .replace("{version}", str(source.get("version", ""))))
            if "{arch}" not in template and len(matrix) > 1:
                # One URL cannot answer for several architectures, and picking
                # the first would ship one build labelled as the others.
                continue

            # A `pica` artifact has no per-architecture layout to put a build
            # in: `root/` is deployed as it stands, so `<platform>/<arch>/`
            # would reach the device as a directory at the root of the
            # filesystem. The matrix selects which archive to fetch, not where
            # it lands.
            subdir = str(source.get("dest_subdir", "")).strip("/")
            dest = os.path.join(root_dir, subdir) if subdir else root_dir

            os.makedirs(dest, exist_ok=True)
            archive = os.path.join(dest, os.path.basename(url.split("?")[0]))

            print(f"Fetching {url}...")
            if not download_file(url, archive):
                print(f"warning: could not fetch {url}", file=sys.stderr)
                ok = False
                continue

            if not unpack_into(dest, strip, includes, source.get("rename")):
                ok = False
                continue
            matched = True
            print(f"Unpacked into {os.path.relpath(dest, pkg_dir)}")
            break

        if not matched and ok:
            print(f"error: no archive matched {platform}/{arch}", file=sys.stderr)
            ok = False

    return ok


def unpack_into(dest, strip, includes, rename=None):
    """
    Unpack a downloaded archive in place, leaving the files under `dest`.

    A download that is not an archive is placed under `rename` instead, which
    is what a release of one bare binary needs: the archive's members keep the
    names they had inside it, but a single file's name is the upstream's, and
    the device wants it where the manifest says.

    The download is removed afterwards either way: it is an input to the
    build, and under `root/` it would otherwise be deployed to the device as a
    stray file.
    """
    archives = [n for n in os.listdir(dest) if n.endswith((".tar.gz", ".tgz", ".tar.xz"))]
    if not archives:
        # Not every upstream ships an archive. A release of a single static
        # binary is one file, and a package carrying it needs that file at a
        # path of its own choosing — the download's name says what it is
        # upstream, not where it belongs on the device.
        stray = [n for n in os.listdir(dest) if os.path.isfile(os.path.join(dest, n))]
        if not stray:
            print(f"error: nothing to unpack in {dest}", file=sys.stderr)
            return False
        target = rename or stray[0]
        destination = os.path.join(dest, target)
        if os.path.abspath(destination) != os.path.abspath(os.path.join(dest, stray[0])):
            os.rename(os.path.join(dest, stray[0]), destination)
        # Whatever it was called upstream, the device gets the name the
        # manifest promises, and an executable has to arrive executable.
        os.chmod(destination, 0o755)
        for name in os.listdir(dest):
            if name != target:
                try:
                    os.remove(os.path.join(dest, name))
                except OSError as err:
                    print(f"warning: could not remove {name}: {err}", file=sys.stderr)
        print(f"Placed {target}")
        return True

    for name in archives:
        path = os.path.join(dest, name)
        try:
            with tarfile.open(path, "r:*") as tar:
                members = []
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    parts = member.name.split("/")
                    if len(parts) <= strip:
                        continue
                    member.name = "/".join(parts[strip:])
                    if includes and not any(
                            member.name == pattern or member.name.startswith(pattern.rstrip("*"))
                            for pattern in includes):
                        continue
                    members.append(member)
                if not members:
                    print(f"error: {name} contains none of {includes}", file=sys.stderr)
                    return False

                if rename:
                    # An archive of one file names it the way the release does,
                    # which is not necessarily the name the device expects.
                    # Giving it the manifest's name is the same thing the
                    # non-archive path does, so a package can ask for it either
                    # way without knowing which it got.
                    if len(members) != 1:
                        print(
                            f"error: {name} holds {len(members)} files; rename applies to an "
                            "archive of one, use include to narrow it first",
                            file=sys.stderr,
                        )
                        return False
                    members[0].name = rename
                    tar.extractall(dest, members=members, filter="data")
                    os.chmod(os.path.join(dest, rename), 0o755)
                    # Fall through to the removal below: the archive is an
                    # input either way, and leaving it under root/ would ship
                    # it to the device.
                else:
                    tar.extractall(dest, members=members, filter="data")
        except (tarfile.TarError, OSError) as err:
            print(f"error: unpacking {name} failed: {err}", file=sys.stderr)
            return False

        # The archive is this package's build input, not part of it: left under
        # `root/` it would be deployed to the device as a stray megabyte.
        try:
            os.remove(path)
        except OSError as err:
            print(f"warning: could not remove {path}: {err}", file=sys.stderr)
    return True


# ---------------------------------------------------------------------------
# Preflight: plan every selected package, fetch all required assets, verify
# them, and only then let the workflow start pica-pack. The functions below
# never call pica-pack; the workflow's build step does that after this exits 0.

def classify_fetch_error(error):
    """Map a failed request onto the four states a check can report.

    A 404 is evidence the resource is gone. A 403 is not: on the GitHub API it
    is usually a rate limit, which says nothing about the resource and must not
    be reported as missing. Timeouts and connection errors are in the same
    category. Anything else is a state this check cannot judge.
    """
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 404:
            return "missing"
        if error.code in (403, 408, 429, 500, 502, 503, 504):
            return "unreachable"
        return "unverified"
    if isinstance(error, (urllib.error.URLError, TimeoutError, OSError)):
        return "unreachable"
    return "unverified"


def describe_fetch_error(error, status):
    """Say what happened, and say it without claiming more than was observed."""
    if status == "unreachable":
        return f"could not reach upstream: {error} (unreachable, not missing)"
    if status == "unverified":
        return f"could not verify upstream: {error}"
    return str(error)


def preflight_error(pkg, mgr, phase, reason, platform="", arch="", asset="",
                    blocks=True, status="invalid"):
    return {
        "pkg": pkg,
        "mgr": mgr,
        "platform": platform or "",
        "arch": arch or "",
        "asset": asset or "",
        "phase": phase,
        "reason": reason,
        "blocks": blocks,
        "status": status,
    }


def stage_package(src_dir, stage_dir):
    """Copy the package's static inputs into a clean staging directory."""
    if os.path.isdir(stage_dir):
        shutil.rmtree(stage_dir)
    os.makedirs(stage_dir)
    manifest = os.path.join(src_dir, "manifest")
    if not os.path.isfile(manifest):
        return False, f"missing manifest in {src_dir}"
    shutil.copy2(manifest, os.path.join(stage_dir, "manifest"))
    for name in ("scripts", "root"):
        source = os.path.join(src_dir, name)
        if os.path.isdir(source):
            shutil.copytree(source, os.path.join(stage_dir, name), symlinks=True)
    return True, ""


def static_errors(pkg, mgr, build_json_path, build_config):
    """Configuration checks that need no network."""
    errors = []

    def add(reason, platform="", arch="", asset=""):
        errors.append(preflight_error(pkg, mgr, "static", reason, platform, arch, asset))

    layout = job_layout_errors(build_json_path)
    if layout is None:
        add("build.json is not valid JSON")
        return errors
    for problem in layout:
        add(problem)

    schema_path = os.path.join("schema", "ci-job-v1.schema.json")
    schema_result = try_jsonschema(schema_path, build_json_path) if os.path.isfile(schema_path) else None
    if schema_result is None:
        for problem in check_job_fallback(build_json_path):
            add(problem)
    else:
        for problem in schema_result:
            add(problem)
        # The JSON Schema sees build.json only; it cannot read the manifest, so
        # the backend/targets agreement is checked here even when the schema is
        # available.
        manifest_path = build_config.get("manifest") or os.path.join("src", mgr, pkg, "manifest")
        if not os.path.isfile(manifest_path):
            add(f"manifest not found: {manifest_path}")
        else:
            targets = []
            declares_pkgmgr = False
            with open(manifest_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line.startswith("pkgmgr"):
                        declares_pkgmgr = True
                    if line.startswith("targets"):
                        _, _, value = line.partition("=")
                        targets.extend(part.strip() for part in value.split(",") if part.strip())
            if declares_pkgmgr:
                add("manifest declares pkgmgr; the packer writes it. Use targets instead")
            if not targets:
                add("manifest has no targets")
            elif mgr not in targets:
                add(f"manifest targets {targets} do not include this job's mgr {mgr}")

    matrix = build_config.get("build", {}).get("matrix", [])
    if not matrix:
        add("build.matrix must have at least one entry")
    seen = set()
    platforms = {}
    for entry in matrix:
        platform = entry.get("platform", "all")
        arch = entry.get("arch")
        if not arch:
            add("build.matrix entry is missing arch")
            continue
        if (platform, arch) in seen:
            add(f"build.matrix has a duplicate target {platform}/{arch}", platform, arch)
        seen.add((platform, arch))
        if arch in platforms and platforms[arch] != platform:
            add(f"build.matrix maps {arch} to both {platforms[arch]} and {platform}", platform, arch)
        platforms[arch] = platform

    source = build_config.get("source", {})
    provider = source.get("provider")
    matrix_arches = {entry.get("arch") for entry in matrix if entry.get("arch")}

    if provider == "github":
        release_cfg = source.get("release", {})
        tag_regex = (release_cfg.get("tag") or {}).get("regex")
        if tag_regex:
            try:
                re.compile(tag_regex)
            except re.error as err:
                add(f"release tag regex does not compile: {err}")
        assets = release_cfg.get("assets", [])
        if not assets:
            add("source.release.assets must not be empty")
        for asset in assets:
            name = asset.get("name", "")
            regex = asset.get("regex")
            if not regex:
                add(f"asset {name!r} is missing a regex", asset=name)
                continue
            try:
                compiled = re.compile(regex)
            except re.error as err:
                add(f"asset {name!r} regex does not compile: {err}", asset=name)
                continue
            inner = asset.get("inner_regex")
            if inner:
                try:
                    re.compile(inner)
                except re.error as err:
                    add(f"asset {name!r} inner_regex does not compile: {err}", asset=name)
            capture = asset.get("capture_to") or {}
            for key, group in capture.items():
                if key not in ("arch", "platform"):
                    add(f"asset {name!r} capture_to has unknown key {key!r}", asset=name)
                try:
                    index = int(group)
                except (TypeError, ValueError):
                    add(f"asset {name!r} capture_to.{key} is not an integer", asset=name)
                    continue
                if index < 1 or index > compiled.groups:
                    add(f"asset {name!r} capture_to.{key}={group} has no capture group", asset=name)
            target = asset.get("target") or {}
            if capture.get("arch") and target.get("platform"):
                add(
                    f"asset {name!r} captures arch but fixes platform {target['platform']!r}; "
                    "the platform would not follow the captured arch",
                    asset=name,
                )
            if len(matrix_arches) > 1 and target and target.get("arch") not in (None, "", "all") and not capture:
                add(
                    f"asset {name!r} fixes arch {target['arch']!r} while build.matrix has several arches; "
                    "use capture_to",
                    asset=name,
                )
        for upstream, mapped in (source.get("arch_map") or {}).items():
            if mapped not in matrix_arches:
                add(
                    f"github arch_map maps {upstream!r} to {mapped!r}, which is not in build.matrix"
                )
    elif provider == "http_template":
        arch_templates = source.get("arch_url_templates") or {}
        for arch in arch_templates:
            if arch not in matrix_arches:
                add(f"arch_url_templates names {arch!r}, which is not in build.matrix")
        if not http_template_urls(source) and not arch_templates:
            add("http_template source has no URL templates")
        for entry in matrix:
            arch = entry.get("arch")
            if arch and not http_template_urls_for_arch(source, arch):
                add(f"no URL template for matrix arch {arch!r}", arch=arch)
    elif provider == "tarball":
        templates = source.get("url_templates") or (
            [source["url_template"]] if source.get("url_template") else []
        )
        if not templates:
            add("tarball source has no URL templates")
        if len(matrix) > 1:
            for template in templates:
                if "{arch}" not in template:
                    add(f"tarball URL {template!r} has no {{arch}} but build.matrix has several arches")
            add(
                "tarball provider stages every matrix arch into one root/; pica-pack builds one "
                "artifact per package, so per-arch archives cannot be represented"
            )
    return errors


def select_github_release(releases, release_cfg):
    tag_regex = (release_cfg.get("tag") or {}).get("regex")
    allow_prerelease = release_cfg.get("allow_prerelease", False)
    for release in releases:
        if release.get("prerelease") and not allow_prerelease:
            continue
        if tag_regex and not re.match(tag_regex, release.get("tag_name", "")):
            continue
        return release
    return None


def plan_github_assets(release, build_config, stage, pkg):
    source = build_config.get("source", {})
    mgr = build_config.get("mgr", "opkg")
    matrix = build_config.get("build", {}).get("matrix", [])
    matrix_arches = [entry.get("arch") for entry in matrix if entry.get("arch")]
    platform_by_arch = {
        entry.get("arch"): entry.get("platform", "all") for entry in matrix if entry.get("arch")
    }
    requests = []
    errors = []
    optional = []
    skipped = []

    for asset_meta in source.get("release", {}).get("assets", []):
        name = asset_meta.get("name", "")
        regex = asset_meta.get("regex", "")
        capture = asset_meta.get("capture_to") or {}
        target = asset_meta.get("target") or {}
        required = asset_meta.get("required", True)
        inner_regex = asset_meta.get("inner_regex")
        found_any = False
        found_arches = set()

        for asset in release.get("assets", []):
            asset_name = asset.get("name", "")
            match = re.match(regex, asset_name)
            if not match:
                continue

            platform = target.get("platform")
            arch = target.get("arch")
            if "arch" in capture:
                arch = match.group(int(capture["arch"]))
                arch = (source.get("arch_map") or {}).get(arch, arch)
            if "platform" in capture:
                platform = match.group(int(capture["platform"]))

            if arch and arch != "all":
                if arch not in platform_by_arch:
                    skipped.append(
                        {"pkg": pkg, "mgr": mgr, "asset": asset_name, "arch": arch,
                         "reason": "arch is not in build.matrix"}
                    )
                    continue
                if not platform:
                    platform = platform_by_arch[arch]
                elif platform != platform_by_arch.get(arch):
                    errors.append(
                        preflight_error(
                            pkg, mgr, "plan",
                            f"captured platform {platform!r} does not match build.matrix for {arch!r}",
                            platform, arch, asset_name,
                        )
                    )
                    continue

            platform = platform or "all"
            arch = arch or "all"

            # An arch-independent asset belongs inside every package the matrix
            # produces, not in a package of its own. A container that says `all`
            # is code every architecture runs: written once to `all/all` it
            # becomes a second package holding the interface and no binary, next
            # to one package per architecture holding the binary and no
            # interface. Both then carry the same name, version and manifest, so
            # an install picks one of them and is missing half the app.
            if arch == "all" and matrix_arches and "all" not in matrix_arches:
                destinations = [
                    (platform_by_arch[matrix_arch], matrix_arch) for matrix_arch in matrix_arches
                ]
            else:
                destinations = [(platform, arch)]

            digest = (asset.get("digest") or "").removeprefix("sha256:")
            for dest_platform, dest_arch in destinations:
                if inner_regex and asset_name.endswith(".zip"):
                    kind = "zip"
                    # A zip request may yield several IPKs, so its destination is
                    # the architecture's package directory, not one guessed name.
                    dest = os.path.join(stage, "pkgs", mgr, dest_platform, dest_arch)
                else:
                    kind = "container"
                    dest = os.path.join(
                        stage, "pkgs", mgr, dest_platform, dest_arch, asset_name
                    )
                requests.append(
                    {
                        "pkg": pkg, "mgr": mgr, "platform": dest_platform, "arch": dest_arch,
                        "url": asset.get("browser_download_url", ""), "dest": dest,
                        "kind": kind, "inner_regex": inner_regex, "asset": asset_name,
                        "role": asset_meta.get("role") or infer_role(asset_name),
                        "required": required, "expected_size": asset.get("size"),
                        "expected_sha256": digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None,
                    }
                )
                if dest_arch != "all":
                    found_arches.add(dest_arch)
            found_any = True

        if required:
            if "arch" in capture:
                for arch in matrix_arches:
                    if arch not in found_arches:
                        errors.append(
                            preflight_error(
                                pkg, mgr, "plan",
                                f"required asset {regex!r} has no match for matrix arch {arch!r}",
                                platform_by_arch.get(arch, ""), arch, name,
                            )
                        )
            elif not found_any:
                errors.append(
                    preflight_error(pkg, mgr, "plan", f"required asset {regex!r} was not found", asset=name)
                )
        elif "arch" in capture:
            for arch in matrix_arches:
                if arch not in found_arches:
                    optional.append(
                        {"pkg": pkg, "mgr": mgr, "asset": name, "arch": arch,
                         "reason": "optional asset not found for this arch"}
                    )
        elif not found_any:
            optional.append(
                {"pkg": pkg, "mgr": mgr, "asset": name, "reason": "optional asset not found"}
            )

    return requests, errors, optional, skipped


def plan_github_package(source, build_config, stage, pkg, offline=False):
    repo = source.get("repo")
    mgr = build_config.get("mgr", "opkg")
    if not repo:
        return [], [preflight_error(pkg, mgr, "plan", "source.repo is missing")], [], []
    if offline:
        # A release list needs the network. Offline planning still validates
        # everything local — static_errors has already checked the asset
        # regexes, their capture groups and their targets by the time this runs
        # — and records plainly that the listing itself was not consulted, so
        # an offline run is never mistaken for a verified asset list.
        return [], [
            preflight_error(
                pkg, mgr, "release",
                "offline: the release asset list was not consulted",
                blocks=False, status="unverified",
            )
        ], [], []
    api_url = f"https://api.github.com/repos/{repo}/releases"
    try:
        releases = fetch_json(api_url)
    except Exception as err:
        status = classify_fetch_error(err)
        # An unreachable upstream is not a verdict on the resource. It still
        # blocks, because nothing can be built from an asset list nobody read,
        # but it is reported as unreachable so an operator does not go looking
        # for a deleted release that is still there.
        return [], [
            preflight_error(
                pkg, mgr, "release",
                f"could not list releases: {describe_fetch_error(err, status)}",
                status=status,
            )
        ], [], []
    release = select_github_release(releases, source.get("release", {}))
    if release is None:
        return [], [
            preflight_error(pkg, mgr, "release", "no matching release", status="missing")
        ], [], []
    print(f"Matched GitHub release: {release.get('tag_name')}")
    return plan_github_assets(release, build_config, stage, pkg)


def plan_http_assets(build_config, stage, pkg):
    source = build_config.get("source", {})
    mgr = build_config.get("mgr", "opkg")
    matrix = build_config.get("build", {}).get("matrix", [])
    version = source.get("version", "latest")
    requests = []
    errors = []

    for entry in matrix:
        platform = entry.get("platform", "all")
        arch = entry.get("arch")
        if not arch:
            continue
        templates = http_template_urls_for_arch(source, arch)
        if not templates:
            errors.append(
                preflight_error(
                    pkg, mgr, "plan", f"no URL template for matrix arch {arch!r}", platform, arch
                )
            )
            continue
        for template in templates:
            url = (
                template.replace("{arch}", arch)
                .replace("{version}", version)
                .replace("{platform}", platform)
            )
            filename = url.split("?")[0].rsplit("/", 1)[-1]
            if not filename:
                errors.append(
                    preflight_error(pkg, mgr, "plan", "URL has no filename", platform, arch, url)
                )
                continue
            dest = os.path.join(stage, "pkgs", mgr, platform, arch, filename)
            requests.append(
                {
                    "pkg": pkg, "mgr": mgr, "platform": platform, "arch": arch,
                    "url": url, "dest": dest, "kind": "container", "asset": filename,
                    "role": infer_role(filename), "required": True,
                    "expected_size": None, "expected_sha256": None,
                }
            )
    return requests, errors, [], []


def plan_tarball_assets(build_config, stage, pkg):
    source = build_config.get("source", {})
    mgr = build_config.get("mgr", "opkg")
    matrix = build_config.get("build", {}).get("matrix", [])
    templates = source.get("url_templates") or (
        [source["url_template"]] if source.get("url_template") else []
    )
    requests = []
    errors = []

    if len(matrix) > 1:
        errors.append(
            preflight_error(
                pkg, mgr, "plan",
                "tarball provider cannot represent several matrix arches in one root/",
            )
        )
        return requests, errors, [], []

    for entry in matrix:
        platform = entry.get("platform", "all")
        arch = entry.get("arch", "all")
        chosen = next((template for template in templates if "{arch}" in template), None)
        if chosen is None and len(matrix) == 1 and templates:
            chosen = templates[0]
        if chosen is None:
            errors.append(
                preflight_error(pkg, mgr, "plan", "no tarball URL for this matrix arch", platform, arch)
            )
            continue
        mapped = (source.get("arch_map") or {}).get(arch, arch)
        url = (
            chosen.replace("{platform}", str(platform))
            .replace("{arch}", str(mapped))
            .replace("{version}", str(source.get("version", "")))
        )
        requests.append(
            {
                "pkg": pkg, "mgr": mgr, "platform": platform, "arch": arch,
                "url": url, "dest": stage, "kind": "tarball",
                "asset": url.rsplit("/", 1)[-1], "role": "core",
                "required": True, "expected_size": None, "expected_sha256": None,
                "extract": {
                    "strip": source.get("strip_components", 0),
                    "includes": source.get("include") or [],
                    "rename": source.get("rename"),
                    "dest_subdir": str(source.get("dest_subdir", "")).strip("/"),
                },
            }
        )
    return requests, errors, [], []


def dedupe_requests(requests, pkg, mgr):
    errors = []
    seen = {}
    result = []
    for request in requests:
        # A zip expands into a directory, so two zips for one arch are not a
        # path collision by themselves; their extracted files are checked.
        dest = request["dest"]
        key = (dest, request["asset"]) if request.get("kind") == "zip" else dest
        if key in seen:
            if seen[key] != request["url"]:
                errors.append(
                    preflight_error(
                        pkg, mgr, "plan",
                        f"two resources write the same path: {seen[key]} and {request['url']}",
                        request["platform"], request["arch"], request["asset"],
                    )
                )
            continue
        seen[key] = request["url"]
        result.append(request)
    return result, errors


def plan_package(src_dir, stage_root, arch_filter=None, offline=False):
    parts = os.path.normpath(src_dir).split(os.sep)
    if len(parts) < 3 or parts[-3] != "src":
        return {
            "source": src_dir, "stage": "", "mgr": "", "pkg": src_dir,
            "requests": [], "errors": [
                preflight_error(src_dir, "", "static", "package dir must be src/<mgr>/<pkg>")
            ],
            "optional": [], "skipped": [], "build_config": None,
        }
    mgr, pkg = parts[-2], parts[-1]
    stage = os.path.join(stage_root, mgr, pkg)
    plan = {
        "source": src_dir, "stage": stage, "mgr": mgr, "pkg": pkg,
        "requests": [], "errors": [], "optional": [], "skipped": [], "build_config": None,
    }

    build_json = os.path.join("ci", mgr, pkg, "build.json")
    if not os.path.isfile(build_json):
        plan["errors"].append(preflight_error(pkg, mgr, "static", f"missing {build_json}"))
        return plan
    try:
        with open(build_json, "r", encoding="utf-8") as handle:
            build_config = json.load(handle)
    except (OSError, json.JSONDecodeError) as err:
        plan["errors"].append(preflight_error(pkg, mgr, "static", f"cannot read {build_json}: {err}"))
        return plan
    plan["build_config"] = build_config

    plan["errors"].extend(static_errors(pkg, mgr, build_json, build_config))
    if arch_filter:
        matrix = build_config.get("build", {}).get("matrix", [])
        if not any(entry.get("arch") == arch_filter for entry in matrix):
            plan["errors"].append(
                preflight_error(pkg, mgr, "static", f"arch {arch_filter!r} is not in build.matrix")
            )
            return plan
        build_config.setdefault("build", {})["matrix"] = [
            entry for entry in matrix if entry.get("arch") == arch_filter
        ]
        if mgr == "pica":
            # A pica artifact takes platform/arch from its manifest; there is
            # no per-arch staging yet. Refuse an arch the manifest cannot name
            # rather than building a mislabelled artifact.
            manifest_path = os.path.join(src_dir, "manifest")
            values = {}
            if os.path.isfile(manifest_path):
                with open(manifest_path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        stripped = line.strip()
                        if stripped.startswith(("platform =", "arch =")):
                            key, _, value = stripped.partition("=")
                            values[key.strip()] = value.strip()
            selected = build_config["build"]["matrix"][0]
            if values.get("platform") != selected.get("platform") or values.get("arch") != selected.get("arch"):
                plan["errors"].append(
                    preflight_error(
                        pkg, mgr, "static",
                        "arch filtering for pica requires per-arch manifests, which this job does not stage",
                        selected.get("platform", ""), arch_filter,
                    )
                )
                return plan
    if plan["errors"]:
        return plan

    ok, error = stage_package(src_dir, stage)
    if not ok:
        plan["errors"].append(preflight_error(pkg, mgr, "stage", error))
        return plan

    provider = build_config.get("source", {}).get("provider")
    if provider == "github":
        requests, errors, optional, skipped = plan_github_package(
            build_config.get("source", {}), build_config, stage, pkg, offline=offline
        )
    elif provider == "http_template":
        requests, errors, optional, skipped = plan_http_assets(build_config, stage, pkg)
    elif provider == "tarball":
        requests, errors, optional, skipped = plan_tarball_assets(build_config, stage, pkg)
    else:
        requests, errors, optional, skipped = [], [
            preflight_error(pkg, mgr, "plan", f"unknown provider {provider!r}")
        ], [], []

    requests, dedupe_errors = dedupe_requests(requests, pkg, mgr)
    plan["requests"] = requests
    plan["errors"].extend(errors)
    plan["errors"].extend(dedupe_errors)
    if not any(request.get("required", True) for request in requests):
        # In an offline run a GitHub package has no requests by design — the
        # asset list was never read — so saying no required resource was
        # planned would report the offline decision as a configuration fault.
        #
        # The same applies after an unreachable upstream: an empty request list
        # is then a consequence of the failed listing, not a second fault, and
        # reporting both would bury the real cause under a message that reads
        # like a bad manifest.
        offline_github = offline and provider == "github"
        already_explained = any(
            error.get("status") in ("unreachable", "unverified") for error in plan["errors"]
        )
        if not offline_github and not already_explained:
            plan["errors"].append(
                preflight_error(pkg, mgr, "plan", "no required resource was planned")
            )
    for request in requests:
        if re.search(r"\{[^}]+\}", request.get("url", "")):
            plan["errors"].append(
                preflight_error(
                    pkg, mgr, "plan",
                    f"URL still contains an unresolved template variable: {request['url']}",
                    request.get("platform", ""), request.get("arch", ""), request.get("asset", ""),
                )
            )
    plan["optional"].extend(optional)
    plan["skipped"].extend(skipped)
    return plan


def unpack_tarball_request(download_path, request, stage):
    """Unpack one tarball request into the stage's root/ and return written files."""
    extract = request.get("extract") or {}
    work = tempfile.mkdtemp(prefix="pica-unpack-", dir=os.path.join(stage, ".pica-downloads"))
    download = os.path.join(work, os.path.basename(download_path))
    os.makedirs(os.path.dirname(download), exist_ok=True)
    shutil.move(download_path, download)
    try:
        if not unpack_into(
            work, extract.get("strip", 0), extract.get("includes") or [], extract.get("rename")
        ):
            shutil.rmtree(work, ignore_errors=True)
            return None
        dest_root = os.path.join(stage, "root", (extract.get("dest_subdir") or "").strip("/"))
        os.makedirs(dest_root, exist_ok=True)
        written = []
        for root, _, files in os.walk(work):
            for filename in files:
                path = os.path.join(root, filename)
                relative = os.path.relpath(path, work)
                target = os.path.join(dest_root, relative)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(path, target)
                written.append(target)
        return written
    finally:
        shutil.rmtree(work, ignore_errors=True)


def execute_requests(plan, cache_dir=None):
    """Download every request into the stage, returning (errors, optional).

    A URL is fetched at most once per plan. Some jobs legitimately point
    several architectures at one noarch file; downloading it again for each
    architecture only adds requests and bytes.
    """
    errors = []
    optional = []
    written = {}
    url_cache = {}
    stage = plan["stage"]
    temp_root = os.path.join(stage, ".pica-downloads")
    os.makedirs(temp_root, exist_ok=True)

    for index, request in enumerate(plan["requests"]):
        filename = os.path.basename(request["url"].split("?")[0]) or f"asset-{index}"
        temp = os.path.join(temp_root, f"{index}-{filename}")

        cached = url_cache.get(request["url"])
        if cached and request["kind"] != "tarball":
            if cached["kind"] == "container":
                dest = request["dest"]
                if os.path.exists(dest):
                    errors.append(
                        preflight_error(
                            request["pkg"], request["mgr"], "stage",
                            f"resource collision at {dest}",
                            request["platform"], request["arch"], request["asset"],
                        )
                    )
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(cached["path"], dest)
                request["ok"] = True
                request["path"] = dest
                request["reused_url"] = True
                written[dest] = request["url"]
                continue
            if cached["kind"] == "zip":
                dest = request["dest"]
                os.makedirs(dest, exist_ok=True)
                copied = []
                for path in cached["files"]:
                    target = os.path.join(dest, os.path.basename(path))
                    shutil.copyfile(path, target)
                    copied.append(target)
                    written[target] = request["url"]
                request["ok"] = True
                request["files"] = copied
                request["reused_url"] = True
                continue

        ok, size, error = download_to(
            request["url"], temp,
            request.get("expected_size"),
            request.get("expected_sha256"),
            cache_dir,
        )
        if not ok:
            entry = preflight_error(
                request["pkg"], request["mgr"], "download", error,
                request["platform"], request["arch"], request["asset"],
            )
            (errors if request["required"] else optional).append(entry)
            continue
        if size <= 0:
            entry = preflight_error(
                request["pkg"], request["mgr"], "download", "empty download",
                request["platform"], request["arch"], request["asset"],
            )
            (errors if request["required"] else optional).append(entry)
            continue

        try:
            if request["kind"] == "container":
                dest = request["dest"]
                if os.path.exists(dest):
                    errors.append(
                        preflight_error(
                            request["pkg"], request["mgr"], "stage",
                            f"resource collision at {dest}",
                            request["platform"], request["arch"], request["asset"],
                        )
                    )
                    os.remove(temp)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(temp, dest)
                request["ok"] = True
                request["path"] = dest
                written[dest] = request["url"]
                url_cache[request["url"]] = {"kind": "container", "path": dest}
            elif request["kind"] == "zip":
                dest = request["dest"]
                files = extract_all_from_zip(temp, request.get("inner_regex", ""), dest)
                os.remove(temp)
                if not files:
                    errors.append(
                        preflight_error(
                            request["pkg"], request["mgr"], "stage",
                            "no inner file matched inner_regex",
                            request["platform"], request["arch"], request["asset"],
                        )
                    )
                    continue
                for path in files:
                    if os.path.getsize(path) <= 0:
                        errors.append(
                            preflight_error(
                                request["pkg"], request["mgr"], "verify",
                                f"extracted file is empty: {os.path.basename(path)}",
                                request["platform"], request["arch"], request["asset"],
                            )
                        )
                    previous = written.get(path)
                    if previous is not None and previous != request["url"]:
                        errors.append(
                            preflight_error(
                                request["pkg"], request["mgr"], "stage",
                                f"two resources wrote {path}",
                                request["platform"], request["arch"], request["asset"],
                            )
                        )
                    written[path] = request["url"]
                request["ok"] = True
                request["files"] = files
                url_cache[request["url"]] = {"kind": "zip", "files": files}
            elif request["kind"] == "tarball":
                files = unpack_tarball_request(temp, request, stage)
                if files is None:
                    errors.append(
                        preflight_error(
                            request["pkg"], request["mgr"], "stage",
                            "tarball could not be unpacked",
                            request["platform"], request["arch"], request["asset"],
                        )
                    )
                    continue
                for path in files:
                    previous = written.get(path)
                    if previous is not None and previous != request["url"]:
                        errors.append(
                            preflight_error(
                                request["pkg"], request["mgr"], "stage",
                                f"two resources wrote {path}",
                                request["platform"], request["arch"], request["asset"],
                            )
                        )
                    written[path] = request["url"]
                request["ok"] = True
                request["files"] = files
            else:
                errors.append(
                    preflight_error(
                        request["pkg"], request["mgr"], "stage",
                        f"unknown request kind {request['kind']!r}",
                        request["platform"], request["arch"], request["asset"],
                    )
                )
                os.remove(temp)
        except OSError as err:
            errors.append(
                preflight_error(
                    request["pkg"], request["mgr"], "stage", f"staging failed: {err}",
                    request["platform"], request["arch"], request["asset"],
                )
            )
            try:
                os.remove(temp)
            except OSError:
                pass

    shutil.rmtree(temp_root, ignore_errors=True)
    plan["written"] = written
    return errors, optional


def verify_plan(plan):
    """Check downloaded containers against the manifest and their target arch."""
    errors = []
    unverified = []
    verified = 0
    stage = plan["stage"]
    manifest = os.path.join(stage, "manifest")
    declared = manifest_dependencies(manifest)
    has_declarations = any(declared.values())
    manifest_version = manifest_pkgver(manifest)
    versions_seen = []
    actual_names = set()

    def verify_one(request, path):
        if not path or not os.path.isfile(path):
            return 0
        if os.path.getsize(path) <= 0:
            errors.append(
                preflight_error(
                    request["pkg"], request["mgr"], "verify", "downloaded file is empty",
                    request["platform"], request["arch"], request["asset"],
                )
            )
            return 0
        metadata = container_metadata(path)
        if metadata is None:
            unverified.append(
                {
                    "pkg": request["pkg"], "mgr": request["mgr"],
                    "platform": request["platform"], "arch": request["arch"],
                    "asset": request["asset"], "path": path,
                    "reason": "container metadata is unreadable (ADB or unsupported format)",
                }
            )
            return 0
        package = metadata.get("package")
        version = metadata.get("version")
        architecture = metadata.get("architecture")
        if package:
            actual_names.add(package)
        if version:
            versions_seen.append(version)
        if package and has_declarations and not declares_package(declared, package):
            errors.append(
                preflight_error(
                    request["pkg"], request["mgr"], "verify",
                    f"container installs as {package!r}, which the manifest does not declare",
                    request["platform"], request["arch"], request["asset"],
                )
            )
        if architecture is None:
            unverified.append(
                {
                    "pkg": request["pkg"], "mgr": request["mgr"],
                    "platform": request["platform"], "arch": request["arch"],
                    "asset": request["asset"], "path": path,
                    "reason": "container declares no Architecture",
                }
            )
            return 0
        if request["arch"] != "all" and architecture not in ("all", request["arch"]):
            errors.append(
                preflight_error(
                    request["pkg"], request["mgr"], "verify",
                    f"container architecture is {architecture!r}, expected {request['arch']!r} or all",
                    request["platform"], request["arch"], request["asset"],
                )
            )
            return 0
        return 1

    for request in plan["requests"]:
        if not request.get("ok"):
            continue
        if request["kind"] == "tarball":
            verified += 1
            continue
        if request["kind"] == "zip":
            for path in request.get("files") or []:
                verified += verify_one(request, path)
            continue
        verified += verify_one(request, request.get("path"))

    # A container can carry more than the package the manifest names (a LuCI
    # bundle also ships the app and translations), so one match is enough. A
    # manifest that matches none of them is advertising a version the package
    # does not contain.
    if manifest_version and versions_seen and not any(
        version_matches(manifest_version, version) for version in versions_seen
    ):
        first = plan["requests"][0] if plan["requests"] else {}
        errors.append(
            preflight_error(
                plan["pkg"], plan["mgr"], "verify",
                f"manifest advertises {manifest_version!r}, but no downloaded container carries it "
                f"(saw {', '.join(sorted(set(versions_seen)))})",
                first.get("platform", ""), first.get("arch", ""),
            )
        )

    # When metadata is readable, every LuCI/i18n component the manifest
    # advertises must actually be present. ADB containers are unreadable, so
    # this coverage check stays silent rather than guessing from filenames.
    if actual_names:
        first = plan["requests"][0] if plan["requests"] else {}
        for app in declared.get("app", ()):
            if app.startswith("luci-app-") and app not in actual_names:
                errors.append(
                    preflight_error(
                        plan["pkg"], plan["mgr"], "verify",
                        f"manifest declares {app!r}, but it was not extracted from the downloaded assets",
                        first.get("platform", ""), first.get("arch", ""),
                    )
                )
        for template in declared.get("app_i18n", ()):
            pattern = "^" + re.escape(template).replace(re.escape("{lang}"), ".+") + "$"
            if not any(re.match(pattern, name) for name in actual_names):
                errors.append(
                    preflight_error(
                        plan["pkg"], plan["mgr"], "verify",
                        f"manifest declares {template!r}, but it was not extracted from the downloaded assets",
                        first.get("platform", ""), first.get("arch", ""),
                    )
                )
    return errors, unverified, verified


def probe_url(url, expected_size=None, attempts=3):
    """Check a URL without downloading the resource.

    The result is deliberately four-valued. A 404 is `missing`; a timeout or
    5xx is `unreachable`, not evidence that the asset does not exist; a server
    that refuses both HEAD and a one-byte range is `unverified`. A size that
    contradicts the release metadata is a definite mismatch and is reported as
    `missing` with that reason.
    """
    if not url.startswith(("http://", "https://")):
        return {"status": "unverified", "reason": "non-HTTP URL", "size": None}
    headers = {"User-Agent": "Mozilla/5.0 (Pica-CI-Resource-Probe)"}
    token = os.environ.get("GITHUB_TOKEN")
    if "github.com" in url and token:
        headers["Authorization"] = f"token {token}"

    def one_request(method, extra=None):
        request_headers = dict(headers)
        if extra:
            request_headers.update(extra)
        request = urllib.request.Request(url, headers=request_headers, method=method)
        with urllib.request.urlopen(request, timeout=20) as response:
            length = response.headers.get("Content-Length")
            response.close()
        return length

    last_reason = ""
    for attempt in range(attempts):
        try:
            length = one_request("HEAD")
            size = int(length) if length and length.isdigit() else None
            if expected_size is not None and size is not None and size != expected_size:
                return {
                    "status": "missing",
                    "reason": f"size changed: got {size}, expected {expected_size}",
                    "size": size,
                }
            return {"status": "valid", "reason": "", "size": size}
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                return {"status": "missing", "reason": f"HTTP {error.code}", "size": None}
            if error.code in (405, 501):
                try:
                    length = one_request("GET", {"Range": "bytes=0-0"})
                    size = None
                    return {"status": "valid", "reason": "HEAD unsupported; range request succeeded", "size": size}
                except urllib.error.HTTPError as range_error:
                    if range_error.code in (404, 410):
                        return {"status": "missing", "reason": f"HTTP {range_error.code}", "size": None}
                    last_reason = f"HTTP {range_error.code} on range request"
                except Exception as range_error:
                    last_reason = str(range_error)
                return {"status": "unverified", "reason": last_reason, "size": None}
            last_reason = f"HTTP {error.code}"
            if not _retryable(error) or attempt + 1 >= attempts:
                return {"status": "unreachable", "reason": last_reason, "size": None}
            _sleep_before_retry(attempt)
        except Exception as error:
            last_reason = str(error)
            if attempt + 1 >= attempts:
                return {"status": "unreachable", "reason": last_reason, "size": None}
            _sleep_before_retry(attempt)
    return {"status": "unreachable", "reason": last_reason, "size": None}


def probe_requests(requests):
    """Probe each unique URL once and attach the result to every request."""
    unique = {}
    for request in requests:
        url = request.get("url")
        if url and url not in unique:
            unique[url] = request
    results = {}
    if unique:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(probe_url, url, request.get("expected_size")): url
                for url, request in unique.items()
            }
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()
    errors = []
    warnings = []
    for request in requests:
        result = results.get(request.get("url"))
        if result is None:
            continue
        request["probe"] = result
        location = "/".join(part for part in (request.get("platform"), request.get("arch")) if part)
        if result["status"] == "missing" and request.get("required", True):
            errors.append(
                preflight_error(
                    request["pkg"], request["mgr"], "probe",
                    f"resource is missing ({result['reason']})",
                    request.get("platform", ""), request.get("arch", ""), request.get("asset", ""),
                )
            )
        elif result["status"] in ("unreachable", "unverified"):
            warnings.append(
                f"{request['pkg']}/{request['mgr']} {location} {request.get('asset', '')}: "
                f"{result['status']} ({result['reason']}); download will be attempted"
            )
    return errors, warnings


def plan_packages(package_dirs, stage_root, arch_filter=None, offline=False):
    """Build the resource plan for every package without downloading assets."""
    stage_root = os.path.abspath(stage_root)
    if os.path.isdir(stage_root):
        shutil.rmtree(stage_root)
    os.makedirs(stage_root, exist_ok=True)
    plans = []
    errors = []
    optional = []
    skipped = []
    for src_dir in package_dirs:
        plan = plan_package(src_dir, stage_root, arch_filter, offline=offline)
        print(f"Preflight {src_dir}: {plan['mgr']}/{plan['pkg']}")
        errors.extend(plan["errors"])
        optional.extend(plan["optional"])
        skipped.extend(plan["skipped"])
        if plan["errors"]:
            print(f"  blocked by {len(plan['errors'])} configuration/planning error(s)")
            continue
        plans.append(plan)
    return plans, errors, optional, skipped


def plan_all(package_dirs, stage_root, plan_output, probe=False, arch_filter=None,
             offline=False, allow_unreachable=False):
    """Write the resource plan and, optionally, probe every unique URL.

    `offline` keeps the whole run off the network. `allow_unreachable` reports
    an unreachable upstream without failing, which is what a scheduled check
    wants: a rate limit is not a reason to paint the repository red.
    """
    if not package_dirs:
        print("No packages selected; nothing to plan.")
        if plan_output:
            with open(plan_output, "w", encoding="utf-8") as handle:
                json.dump({"summary": {"packages": 0}, "packages": []}, handle, indent=2)
                handle.write("\n")
        return 0

    plans, errors, optional, skipped = plan_packages(
        package_dirs, stage_root, arch_filter, offline=offline
    )
    probe_warnings = []
    if probe and plans:
        requests = [request for plan in plans for request in plan["requests"]]
        probe_errors, probe_warnings = probe_requests(requests)
        errors.extend(probe_errors)

    stages = [
        {"source": plan["source"], "stage": plan["stage"], "mgr": plan["mgr"], "pkg": plan["pkg"]}
        for plan in plans
    ]
    blocking = [error for error in errors if error.get("blocks", True)]
    if allow_unreachable:
        blocking = [error for error in blocking if error.get("status") != "unreachable"]
        # Record the decision in the plan itself, so a reader of plan.json sees
        # what was actually enforced rather than what would have been by default.
        for error in errors:
            if error.get("status") == "unreachable":
                error["blocks"] = False
    by_status = {}
    for error in errors:
        key = error.get("status") or "invalid"
        by_status[key] = by_status.get(key, 0) + 1
    summary = {
        "checked": len(package_dirs),
        "packages_planned": len(plans),
        "requests": sum(len(plan["requests"]) for plan in plans),
        "optional_missing": len(optional),
        "validation_failures": len(errors),
        "packages_blocked": len({error["pkg"] for error in blocking}),
        "unreachable": by_status.get("unreachable", 0),
        "by_status": by_status,
        "offline": offline,
        "stages": stages,
        "probe": probe,
    }
    for error in errors:
        location = "/".join(part for part in (error.get("platform"), error.get("arch")) if part)
        print(
            f"error: [{error['pkg']}/{error['mgr']} {location} {error['phase']}] "
            f"{error['asset']}: {error['reason']}",
            file=sys.stderr,
        )
    for item in optional:
        print(f"warning: optional asset missing for {item['pkg']}: {item.get('asset', '')}", file=sys.stderr)
    for item in skipped:
        print(f"note: skipped {item['pkg']}/{item['mgr']} {item['asset']} ({item['reason']})")
    for warning in probe_warnings:
        print(f"warning: {warning}", file=sys.stderr)

    print("Plan summary:")
    print(f"- Packages planned: {summary['packages_planned']}/{summary['checked']}")
    print(f"- Resource requests: {summary['requests']}")
    print(f"- Optional assets missing: {summary['optional_missing']}")
    print(f"- Validation failures: {summary['validation_failures']}")
    print(f"- Packages blocked: {summary['packages_blocked']}")
    if by_status:
        print(f"- Failures by status: {', '.join(f'{key}={value}' for key, value in sorted(by_status.items()))}")
    if probe:
        statuses = {}
        for plan in plans:
            for request in plan["requests"]:
                status = (request.get("probe") or {}).get("status", "unprobed")
                statuses[status] = statuses.get(status, 0) + 1
        print(f"- Probe results: {', '.join(f'{key}={value}' for key, value in sorted(statuses.items())) or 'none'}")

    if plan_output:
        os.makedirs(os.path.dirname(os.path.abspath(plan_output)) or ".", exist_ok=True)
        with open(plan_output, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "summary": summary,
                    "packages": plans,
                    "errors": errors,
                    "optional_missing": optional,
                    "skipped": skipped,
                    "probe_warnings": probe_warnings,
                },
                handle,
                indent=2,
                ensure_ascii=False,
            )
            handle.write("\n")

    if blocking:
        if len(blocking) != len(errors):
            print(
                f"note: {len(errors) - len(blocking)} failure(s) reported but not "
                "blocking (unreachable upstream)"
            )
        print("FETCH PHASE NOT STARTED")
        return 1
    if errors:
        print(f"Plan complete with {len(errors)} non-blocking note(s).")
        return 0
    print(f"Plan complete; {len(plans)} package(s) ready to fetch.")
    return 0


def fetch_plan_file(plan_path, cache_dir=None):
    """Download and verify exactly the resources a prior plan named."""
    with open(plan_path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    plans = document.get("packages", [])
    errors = list(document.get("errors", []))
    optional = list(document.get("optional_missing", []))
    skipped = list(document.get("skipped", []))
    unverified = []
    stages = []
    required_passed = 0

    for plan in plans:
        if plan.get("errors"):
            errors.extend(plan["errors"])
            continue
        print(f"Fetching {plan['mgr']}/{plan['pkg']} from {plan['stage']}")
        download_errors, download_optional = execute_requests(plan, cache_dir)
        errors.extend(download_errors)
        optional.extend(download_optional)
        verify_errors, verify_unverified, verified = verify_plan(plan)
        errors.extend(verify_errors)
        unverified.extend(verify_unverified)
        if verify_errors and cache_dir:
            # The cached bytes were complete but not the resource this package
            # can use. Drop them so the next run does not repeat the failure.
            for request in plan["requests"]:
                _cache_remove(cache_dir, request.get("url", ""))
        required_passed += sum(
            1 for request in plan["requests"] if request.get("ok") and request.get("required", True)
        )
        if not download_errors and not verify_errors:
            stages.append(
                {"source": plan["source"], "stage": plan["stage"], "mgr": plan["mgr"], "pkg": plan["pkg"]}
            )
            print(f"  staged at {plan['stage']} ({verified} verified asset(s))")

    for error in errors:
        location = "/".join(part for part in (error.get("platform"), error.get("arch")) if part)
        print(
            f"error: [{error['pkg']}/{error['mgr']} {location} {error['phase']}] "
            f"{error['asset']}: {error['reason']}",
            file=sys.stderr,
        )
    for item in optional:
        print(f"warning: optional asset missing for {item['pkg']}: {item.get('asset', '')}", file=sys.stderr)
    for item in skipped:
        print(f"note: skipped {item['pkg']}/{item['mgr']} {item['asset']} ({item['reason']})")
    for item in unverified:
        print(
            f"warning: {item['pkg']}/{item['mgr']} {item['asset']}: {item['reason']}; not verified",
            file=sys.stderr,
        )

    print("Fetch summary:")
    print(f"- Packages staged: {len(stages)}/{len(plans)}")
    print(f"- Required assets passed: {required_passed}")
    print(f"- Optional assets missing: {len(optional)}")
    print(f"- Validation failures: {len(errors)}")
    print(f"- Packages blocked: {len({error['pkg'] for error in errors if error.get('blocks', True)})}")
    print(f"- Assets unverified: {len(unverified)}")

    if errors:
        print("PACKAGE PHASE NOT STARTED")
        return 1
    print(f"Fetch complete; {len(stages)} package(s) staged.")
    return 0


def preflight(package_dirs, stage_root, plan_output=None, arch_filter=None, cache_dir=None):
    """Run the combined legacy preflight; never calls pica-pack.

    The workflow uses `--plan` followed by `--fetch-plan` so the no-download
    plan is a separate, enforced stage. This combined form is kept for local
    runs and the integration tests.
    """
    stage_root = os.path.abspath(stage_root)
    if not package_dirs:
        print("Preflight summary:")
        print("- Packages checked: 0")
        print("- Required assets passed: 0")
        print("- Optional assets missing: 0")
        print("- Validation failures: 0")
        print("- Packages blocked: 0")
        print("No packages selected; nothing to preflight.")
        return 0

    plans, errors, optional, skipped = plan_packages(package_dirs, stage_root, arch_filter)
    checked = len(package_dirs)
    required_passed = 0
    unverified = []
    stages = []

    for plan in plans:
        download_errors, download_optional = execute_requests(plan, cache_dir)
        errors.extend(download_errors)
        optional.extend(download_optional)
        verify_errors, verify_unverified, verified = verify_plan(plan)
        errors.extend(verify_errors)
        unverified.extend(verify_unverified)
        required_passed += sum(
            1 for request in plan["requests"] if request.get("ok") and request["required"]
        )
        if not download_errors and not verify_errors:
            stages.append({"source": plan["source"], "stage": plan["stage"], "mgr": plan["mgr"], "pkg": plan["pkg"]})
            print(f"  staged at {plan['stage']} ({verified} verified asset(s))")
        else:
            print(f"  blocked by {len(download_errors) + len(verify_errors)} resource/verification error(s)")

    blocked = {error["pkg"] for error in errors if error.get("blocks", True)}
    summary = {
        "checked": checked,
        "required_passed": required_passed,
        "optional_missing": len(optional),
        "validation_failures": len(errors),
        "packages_blocked": len(blocked),
        "unverified": len(unverified),
        "stages": stages,
    }

    for error in errors:
        location = "/".join(part for part in (error["platform"], error["arch"]) if part)
        print(
            f"error: [{error['pkg']}/{error['mgr']} {location} {error['phase']}] "
            f"{error['asset']}: {error['reason']}",
            file=sys.stderr,
        )
    for item in optional:
        print(f"warning: optional asset missing for {item['pkg']}: {item.get('asset', '')}", file=sys.stderr)
    for item in skipped:
        print(f"note: skipped {item['pkg']}/{item['mgr']} {item['asset']} ({item['reason']})")
    for item in unverified:
        print(
            f"warning: {item['pkg']}/{item['mgr']} {item['asset']}: {item['reason']}; not verified",
            file=sys.stderr,
        )

    print("Preflight summary:")
    print(f"- Packages checked: {checked}")
    print(f"- Required assets passed: {required_passed}")
    print(f"- Optional assets missing: {len(optional)}")
    print(f"- Validation failures: {len(errors)}")
    print(f"- Packages blocked: {len(blocked)}")
    print(f"- Assets unverified: {len(unverified)}")

    if plan_output:
        os.makedirs(os.path.dirname(os.path.abspath(plan_output)) or ".", exist_ok=True)
        with open(plan_output, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "summary": summary,
                    "packages": stages,
                    "errors": errors,
                    "optional_missing": optional,
                    "skipped": skipped,
                    "unverified": unverified,
                },
                handle,
                indent=2,
                ensure_ascii=False,
            )
            handle.write("\n")

    if errors:
        print("BUILD PHASE NOT STARTED")
        return 1
    print(f"Preflight complete; {len(stages)} package(s) staged.")
    return 0


USAGE = """Usage:
  fetch_assets.py --plan --stage-root DIR --plan-output FILE [--probe] [--offline] [--allow-unreachable] [--arch ARCH] <src/mgr/pkg>...
  fetch_assets.py --fetch-plan FILE [--cache-dir DIR]
  fetch_assets.py --preflight --stage-root DIR [--plan-output FILE] [--cache-dir DIR] [--arch ARCH] <src/mgr/pkg>...
  fetch_assets.py <package_dir> [build_json_path]"""


def parse_arguments(argv):
    options = {
        "mode": None, "stage_root": None, "plan_output": None, "plan_path": None,
        "cache_dir": None, "arch": None, "probe": False, "packages": [],
        "offline": False, "allow_unreachable": False,
    }
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--plan":
            options["mode"] = "plan"
            index += 1
        elif arg == "--fetch-plan":
            options["mode"] = "fetch"
            if index + 1 >= len(argv):
                raise ValueError("--fetch-plan requires a file")
            options["plan_path"] = argv[index + 1]
            index += 2
        elif arg == "--preflight":
            options["mode"] = "preflight"
            index += 1
        elif arg == "--probe":
            options["probe"] = True
            index += 1
        elif arg == "--offline":
            options["offline"] = True
            index += 1
        elif arg == "--allow-unreachable":
            options["allow_unreachable"] = True
            index += 1
        elif arg in ("--stage-root", "--plan-output", "--cache-dir", "--arch"):
            if index + 1 >= len(argv):
                raise ValueError(f"{arg} requires a value")
            options[arg[2:].replace("-", "_")] = argv[index + 1]
            index += 2
        elif arg.startswith("-"):
            raise ValueError(f"unknown option {arg}")
        else:
            options["packages"].append(arg)
            index += 1
    return options


def main():
    argv = sys.argv[1:]
    if not argv:
        print(USAGE, file=sys.stderr)
        sys.exit(2)

    if not argv[0].startswith("-"):
        # Legacy direct mode for one package, used by local checks.
        pkg_dir = argv[0]
        build_json_path = argv[1] if len(argv) > 1 else os.path.join(pkg_dir, "build.json")
        if not os.path.exists(build_json_path):
            print(f"Skipping: {build_json_path} not found.")
            return
        with open(build_json_path, 'r') as f:
            build_config = json.load(f)
        provider = build_config.get("source", {}).get("provider", "github")
        if provider == "github":
            success = fetch_github_assets(pkg_dir, build_config)
        elif provider == "http_template":
            success = fetch_http_template_assets(pkg_dir, build_config)
        elif provider == "tarball":
            success = extract_tarball(pkg_dir, build_config)
        else:
            print(f"Unknown provider: {provider}")
            sys.exit(1)
        if not success:
            print("Fetch failed or no assets found.")
            sys.exit(1)
        problems, skipped = check_downloaded(pkg_dir, build_config)
        for path in skipped:
            print(f"warning: could not read the package name of {path}; not verified")
        if problems:
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            print(f"\n{len(problems)} dependency mismatch(es)", file=sys.stderr)
            sys.exit(1)
        return

    try:
        options = parse_arguments(argv)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(2)

    mode = options["mode"]
    if mode == "plan":
        if not options["stage_root"] or not options["plan_output"]:
            print("error: --plan requires --stage-root and --plan-output", file=sys.stderr)
            sys.exit(2)
        sys.exit(
            plan_all(
                options["packages"], options["stage_root"], options["plan_output"],
                probe=options["probe"], arch_filter=options["arch"],
                offline=options["offline"], allow_unreachable=options["allow_unreachable"],
            )
        )
    if mode == "fetch":
        sys.exit(fetch_plan_file(options["plan_path"], options["cache_dir"]))
    if mode == "preflight":
        if not options["stage_root"]:
            print("error: --preflight requires --stage-root", file=sys.stderr)
            sys.exit(2)
        if options["probe"]:
            print("error: --probe is only valid with --plan", file=sys.stderr)
            sys.exit(2)
        sys.exit(
            preflight(
                options["packages"], options["stage_root"], options["plan_output"],
                arch_filter=options["arch"], cache_dir=options["cache_dir"],
            )
        )
    print(USAGE, file=sys.stderr)
    sys.exit(2)

if __name__ == "__main__":
    main()
