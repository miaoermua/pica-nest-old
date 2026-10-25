#!/usr/bin/env python3
"""Tests for reading package identity out of a container.

The apk-tools 3.x container is the interesting case: it cannot be opened by
`tar` at all, so it is parsed here. The fixtures are built rather than
downloaded, so the tests do not depend on a feed staying where it is.
"""
import os
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from container_metadata import (  # noqa: E402
    container_metadata,
    container_pkgname,
    declares_package,
    manifest_dependencies,
)

# The segment header of the package database: a magic, a 64-bit length, a
# 24-bit one and a 16-bit one. Only the magic is load-bearing for the parser;
# the rest is reproduced so the fixture looks like the real thing.
SEGMENT = b"ADB.pckg" + (2524).to_bytes(8, "little") + (2504).to_bytes(3, "little") + (2528).to_bytes(2, "little")


def adb_apk(pkgname, version, *, length=None, name=None, tail=b""):
    """Build an apk-tools 3.x container holding one package record."""
    body = bytearray(SEGMENT)
    body += (name if name is not None else pkgname).encode()
    body.append(len(version) if length is None else length)
    body += version.encode()
    body += tail
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return b"ADBd" + compressor.compress(bytes(body)) + compressor.flush()


class AdbContainerTest(unittest.TestCase):
    def write(self, payload, name="demo.apk"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_bytes(payload)
            return container_metadata(str(path))

    def test_the_package_name_and_version_are_read(self):
        meta = self.write(adb_apk("node-pnpm", "11.15.1-r1"))
        self.assertEqual(meta["package"], "node-pnpm")
        self.assertEqual(meta["version"], "11.15.1-r1")

    def test_a_luci_build_identifier_is_a_version(self):
        """LuCI packages carry a vendor build of the interface.

        The version is `26.236.50544~cb5d434`, and a version pattern that only
        allows a `-r1` release would refuse every LuCI package in the source.
        """
        meta = self.write(adb_apk("luci-app-adguardhome", "26.236.50544~cb5d434"))
        self.assertEqual(meta["package"], "luci-app-adguardhome")
        self.assertEqual(meta["version"], "26.236.50544~cb5d434")

    def test_a_capitalised_package_name_is_read(self):
        """`UAmask` is spelled with capitals, and this repository publishes it.

        A name pattern that demanded lowercase would refuse that package and
        quietly stop verifying it.
        """
        meta = self.write(adb_apk("UAmask", "0.4.3-r1"))
        self.assertEqual(meta["package"], "UAmask")
        self.assertEqual(meta["version"], "0.4.3-r1")

    def test_a_name_that_is_not_a_package_name_is_refused(self):
        """Reading a length prefix as a name would produce nonsense."""
        meta = self.write(adb_apk("demo", "1.0-r1", name="!! not a name"))
        self.assertIsNone(meta)

    def test_a_length_prefix_that_does_not_match_the_version_is_refused(self):
        """The byte after the name has to be the version's length.

        Without this the parser would believe any string that happened to sit
        where a package name is, and a container read from the wrong offset
        would look like a valid name.
        """
        meta = self.write(adb_apk("demo", "1.0-r1", length=99))
        self.assertIsNone(meta)

    def test_a_version_that_is_not_a_version_is_refused(self):
        meta = self.write(adb_apk("demo", "not-a-version"))
        self.assertIsNone(meta)

    def test_a_file_that_is_not_a_container_is_refused(self):
        self.assertIsNone(self.write(b"not an apk at all"))
        self.assertIsNone(self.write(b""))

    def test_a_legacy_container_still_reads(self):
        """The concatenated-gzip layout is handled by `tar`, and still is."""
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # The legacy layout keeps its metadata in a dotfile, which is what
            # the reader looks for.
            info = tarfile.TarInfo(".PKGINFO")
            payload = b"Package: legacy-demo\nVersion: 2.0-r1\nArchitecture: x86_64\n"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        meta = self.write(buf.getvalue(), name="legacy.apk")
        self.assertEqual(meta["package"], "legacy-demo")
        self.assertEqual(meta["version"], "2.0-r1")
        self.assertEqual(meta["architecture"], "x86_64")

    def test_the_package_name_is_available_on_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.apk"
            path.write_bytes(adb_apk("node-npm", "22.23.2-r1"))
            self.assertEqual(container_pkgname(str(path)), "node-npm")


class ManifestDependencyTest(unittest.TestCase):
    """The names a manifest declares, reduced to the names containers carry.

    A manifest spells constraints the way the client reads them — `libfoo>=1.2`
    for a version bound, a trailing `?` for an optional declaration. A container
    installs under the bare name, so comparing against the raw text would say
    every optional dependency is undeclared.
    """

    def declared(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest"
            path.write_text(text, encoding="utf-8")
            return manifest_dependencies(str(path))

    def test_an_optional_marker_is_not_part_of_the_name(self):
        declared = self.declared("app = luci-app-x\nbase = sing-box?\nbase = xray-core?\n")
        self.assertIn("sing-box", declared["base"])
        self.assertNotIn("sing-box?", declared["base"])
        self.assertTrue(declares_package(declared, "sing-box"))

    def test_a_version_bound_is_not_part_of_the_name(self):
        declared = self.declared("app = luci-app-x\nbase = libfoo>=1.2\nbase = bar<2?\n")
        self.assertEqual(declared["base"], {"libfoo", "bar"})

    def test_a_language_template_is_kept_whole(self):
        """`{lang}` has to survive: it is expanded into a pattern, not compared."""
        declared = self.declared("app = luci-app-x\napp_i18n = luci-i18n-x-{lang}\n")
        self.assertEqual(declared["app_i18n"], {"luci-i18n-x-{lang}"})
        self.assertTrue(declares_package(declared, "luci-i18n-x-zh-cn"))
        self.assertFalse(declares_package(declared, "luci-i18n-other-zh-cn"))

    def test_the_container_name_is_what_matches(self):
        """`app` holds the container's own name, which is what gets installed."""
        declared = self.declared("app = luci-app-passwall\nbase = luci-base\n")
        self.assertTrue(declares_package(declared, "luci-app-passwall"))
        self.assertFalse(declares_package(declared, "passwall"))


if __name__ == "__main__":
    unittest.main()
