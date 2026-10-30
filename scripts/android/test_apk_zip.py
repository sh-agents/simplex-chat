#!/usr/bin/env python3

import importlib.util
import io
import pathlib
import struct
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("apk_zip", pathlib.Path(__file__).with_name("apk-zip.py"))
apk_zip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apk_zip)

UTF8_FLAG = 0x800
APKSIGNER_PADDING = struct.pack("<HHH", 0xD935, 2, 4)


def make_zip(entries):
    """entries: (name, data, flags, extra) tuples"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data, _, extra in entries:
            info = zipfile.ZipInfo(name, date_time=(2025, 12, 1, 0, 0, 0))
            info.extra = extra
            z.writestr(info, data)
    # zipfile resets flag_bits when it writes an entry, so the flags are set in the written headers
    data = bytearray(buffer.getvalue())
    flags = {name: f for name, _, f, _ in entries}
    for entry in apk_zip.central_directory(bytes(data)):
        packed = struct.pack("<H", flags[entry.name])
        data[entry.central_offset + 8 : entry.central_offset + 10] = packed
        data[entry.local_offset + 6 : entry.local_offset + 8] = packed
    return bytes(data)


def local_flags(data, name):
    for entry in apk_zip.central_directory(data):
        if entry.name == name:
            return struct.unpack("<H", data[entry.local_offset + 6 : entry.local_offset + 8])[0]
    raise KeyError(name)


def central_flags(data, name):
    return next(e.flags for e in apk_zip.central_directory(data) if e.name == name)


class PaddingTest(unittest.TestCase):
    def test_apksigner_padding_is_detected_on_any_entry(self):
        apk = make_zip([("classes.dex", b"dex", 0, b""), ("res/a.png", b"png", 0, APKSIGNER_PADDING)])
        self.assertEqual(apk_zip.padding(apk), "apksigner")

    def test_zip_without_the_extra_field_has_zero_padding(self):
        apk = make_zip([("classes.dex", b"dex", 0, b""), ("res/a.png", b"png", 0, b"\x00\x00")])
        self.assertEqual(apk_zip.padding(apk), "zero")


class CopyV1FlagsTest(unittest.TestCase):
    signed = make_zip(
        [
            ("classes.dex", b"dex", 0, b""),
            ("META-INF/KEY0.SF", b"sf", UTF8_FLAG, b""),
            ("META-INF/KEY0.RSA", b"rsa", UTF8_FLAG, b""),
            ("META-INF/MANIFEST.MF", b"mf", UTF8_FLAG, b""),
            ("META-INF/versions/9/OSGI-INF/MANIFEST.MF", b"osgi", 0, b""),
        ]
    )
    copied = make_zip(
        [
            ("classes.dex", b"dex", 0, b""),
            ("META-INF/KEY0.SF", b"sf", 0, b""),
            ("META-INF/KEY0.RSA", b"rsa", 0, b""),
            ("META-INF/MANIFEST.MF", b"mf", 0, b""),
            ("META-INF/versions/9/OSGI-INF/MANIFEST.MF", b"osgi", 0, b""),
        ]
    )

    def test_v1_signature_files_get_the_flags_in_both_headers(self):
        patched = apk_zip.copy_v1_flags(self.signed, self.copied)
        for name in ("META-INF/KEY0.SF", "META-INF/KEY0.RSA", "META-INF/MANIFEST.MF"):
            self.assertEqual(local_flags(patched, name), UTF8_FLAG, name)
            self.assertEqual(central_flags(patched, name), UTF8_FLAG, name)

    def test_other_entries_keep_their_flags(self):
        patched = apk_zip.copy_v1_flags(self.signed, self.copied)
        for name in ("classes.dex", "META-INF/versions/9/OSGI-INF/MANIFEST.MF"):
            self.assertEqual(local_flags(patched, name), 0, name)
            self.assertEqual(central_flags(patched, name), 0, name)

    def test_patched_zip_matches_the_signed_one_byte_for_byte(self):
        self.assertNotEqual(self.copied, self.signed)
        self.assertEqual(apk_zip.copy_v1_flags(self.signed, self.copied), self.signed)

    def test_patched_zip_still_reads(self):
        with zipfile.ZipFile(io.BytesIO(apk_zip.copy_v1_flags(self.signed, self.copied))) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.read("META-INF/KEY0.SF"), b"sf")


class V1SignatureFileTest(unittest.TestCase):
    def test_names(self):
        self.assertTrue(apk_zip.is_v1_signature_file("META-INF/MANIFEST.MF"))
        self.assertTrue(apk_zip.is_v1_signature_file("META-INF/KEY0.SF"))
        self.assertTrue(apk_zip.is_v1_signature_file("META-INF/CERT.EC"))
        self.assertFalse(apk_zip.is_v1_signature_file("META-INF/versions/9/OSGI-INF/MANIFEST.MF"))
        self.assertFalse(apk_zip.is_v1_signature_file("META-INF/androidx.core_core.version"))
        self.assertFalse(apk_zip.is_v1_signature_file("assets/KEY0.SF"))


if __name__ == "__main__":
    unittest.main()
