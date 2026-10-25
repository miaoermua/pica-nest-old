"""Read package identity out of a built `.ipk` or `.apk` container.

The filename a job matches upstream with is not the name Pica installs. An
`luci-app-openclash_0.47.075_all.ipk` asset installs as `luci-app-openclash`,
and only the container's control metadata says so authoritatively.

`.ipk` is read here: an outer tar whose `control.tar.gz` member holds `control`.

`.apk` comes in two shapes. The legacy one is concatenated gzip and `tar` opens
it. The apk-tools 3.x one is the ADB container: it starts with `ADBd`, and
neither GNU tar nor BusyBox tar can open it — `unrecognized archive format` /
`unrecognized option: ignore-zeros` respectively. `apk adbdump` is the only
tool that reads it properly, and it is not something a build can depend on
being installed.

So it is read here instead. The container is a single raw-deflate stream
behind a four-byte `ADBd` header; inflated, it begins with an `ADB.pckg`
segment — the package database — whose records hold the same fields a
`control` file does. A record starts with the package name, and the byte after
it is the length of the version string that follows, which is enough to tell a
name from a length prefix without guessing:

    adguardhome \\x0b 0.107.76-r1
    ^^^^^^^^^^^ ^^^^^^ ^^^^^^^^^
    name         len    version

Three things have to agree before the name is believed — the name is made of
package-name characters, the byte after it really is the length of the string
that follows, and that string looks like a version. A container that fails
any of them is reported as unreadable rather than as a name, because a
guessed name would defeat the purpose of the check.

The package name and architecture are read; dependency metadata is deliberately
ignored because no backend in this repository resolves dependency graphs.
"""
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import zlib

# APK signature block of the apk-tools 3.x (ADB) format, which tar cannot open.
ADB_SIGNATURE = b"ADBd"
# The package-database segment inside the inflated container.
ADB_PACKAGE_SEGMENT = b"ADB.pckg"
# magic, a 64-bit length, a 24-bit one and a 16-bit one.
ADB_SEGMENT_HEADER = 21

DEPENDENCY_KEYS = ("app", "base", "kmod", "app_i18n")

# A package name as the feeds spell it. The alphabet is not all lowercase:
# `UAmask` is spelled with capitals, so a pattern that demands lowercase
# would refuse a package this repository publishes.
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$")
# A version as the feeds spell it: digits and dots, with an optional release
# and an optional build identifier. LuCI packages carry the latter — a
# vendor build of the interface, such as `26.236.50544~cb5d434` — so a
# version that only allows `-r1` would refuse every one of them.
_VERSION = re.compile(r"^\d+(?:\.\d+)*(?:[-~][0-9A-Za-z.+~]*)?$")


