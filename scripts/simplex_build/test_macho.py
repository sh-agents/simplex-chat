import pathlib
import struct
import tempfile
import unittest

from .testing import build_script

HEADER = "<IiiIIIII"
CPU_ARM64 = 0x0100000C
DATA = b"\x1f\x20\x03\xd5" * 4  # four arm64 nops


def device_object(extra_commands=b"", extra_ncmds=0):
    """An arm64 object as the Nix iOS build leaves it: one __text section with a relocation, a symbol table, and
    LC_VERSION_MIN_IPHONEOS."""
    segment_size, symtab_size, version_size = 72 + 80, 24, 16
    sizeofcmds = segment_size + symtab_size + version_size + len(extra_commands)
    data_start = 32 + sizeofcmds
    reloc_off = data_start + len(DATA)
    sym_off = reloc_off + 8
    str_off = sym_off + 16
    strings = b"\x00_main\x00\x00"
    segment = struct.pack("<II16sQQQQiiII", 0x19, segment_size, b"", 0, len(DATA), data_start, len(DATA), 7, 7, 1, 0)
    section = struct.pack("<16s16sQQIIIIIIII", b"__text", b"__TEXT", 0, len(DATA), data_start, 2, reloc_off, 1, 0x80000400, 0, 0, 0)
    symtab = struct.pack("<IIIIII", 0x2, symtab_size, sym_off, 1, str_off, len(strings))
    version = struct.pack("<IIII", 0x25, version_size, 0x000B0000, 0x00110000)
    header = struct.pack(HEADER, 0xFEEDFACF, CPU_ARM64, 0, 1, 3 + extra_ncmds, sizeofcmds, 0x2000, 0)
    relocation = struct.pack("<iI", 4, 0x2D000000)
    symbol = struct.pack("<IBBHQ", 1, 0x0F, 1, 0, 0)
    return header + segment + section + symtab + version + extra_commands + DATA + relocation + symbol + strings


class MachoTest(unittest.TestCase):
    def test_device_object_becomes_a_simulator_object_with_its_contents_moved_intact(self):
        dev = device_object()
        sim = build_script.simulator_object(dev)
        self.assertEqual(len(sim), len(dev) + 16)
        _, _, _, _, ncmds, sizeofcmds, _, _ = struct.unpack_from(HEADER, sim)
        self.assertEqual((ncmds, sizeofcmds), (3, struct.unpack_from(HEADER, dev)[5] + 16))
        build = sim[32 + 152 + 24 : 32 + 152 + 24 + 32]
        self.assertEqual(struct.unpack("<IIIIIIII", build), (0x32, 32, 7, 0x000B0000, 0x00110000, 1, 1, 0x00110000))
        fileoff, section_offset, reloff = (
            struct.unpack_from("<Q", sim, 32 + 40)[0],
            *struct.unpack_from("<I", sim, 32 + 72 + 48),
            struct.unpack_from("<I", sim, 32 + 72 + 56)[0],
        )
        self.assertEqual(sim[section_offset : section_offset + len(DATA)], DATA)
        self.assertEqual(fileoff, section_offset)
        self.assertEqual(sim[reloff : reloff + 8], dev[reloff - 16 : reloff - 8])
        sym_off, _, str_off, str_size = struct.unpack_from("<IIII", sim, 32 + 152 + 8)
        self.assertEqual(sim[str_off : str_off + str_size], b"\x00_main\x00\x00")
        self.assertEqual(sim[sym_off : sym_off + 16], dev[sym_off - 16 : sym_off])

    def test_objects_without_the_old_command_stay_as_they_are(self):
        sim = build_script.simulator_object(device_object())
        self.assertIs(build_script.simulator_object(sim), sim)
        self.assertEqual(build_script.simulator_object(b"\x00" * 64), b"\x00" * 64)

    def test_unknown_load_commands_are_refused(self):
        unknown = struct.pack("<IIII", 0x99, 16, 0, 0)
        with self.assertRaises(build_script.BuildError):
            build_script.simulator_object(device_object(unknown, 1))

    def test_archive_keeps_its_members_and_drops_the_symbol_table(self):
        dev = device_object()
        with tempfile.TemporaryDirectory() as tmp:
            lib = pathlib.Path(tmp) / "libgmp.a"
            members = b"!<arch>\n"
            for name, body in ((b"__.SYMDEF SORTED\x00\x00\x00\x00", b"symdef\x00\x00"), (b"assert.o\x00\x00\x00\x00", dev)):
                size = len(name) + len(body)
                header = (
                    f"#1/{len(name)}".ljust(16)
                    + "0".ljust(12)
                    + "0".ljust(6)
                    + "0".ljust(6)
                    + "100644".ljust(8)
                    + str(size).ljust(10)
                    + "`\n"
                )
                members += header.encode() + name + body
            lib.write_bytes(members)
            self.assertEqual(build_script.simulator_archive(lib), 1)
            ((header, name, contents),) = build_script.archive_members(lib.read_bytes())
        self.assertEqual(name, b"assert.o\x00\x00\x00\x00")
        self.assertEqual(int(header[48:58]), len(name) + len(dev) + 16)
        self.assertEqual(contents, build_script.simulator_object(dev))
