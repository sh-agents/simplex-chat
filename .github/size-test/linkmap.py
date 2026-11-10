#!/usr/bin/env python3
"""The bytes that a link map's symbols take, by Haskell package (from GHC's z-encoded symbol names) or by
object file, both kept in the binary and removed by -dead_strip.
Usage: linkmap.py <link map> [top]"""

import collections
import os
import re
import sys
from pathlib import Path

MB = 1_000_000
UNIT = re.compile(r"^_?([a-z0-9][A-Za-z0-9]*?zm[0-9][0-9zi]*)(?:zm[A-Za-z0-9]+)?_")
VERSION = re.compile(r"zm[0-9][0-9zi]*$")
OBJECT = re.compile(r"^\[\s*(\d+)\]\s+(.*)$")
SYMBOL = re.compile(r"^(0x[0-9A-F]+|<<dead>>)\s+0x([0-9A-F]+)\s+\[\s*(\d+)\]\s+(.*)$")


def owner(symbol: str, path: str) -> str:
    m = UNIT.match(symbol)
    if m:
        return VERSION.sub("", m.group(1)).replace("zm", "-")
    archive = re.search(r"([^/]+\.a)\(", path)
    return f"[{archive.group(1)}]" if archive else f"[{Path(path).name}]"


def main() -> None:
    path = Path(sys.argv[1])
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    objects: dict[int, str] = {}
    kept: dict[str, int] = collections.Counter()
    dead: dict[str, int] = collections.Counter()
    for line in path.read_text(errors="replace").splitlines():
        if m := OBJECT.match(line):
            objects[int(m.group(1))] = m.group(2)
        elif m := SYMBOL.match(line):
            size = int(m.group(2), 16)
            who = owner(m.group(4), objects.get(int(m.group(3)), ""))
            (dead if m.group(1) == "<<dead>>" else kept)[who] += size
    lines = [f"### {path.name}", "", "| Owner | Kept | Dead-stripped |", "|---|---|---|"]
    for who, size in sorted(kept.items(), key=lambda kv: -kv[1])[:top]:
        lines.append(f"| {who} | {size / MB:.2f} MB | {dead.get(who, 0) / MB:.2f} MB |")
    lines.append(f"| total | {sum(kept.values()) / MB:.2f} MB | {sum(dead.values()) / MB:.2f} MB |")
    table = "\n".join(lines) + "\n"
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(table + "\n")


main()
