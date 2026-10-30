"""What the Linux, macOS and Windows desktop builds share."""

import enum
import os
import platform
import re
import shutil
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .common import BuildError, Platform, host_platform, remove_tree, run
from .docker import Step

DESKTOP_ENV = {"ASSETS_DIR": "../../assets"}


DESKTOP_RELEASE_DIR = "apps/multiplatform/release/main"


SOURCE_DATE_EPOCH = "1704067200"  # the desktop releases' timestamps since v5.4


DESKTOP_LIBS_DIR = "apps/multiplatform/common/src/commonMain/cpp/desktop/libs"


COMPOSE_ARCHES = {"x86_64": "x64", "aarch64": "arm64"}


# Android only: Mobile.hs exports them, the desktop library doesn't
ANDROID_ONLY_EXPORTS = ("chat_migrate_init_key", "chat_reopen_store")


VLC_RELEASE = "https://github.com/simplex-chat/vlc/releases/download/v3.0.23-2"


VLC_SHA256 = {
    "vlc-linux-x86_64.appimage": "594fc3a603167a977cc0de519e1d5f785037df0db60c57518f88e46606d5bb98",
    "vlc-linux-aarch64.appimage": "697e3d1d0eaad0729529826021fd78b10eee411ba413866c5ab24ac4ac754ba4",
    "vlc-macos-aarch64.zip": "ee537e339b4b40546dc9374db4a2e74eeb46d4b76fb7f23b201e7beb144decb8",
    "vlc-macos-x86_64.zip": "a4a782b8d2f5475ae7a7dda84a2ba4989d415cd4c0a7d19c68e75a90920a0d6b",
    "vlc-win-x86_64.zip": "09993fc4e6bf8239cf847f943ae593dd62ba9abbe5f2f18c42bfca1bc9e94443",
}


class Database(enum.StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


def client_library_constraints(database: Database) -> tuple[str, ...]:
    flags = "+client_library +client_postgres" if database == Database.POSTGRES else "+client_library"
    return ("--constraint", f"simplexmq {flags}", "--constraint", f"simplex-chat {flags}")


def check_exports(source: Path) -> None:
    """Each function that Mobile.hs exports to the desktop library is in libsimplex.dll.def and flake.nix once."""
    exports = re.findall(r'foreign export ccall "([^"]+)"', (source / "src/Simplex/Chat/Mobile.hs").read_text())
    definitions = (source / "libsimplex.dll.def").read_text().splitlines()
    flake = (source / "flake.nix").read_text().splitlines()
    for name in exports:
        if name in ANDROID_ONLY_EXPORTS:
            continue
        if sum(line.endswith(name) for line in definitions) != 1:
            raise BuildError(f'wrong exports in libsimplex.dll.def: add "{name}" to it')
        if sum(f'"{name}"' in line for line in flake) != 1:
            raise BuildError(f'wrong exports in flake.nix: add "{name}" to aarch64-android:lib:simplex-chat')


def desktop_libs(source: Path, platform_dir: str) -> Path:
    return source / DESKTOP_LIBS_DIR / platform_dir


def link_desktop_libs(source: Path, platform_dir: str, compose_dir: str) -> None:
    """build/links/<compose_dir>, where the desktop app's Gradle build finds the libraries."""
    links = source / "apps/multiplatform/build/links"
    links.mkdir(parents=True, exist_ok=True)
    link = links / compose_dir
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    target = f"../../common/src/commonMain/cpp/desktop/libs/{platform_dir}/"
    if os.name == "nt":
        # as MSYS2's ln -s, which copies unless Windows symlinks are enabled
        shutil.copytree(desktop_libs(source, platform_dir), link)
    else:
        link.symlink_to(target)


CLI_BUILD_STEPS = (Step(("cabal", "clean")), Step(("cabal", "update")), Step(("cabal", "build", "-j")))


@dataclass(frozen=True, slots=True)
class HostDesktop:
    """The release files of a macOS or Windows machine, and the OpenSSL target that builds for it."""

    arch: str
    openssl_target: str
    cli: str
    desktop: str
    libs: str
    libs_dir: str


MACOS_DESKTOPS = {
    "arm64": HostDesktop(
        "aarch64",
        "darwin64-arm64-cc",
        "simplex-chat-macos-aarch64",
        "simplex-desktop-macos-aarch64.dmg",
        "simplex-chat-libs-macos-aarch64.zip",
        "mac-aarch64",
    ),
    "x86_64": HostDesktop(
        "x86_64",
        "darwin64-x86_64-cc",
        "simplex-chat-macos-x86-64",
        "simplex-desktop-macos-x86_64.dmg",
        "simplex-chat-libs-macos-x86_64.zip",
        "mac-x86_64",
    ),
}


WINDOWS_DESKTOP = HostDesktop(
    "x86_64",
    "mingw64",
    "simplex-chat-windows-x86-64",
    "simplex-desktop-windows-x86_64.msi",
    "simplex-chat-libs-windows-x86_64.zip",
    "windows-x86_64",
)


def macos_desktop() -> HostDesktop:
    host = MACOS_DESKTOPS.get(platform.machine())
    if host is None:
        raise BuildError(f"no macOS build for {platform.machine()}")
    return host


def remove_cabal_sources(source: Path) -> None:
    """cabal fails on Windows when these checkouts exist, and can't delete simplexmq's read-only git pack files."""
    for path in [*source.glob("dist-newstyle/src/direct-sq*"), *source.glob("dist-newstyle/src/simplexmq*")]:
        remove_tree(path)


def build_host_cli(source: Path, cabal_local: str) -> Path:
    (source / "cabal.project.local").write_text(cabal_local)
    # in place of cabal clean, which can't delete the read-only git pack files of its checkouts on Windows
    remove_tree(source / "dist-newstyle")
    update, build = CLI_BUILD_STEPS[1:]
    run(update.argv, cwd=source)
    run_cabal(source, build.argv[1:])
    return Path(run_cabal(source, ["list-bin", "exe:simplex-chat"], capture=True).strip().splitlines()[-1])


def run_cabal(source: Path, args: Sequence[str], capture: bool = False) -> str:
    if host_platform() == Platform.WINDOWS:
        # each cabal command updates the checkouts that an earlier one left, which fails on Windows
        remove_cabal_sources(source)
    return run(["cabal", *args], cwd=source, capture=capture).stdout or ""


def zip_desktop_libs(libs: Path, extra: Sequence[Path], target: Path) -> None:
    """The library release: the files directly in the desktop app's libraries directory, under libs/."""
    files = sorted([p for p in libs.iterdir() if p.is_file()] + list(extra))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files:
            z.write(path, f"libs/{path.name}")
