"""Relabelling iOS device objects for the arm64 iOS simulator, for objects that mac2ios can't relabel."""

import struct
from collections.abc import Iterator
from pathlib import Path

from .common import BuildError

MH_MAGIC_64 = 0xFEEDFACF
MH_OBJECT = 1
MACH_HEADER_64 = "<IiiIIIII"
LOAD_COMMAND = "<II"

LC_SYMTAB = 0x2
LC_DYSYMTAB = 0xB
LC_SEGMENT_64 = 0x19
LC_VERSION_MIN_IPHONEOS = 0x25
LC_FUNCTION_STARTS = 0x26
LC_DATA_IN_CODE = 0x29
LC_LINKER_OPTION = 0x2D
LC_LINKER_OPTIMIZATION_HINT = 0x2E
LC_BUILD_VERSION = 0x32

PLATFORM_IOSSIMULATOR = 7
TOOL_CLANG = 1

# LC_BUILD_VERSION with one tool is 16 bytes longer than LC_VERSION_MIN_IPHONEOS: everything after the load commands
# moves by 16, which keeps the 16-byte alignment of the sections' data
BUILD_VERSION = "<IIIIIIII"
SHIFT = struct.calcsize(BUILD_VERSION) - struct.calcsize("<IIII")

# the offset fields, by their byte offset in each command, that point into the data after the load commands
SYMTAB_OFFSETS = (8, 16)
DYSYMTAB_OFFSETS = (32, 40, 48, 56, 64, 72)
LINKEDIT_DATA_OFFSETS = (8,)
SEGMENT_64_FILEOFF = 40
SEGMENT_64_NSECTS = 64
SEGMENT_64_HEADER = 72
SECTION_64_SIZE = 80
SECTION_64_OFFSET = 48
SECTION_64_RELOFF = 56
OFFSET_FIELDS = {
    LC_SYMTAB: SYMTAB_OFFSETS,
    LC_DYSYMTAB: DYSYMTAB_OFFSETS,
    LC_FUNCTION_STARTS: LINKEDIT_DATA_OFFSETS,
    LC_DATA_IN_CODE: LINKEDIT_DATA_OFFSETS,
    LC_LINKER_OPTIMIZATION_HINT: LINKEDIT_DATA_OFFSETS,
    LC_LINKER_OPTION: (),
    LC_BUILD_VERSION: (),
}

AR_MAGIC = b"!<arch>\n"
AR_HEADER = 60
AR_SIZE = slice(48, 58)
AR_LONG_NAME = b"#1/"
AR_SYMBOL_TABLE = b"__.SYMDEF"


def shift_offset(command: bytearray, at: int, field: str = "<I") -> None:
    (value,) = struct.unpack_from(field, command, at)
    if value:
        struct.pack_into(field, command, at, value + SHIFT)


def shift_command(cmd: int, command: bytearray) -> None:
    if cmd == LC_SEGMENT_64:
        shift_offset(command, SEGMENT_64_FILEOFF, "<Q")
        (nsects,) = struct.unpack_from("<I", command, SEGMENT_64_NSECTS)
        for i in range(nsects):
            section = SEGMENT_64_HEADER + i * SECTION_64_SIZE
            shift_offset(command, section + SECTION_64_OFFSET)
            shift_offset(command, section + SECTION_64_RELOFF)
        return
    if cmd not in OFFSET_FIELDS:
        raise BuildError(f"load command {cmd:#x} may point into the object; it is not known how to move it")
    for at in OFFSET_FIELDS[cmd]:
        shift_offset(command, at)


def simulator_object(obj: bytes) -> bytes:
    """obj with its LC_VERSION_MIN_IPHONEOS replaced by an LC_BUILD_VERSION for the simulator, or obj unchanged."""
    magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved = struct.unpack_from(MACH_HEADER_64, obj)
    if magic != MH_MAGIC_64 or filetype != MH_OBJECT:
        return obj
    header = struct.calcsize(MACH_HEADER_64)
    commands, offset, replaced = [], header, False
    for _ in range(ncmds):
        cmd, size = struct.unpack_from(LOAD_COMMAND, obj, offset)
        command = bytearray(obj[offset : offset + size])
        if cmd == LC_VERSION_MIN_IPHONEOS:
            _, _, minos, sdk = struct.unpack_from("<IIII", command)
            new_size = struct.calcsize(BUILD_VERSION)
            build = struct.pack(BUILD_VERSION, LC_BUILD_VERSION, new_size, PLATFORM_IOSSIMULATOR, minos, sdk, 1, TOOL_CLANG, sdk)
            commands.append(bytearray(build))
            replaced = True
        else:
            commands.append(command)
        offset += size
    if not replaced:
        return obj
    for command in commands:
        shift_command(struct.unpack_from("<I", command)[0], command)
    new_header = struct.pack(MACH_HEADER_64, magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds + SHIFT, flags, reserved)
    return new_header + b"".join(commands) + obj[header + sizeofcmds :]


def archive_members(data: bytes) -> Iterator[tuple[bytes, bytes, bytes]]:
    """Each member's header, its BSD long name bytes (empty for short names) and its contents."""
    if not data.startswith(AR_MAGIC):
        raise BuildError("not an ar archive")
    offset = len(AR_MAGIC)
    while offset < len(data):
        header = data[offset : offset + AR_HEADER]
        size = int(header[AR_SIZE].decode().strip())
        body = data[offset + AR_HEADER : offset + AR_HEADER + size]
        name_length = int(header[3:16].decode().strip()) if header.startswith(AR_LONG_NAME) else 0
        yield header, body[:name_length], body[name_length:]
        offset += AR_HEADER + size + (size & 1)


def simulator_archive(lib: Path) -> int:
    """Rewrites lib's device objects for the simulator, without its symbol table, which ranlib must write again;
    returns how many objects changed."""
    data = lib.read_bytes()
    out, changed = bytearray(AR_MAGIC), 0
    for header, name, contents in archive_members(data):
        if name.startswith(AR_SYMBOL_TABLE) or header.startswith(AR_SYMBOL_TABLE):
            continue
        new = simulator_object(contents)
        changed += new is not contents
        size = len(name) + len(new)
        out += header[: AR_SIZE.start] + str(size).encode().ljust(AR_SIZE.stop - AR_SIZE.start) + header[AR_SIZE.stop :]
        out += name + new + (b"\n" if size & 1 else b"")
    lib.write_bytes(out)
    return changed