def _extract(path, dest):
    """Unpack `path` into `dest`, returning True on success."""
    try:
        # bsdtar (macOS) rejects --ignore-zeros; busybox tar needs it. Try the
        # portable spelling last so a single-member archive still unpacks.
        for extra in (["--ignore-zeros"], []):
            result = subprocess.run(
                ["tar", "-xzf", path, "-C", dest] + extra,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return True
    except FileNotFoundError:
        print(f"warning: tar not available, cannot inspect {os.path.basename(path)}")
    return False


def _read_ipk_control(path, dest):
    """Return the raw `control` text of an .ipk, or None."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()
            control_member = next(
                (m for m in members if os.path.basename(m.name).startswith("control.tar")),
                None,
            )
            if control_member is not None:
                extracted = os.path.join(dest, "control.tar")
                with open(extracted, "wb") as handle:
                    shutil.copyfileobj(tar.extractfile(control_member), handle)
            else:
                # Some builders inline control as a plain file.
                plain = next((m for m in members if os.path.basename(m.name) == "control"), None)
                if plain is None:
                    return None
                return tar.extractfile(plain).read().decode("utf-8", "replace")
    except (tarfile.TarError, OSError) as err:
        print(f"warning: cannot read {os.path.basename(path)}: {err}")
        return None

    inner = os.path.join(dest, "control")
    os.makedirs(inner, exist_ok=True)
    if not _extract(extracted, inner):
        return None
    for root, _, files in os.walk(inner):
        if "control" in files:
            with open(os.path.join(root, "control"), "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
    return None


def _inflate_adb(path):
    """Return the inflated ADB container of an .apk, or None.

    The container is one raw-deflate stream after the four-byte signature, so
    it is inflated with a plain deflate decompressor at a four-byte offset —
    which is why no zlib header is found anywhere in the file.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if raw[:4] != ADB_SIGNATURE:
        return None
    try:
        return zlib.decompressobj(-15).decompress(raw[4:])
    except zlib.error:
        return None


def _read_adb_package(path):
    """Return (package, version) from an apk-tools 3.x container, or (None, None).

    The package database segment holds one record per package, each starting
    with the package name. A single .apk carries one package here, so the first
    record is the one. The name is only accepted when the byte after it
    really is the length of the version string that follows — a name read out
    of the wrong offset would otherwise look perfectly plausible.
    """
    inflated = _inflate_adb(path)
    if inflated is None:
        return None, None
    if inflated[:8] != ADB_PACKAGE_SEGMENT:
        return None, None

    body = inflated[ADB_SEGMENT_HEADER:]
    end = 0
    while end < len(body) and body[end] >= 0x20:
        end += 1
    name = body[:end]
    if not name or not _PACKAGE_NAME.match(name.decode("ascii", "replace")):
        return None, None

    # The byte after the name is the length of the version string.
    if end >= len(body):
        return None, None
    length = body[end]
    version = body[end + 1: end + 1 + length]
    if len(version) != length or not _VERSION.match(version.decode("ascii", "replace")):
        return None, None

    return name.decode("ascii", "replace"), version.decode("ascii", "replace")


def _read_apk_control(path, dest):
    """Return the control text of an .apk, or None when it cannot be read.

    Handles the legacy concatenated-gzip layout. The apk-tools 3.x (ADB)
    layout is read by `_read_adb_package` instead, which pulls the name and
    version out of the package database rather than a `control` file.
    """
    package, version = _read_adb_package(path)
    if package is not None:
        return f"Package: {package}\nVersion: {version}\n"

    try:
        with open(path, "rb") as handle:
            if handle.read(4) == ADB_SIGNATURE:
                print(
                    f"note: {os.path.basename(path)} is the apk-tools 3.x (ADB) format"
                    " and its package database could not be read;"
                    " the name was not verified against the manifest."
                )
                return None
    except OSError:
        return None

    if not _extract(path, dest):
        return None
    chunks = []
    for entry in sorted(os.listdir(dest)):
        if entry.startswith(".") and os.path.isfile(os.path.join(dest, entry)):
            try:
                with open(os.path.join(dest, entry), "r", encoding="utf-8", errors="replace") as handle:
                    chunks.append(handle.read())
            except OSError:
                continue
    return "\n".join(chunks) if chunks else None


def container_pkgname(path):
    """Return the `Package:` declared inside a container, or None.

    Only the package name is read here; dependency metadata is deliberately
    ignored because no backend in this repo resolves dependency graphs.
    """
    metadata = container_metadata(path)
    return metadata["package"] if metadata else None


def container_metadata(path):
    """Return `Package:`, `Version:` and `Architecture:` of a container, or None.

    `None` means the container could not be read: a corrupt archive, a missing
    `tar`, or an ADB container whose package database does not parse. That is a
    distinct outcome from "read and found no metadata"; callers report it as
    unverified instead of treating it as a successful check.
    """
    extension = os.path.splitext(path)[1].lower()
    if extension not in (".ipk", ".apk"):
        return None

    with tempfile.TemporaryDirectory(prefix="pica-inspect-") as tmp:
        text = _read_apk_control(path, tmp) if extension == ".apk" else _read_ipk_control(path, tmp)
        if not text:
            return None
        package = re.search(r"^Package:\s*(\S+)\s*$", text, re.MULTILINE)
        version = re.search(r"^Version:\s*(\S+)\s*$", text, re.MULTILINE)
        architecture = re.search(r"^Architecture:\s*(\S+)\s*$", text, re.MULTILINE)
        if package is None and architecture is None:
            return None
        return {
            "package": package.group(1) if package else None,
            "version": version.group(1) if version else None,
            "architecture": architecture.group(1) if architecture else None,
        }


def manifest_pkgver(manifest_path):
    """Return the manifest's `pkgver`, or None when it cannot be read."""
    if not os.path.isfile(manifest_path):
        return None
    with open(manifest_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("pkgver"):
                _, _, value = line.partition("=")
                return value.strip() or None
    return None


def version_matches(expected, actual):
    """Whether `actual` names the same upstream version as `expected`.

    Upstream spells separators differently across containers and filenames
    (`1.8-9`/`1.8.9`, `26.156.15499~hash`/`26.156.15499.hash`), and a container
    may append its package revision (`-r1`). Those are one version. A different
    base version is not.
    """
    def normalize(value):
        return re.sub(r"[._~-]+", ".", value.strip().lower().lstrip("v"))

    needle = normalize(expected)
    haystack = normalize(actual)
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


def manifest_dependencies(manifest_path):
    """Map each declared key to every value declared for it.

    A manifest may declare `app = a` twice, or a backend-scoped
    `app@opkg = b` / arch-scoped `app.<arch> = c`, so all scoped forms are
    collected under their base key. `app_i18n` is collected with its `{lang}`
    template intact; [`declares_package`] matches it against a concrete
    language package.
    """
    declared = {key: set() for key in DEPENDENCY_KEYS}
    if not os.path.isfile(manifest_path):
        return declared

    with open(manifest_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().split(".", 1)[0].split("@", 1)[0]
            if key in declared and value.strip():
                declared[key].add(value.strip())
    return declared


def declares_package(declared, name):
    """Whether a manifest declares `name` as an app/base/kmod/i18n package.

    `app_i18n` is a template (`luci-i18n-demo-{lang}`), so the language segment
    is matched as a wildcard rather than requiring the template literally.
    """
    known = declared.get("app", set()) | declared.get("base", set()) | declared.get("kmod", set())
    if name in known:
        return True
    for template in declared.get("app_i18n", ()):
        pattern = "^" + re.escape(template).replace(re.escape("{lang}"), ".+") + "$"
        if re.match(pattern, name):
            return True
    return False


def check_downloaded(pkg_dir, build_config):
    """Fail when a fetched container's package name is not declared in the manifest.

    Upstream renames a package without touching this repo often enough that a
    silent mismatch would ship a package that installs a name nothing else
    references. Declared names are the contract; this makes a violation loud.

    Containers whose name cannot be read are reported separately rather than
    counted as mismatches, so an unreadable format does not fail the build.

    Returns (problems, skipped).
    """
    backend = build_config.get("mgr", "opkg")
    pkgs_root = os.path.join(pkg_dir, "pkgs", backend)
    declared = manifest_dependencies(build_config.get("manifest") or os.path.join(pkg_dir, "manifest"))
    known = declared["app"] | declared["base"] | declared["kmod"]

    problems = []
    skipped = []
    if not os.path.isdir(pkgs_root):
        return problems, skipped
    has_declarations = any(declared.values())

    for platform in sorted(os.listdir(pkgs_root)):
        platform_dir = os.path.join(pkgs_root, platform)
        if not os.path.isdir(platform_dir):
            continue
        for arch in sorted(os.listdir(platform_dir)):
            arch_dir = os.path.join(platform_dir, arch)
            if not os.path.isdir(arch_dir):
                continue
            for entry in sorted(os.listdir(arch_dir)):
                path = os.path.join(arch_dir, entry)
                if not os.path.isfile(path):
                    continue
                name = container_pkgname(path)
                if name is None:
                    # An unreadable container is not proof of a mismatch; only a
                    # confirmed mismatch should fail the build.
                    skipped.append(f"{platform}/{arch}/{entry}")
                elif has_declarations and not declares_package(declared, name):
                    problems.append(
                        f"{platform}/{arch}/{entry}: installs as '{name}', which the manifest does not"
                        f" declare. Add it (app/base/kmod, or '<key>.{arch} =' when it varies by arch)"
                        f" or update the asset regex."
                    )
                else:
                    print(f"checked {platform}/{arch}/{entry}: installs as '{name}'")

    return problems, skipped
