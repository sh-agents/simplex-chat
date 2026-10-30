#!/usr/bin/env python3

# ZIP details of signed APKs that scripts/simplex-chat-reproduce-builds-android.sh needs and its tools don't handle.
# Usage:
#   apk-zip.py padding <apk>                  prints "apksigner" when entries are padded with apksigner's 0xd935 extra
#                                             field (apksigner >= 35.0.0-rc1), otherwise "zero"
#   apk-zip.py copy-v1-flags <from> <to>      copies the general purpose flags of the v1 signature files from one APK
#                                             to another, in place: apksigner >= 35.0.0-rc1 sets the UTF-8 flag on
#                                             them, which apksigcopier 1.1.1 drops when it copies the signature

import struct
import sys

LOCAL_HEADER = b"PK\x03\x04"
CENTRAL_HEADER = b"PK\x01\x02"
END_OF_CENTRAL_DIRECTORY = b"PK\x05\x06"
APKSIGNER_ALIGNMENT_EXTRA = 0xD935
V1_SIGNATURE_SUFFIXES = (".SF", ".RSA", ".DSA", ".EC")


class ZipEntry:
    def __init__(self, name, central_offset, local_offset, flags):
        self.name = name
        self.central_offset = central_offset
        self.local_offset = local_offset
        self.flags = flags


def central_directory(data):
    end = data.rfind(END_OF_CENTRAL_DIRECTORY)
    if end < 0:
        raise ValueError("no end of central directory record")
    count, _, offset = struct.unpack("<HII", data[end + 10 : end + 20])
    entries = []
    for _ in range(count):
        if data[offset : offset + 4] != CENTRAL_HEADER:
            raise ValueError(f"no central directory header at {offset}")
        flags = struct.unpack("<H", data[offset + 8 : offset + 10])[0]
        name_len, extra_len, comment_len = struct.unpack("<HHH", data[offset + 28 : offset + 34])
        local_offset = struct.unpack("<I", data[offset + 42 : offset + 46])[0]
        name = data[offset + 46 : offset + 46 + name_len].decode("utf-8")
        entries.append(ZipEntry(name, offset, local_offset, flags))
        offset += 46 + name_len + extra_len + comment_len
    return entries


def local_extra_fields(data, entry):
    offset = entry.local_offset
    if data[offset : offset + 4] != LOCAL_HEADER:
        raise ValueError(f"no local header for {entry.name} at {offset}")
    name_len, extra_len = struct.unpack("<HH", data[offset + 26 : offset + 30])
    extra = data[offset + 30 + name_len : offset + 30 + name_len + extra_len]
    fields = []
    while len(extra) >= 4:
        field_id, size = struct.unpack("<HH", extra[:4])
        fields.append(field_id)
        extra = extra[4 + size :]
    return fields


def padding(data):
    for entry in central_directory(data):
        if APKSIGNER_ALIGNMENT_EXTRA in local_extra_fields(data, entry):
            return "apksigner"
    return "zero"


def is_v1_signature_file(name):
    if not name.startswith("META-INF/") or "/" in name[len("META-INF/") :]:
        return False
    return name == "META-INF/MANIFEST.MF" or name.endswith(V1_SIGNATURE_SUFFIXES)


def copy_v1_flags(source, target):
    flags = {e.name: e.flags for e in central_directory(source) if is_v1_signature_file(e.name)}
    patched = bytearray(target)
    for entry in central_directory(target):
        if entry.name in flags:
            packed = struct.pack("<H", flags[entry.name])
            patched[entry.central_offset + 8 : entry.central_offset + 10] = packed
            patched[entry.local_offset + 6 : entry.local_offset + 8] = packed
    return bytes(patched)


def main(args):
    if len(args) == 2 and args[0] == "padding":
        with open(args[1], "rb") as f:
            print(padding(f.read()))
    elif len(args) == 3 and args[0] == "copy-v1-flags":
        with open(args[1], "rb") as f:
            source = f.read()
        with open(args[2], "rb") as f:
            target = f.read()
        with open(args[2], "wb") as f:
            f.write(copy_v1_flags(source, target))
    else:
        sys.exit("usage: apk-zip.py padding <apk> | apk-zip.py copy-v1-flags <from apk> <to apk>")


if __name__ == "__main__":
    main(sys.argv[1:])
