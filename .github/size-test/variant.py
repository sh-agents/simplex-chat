#!/usr/bin/env python3
"""Changes the sources for a size variant.
Usage: variant.py osize   Swift -Osize in the Release configurations that use -O (project and SimpleXChat)
       variant.py core-o1 the iOS core's simplex-chat and simplexmq at -O1, after their cabal -O2"""

import re
import sys
from pathlib import Path

PBXPROJ = Path("apps/ios/SimpleX.xcodeproj/project.pbxproj")
FLAKE = Path("flake.nix")
RELEASE_CONFIGS = ("5CA059F2279559F40002BEB4", "5CE2BA742845308900EC33A6")


def osize() -> None:
    text = PBXPROJ.read_text()
    for config in RELEASE_CONFIGS:
        block = re.search(rf"\t\t{config} /\* Release \*/ = \{{.*?\n\t\t\}};", text, re.S)
        assert block, config
        old = block.group(0)
        new = old.replace('SWIFT_OPTIMIZATION_LEVEL = "-O";', 'SWIFT_OPTIMIZATION_LEVEL = "-Osize";')
        assert new != old, f"{config} has no -O"
        text = text.replace(old, new)
    PBXPROJ.write_text(text)


def core_o1() -> None:
    text = FLAKE.read_text()
    old = 'ghcOptions = [ "-staticlib" "-threaded" "-DIOS" "-j1" ];'
    assert text.count(old) == 1
    text = text.replace(old, 'ghcOptions = [ "-staticlib" "-threaded" "-DIOS" "-j1" "-O1" ];')
    anchor = '"aarch64-darwin-ios:lib:simplex-chat" = (drv\' {\n                pkgs\' = pkgs;\n                extra-modules = [{\n'
    assert text.count(anchor) == 1
    text = text.replace(anchor, anchor + '                  packages.simplexmq.ghcOptions = [ "-j1" "-O1" ];\n')
    FLAKE.write_text(text)


{"osize": osize, "core-o1": core_o1}[sys.argv[1]]()
