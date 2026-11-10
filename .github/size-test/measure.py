#!/usr/bin/env python3
"""Sizes of iOS archives: each Mach-O file with its __TEXT segment and signature, the asset catalogs, and the
estimated download size, the way App Store delivery packs the app (zip deflate; FairPlay encrypts __TEXT, which then
doesn't compress). On macOS the asset catalogs are also thinned for 2x and 3x phones with assetutil.
Usage: measure.py <out.json> <name> <archive dir> [<name> <archive dir>...]"""

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

MB = 1_000_000
FAT_MAGIC = 0xCAFEBABE
MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_ARM64 = 0x0100000C
LC_SEGMENT_64 = 0x19
LC_CODE_SIGNATURE = 0x1D
SCALES = (2, 3)


def arm64_slice(data: bytes) -> bytes:
    if struct.unpack_from(">I", data, 0)[0] != FAT_MAGIC:
        return data
    count = struct.unpack_from(">I", data, 4)[0]
    for i in range(count):
        cputype, _, offset, size, _ = struct.unpack_from(">iiIII", data, 8 + i * 20)
        if cputype == CPU_TYPE_ARM64:
            return data[offset : offset + size]
    raise ValueError("no arm64 slice")


def is_macho(path: Path) -> bool:
    with path.open("rb") as f:
        head = f.read(4)
    return len(head) == 4 and (struct.unpack("<I", head)[0] == MH_MAGIC_64 or struct.unpack(">I", head)[0] == FAT_MAGIC)


def macho_parts(data: bytes) -> tuple[int, int]:
    """(__TEXT bytes, code signature bytes) of a thin Mach-O."""
    _, _, _, _, ncmds, _, _, _ = struct.unpack_from("<IiiIIIII", data, 0)
    offset = 32
    text = signature = 0
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, offset)
        if cmd == LC_SEGMENT_64 and data[offset + 8 : offset + 24].rstrip(b"\0") == b"__TEXT":
            text = struct.unpack_from("<Q", data, offset + 48)[0]
        elif cmd == LC_CODE_SIGNATURE:
            signature = struct.unpack_from("<I", data, offset + 12)[0]
        offset += cmdsize
    return text, signature


def deflated(data: bytes) -> int:
    return len(zlib.compress(data, 9))


def thin_catalog(car: Path, scale: int, work: Path) -> int | None:
    if shutil.which("xcrun") is None:
        return None
    out = work / f"{car.parent.name}-{scale}x.car"
    cmd = ["xcrun", "assetutil", "--idiom", "phone", "--scale", str(scale), "--display-gamut", "srgb", "-o", str(out), str(car)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out.exists():
        print(f"assetutil failed for {car}: {result.stderr.strip()}", file=sys.stderr)
        return None
    return deflated(out.read_bytes())


def measure(archive: Path, work: Path) -> dict:
    app = next((archive / "Products/Applications").glob("*.app"))
    files = {}
    code_whole = code_text = rest = 0
    catalogs = {}
    for path in sorted(p for p in app.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = str(path.relative_to(app))
        if is_macho(path):
            data = arm64_slice(path.read_bytes())
            text, signature = macho_parts(data)
            # unsigned archives have no signature; the delivered app always has one, so it is left out of both
            unsigned = len(data) - signature
            code_whole += unsigned
            code_text += text + deflated(data[text : len(data) - signature])
            if unsigned > MB / 2:
                files[rel] = {"bytes": unsigned, "text": text, "signature": signature}
        else:
            size = deflated(path.read_bytes())
            rest += size
            if path.name == "Assets.car":
                catalogs[rel] = {"bytes": path.stat().st_size, "deflated": size}
                for scale in SCALES:
                    catalogs[rel][f"{scale}x"] = thin_catalog(path, scale, work)
    result = {
        "files": files,
        "catalogs": catalogs,
        "estimate_whole_binaries": code_whole + rest,
        "estimate_text_encrypted": code_text + rest,
    }
    for scale in SCALES:
        thinned = [c[f"{scale}x"] for c in catalogs.values()]
        if all(t is not None for t in thinned):
            saving = sum(c["deflated"] for c in catalogs.values()) - sum(thinned)
            result[f"estimate_whole_binaries_{scale}x"] = code_whole + rest - saving
            result[f"estimate_text_encrypted_{scale}x"] = code_text + rest - saving
    return result


def markdown(results: dict[str, dict]) -> str:
    names = list(results)
    rows = ["| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    keys = sorted({k for r in results.values() for k in r if k.startswith("estimate")})
    for key in keys:
        rows.append(f"| {key} | " + " | ".join(f"{r[key] / MB:.2f} MB" if key in r else "-" for r in results.values()) + " |")
    files = sorted({f for r in results.values() for f in r["files"]})
    for f in files:
        rows.append(f"| {f} | " + " | ".join(f"{r['files'][f]['bytes'] / MB:.2f} MB" if f in r["files"] else "-" for r in results.values()) + " |")
    cats = sorted({c for r in results.values() for c in r["catalogs"]})
    for c in cats:
        for field in ("bytes", "2x", "3x"):
            rows.append(
                f"| {c} {field} | "
                + " | ".join(
                    f"{r['catalogs'][c][field] / MB:.2f} MB" if c in r["catalogs"] and r["catalogs"][c][field] else "-"
                    for r in results.values()
                )
                + " |"
            )
    return "\n".join(rows) + "\n"


def main() -> None:
    out = Path(sys.argv[1])
    pairs = sys.argv[2:]
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, archive in zip(pairs[::2], pairs[1::2]):
            results[name] = measure(Path(archive), Path(tmp))
    out.write_text(json.dumps(results, indent=2))
    table = markdown(results)
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("### iOS archive sizes\n\n" + table)


main()
