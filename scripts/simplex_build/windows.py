"""The Windows CLI, msi and core libraries, built in an MSYS2 UCRT64 shell."""

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from .common import edit_lines, fetch_pinned, remove_tree, run, single_dir, single_file
from .desktop import (
    DESKTOP_ENV,
    DESKTOP_RELEASE_DIR,
    VLC_RELEASE,
    VLC_SHA256,
    WINDOWS_DESKTOP,
    Database,
    build_host_cli,
    check_exports,
    client_library_constraints,
    desktop_libs,
    link_desktop_libs,
    run_cabal,
    zip_desktop_libs,
)
from .openssl import OpenSsl, check_static_openssl, host_openssl, openssl_work, static_libcrypto

# The system libraries that the static libcrypto of a MinGW build needs (ex_libs of OpenSSL's mingw-common)
OPENSSL_WINDOWS_STATIC_LINK = "-optl-lws2_32 -optl-lgdi32 -optl-lcrypt32"


# the linker writes the time of the link into each executable and DLL unless told not to
LINKER_NO_TIMESTAMP = "-Wl,--no-insert-timestamp"


WINDOWS_NO_TIMESTAMP = f"-optl{LINKER_NO_TIMESTAMP}"


def disable_ghc_lld() -> None:
    """With GHC's lld, linking libsimplex.dll can't find the exported symbols; a missing linker makes GHC use ld."""
    settings = Path(run(["ghc", "--print-libdir"], capture=True).stdout.strip()) / "settings"
    edit_lines(settings, lambda line: line.replace("ld.lld.exe", "abracadabra.exe", 1))


def build_windows_lib(source: Path, openssl: OpenSsl) -> Path:
    """libsimplex.dll and the libcrypto it loads, in the desktop app's libraries directory, which it returns."""
    check_exports(source)
    for old in source.glob("dist-newstyle/build/x86_64-windows/ghc-*/simplex-chat-*"):
        remove_tree(old)
    options = (
        f"-shared -threaded {WINDOWS_NO_TIMESTAMP} -optl-L{openssl.lib.as_posix()} -optl-lcrypto-3-x64 "
        "-o libsimplex.dll libsimplex.dll.def"
    )
    (source / "cabal.project.local").write_text(openssl.cabal_local(options))
    disable_ghc_lld()
    run_cabal(source, ["build", "lib:simplex-chat", *client_library_constraints(Database.SQLITE)])
    libs = desktop_libs(source, WINDOWS_DESKTOP.libs_dir)
    shutil.rmtree(libs, ignore_errors=True)
    shutil.rmtree(source / "apps/multiplatform/desktop/build/cmake", ignore_errors=True)
    libs.mkdir(parents=True)
    shutil.copy(openssl.lib / "libcrypto-3-x64.dll", libs)
    shutil.copy(source / "libsimplex.dll", libs)
    return libs


def add_vlc_windows(libs: Path, work: Path) -> None:
    vlc = libs / "vlc"
    shutil.rmtree(vlc, ignore_errors=True)
    (vlc / "vlc").mkdir(parents=True)
    name = "vlc-win-x86_64.zip"
    extracted = work / "vlc"
    with zipfile.ZipFile(fetch_pinned(f"{VLC_RELEASE}/{name}", VLC_SHA256[name], work / name)) as z:
        z.extractall(extracted)
    root = single_dir(extracted, "vlc-*")
    # the plugin cache must hold the time the plugins get when the app extracts them from its jar
    for plugin in (p for p in (root / "plugins").rglob("*") if ".dll" in p.name):
        os.utime(plugin, (plugin.stat().st_atime, 0))
    run([root / "vlc-cache-gen.exe", "plugins"], cwd=root)
    for dll in root.glob("*.dll"):
        shutil.copy(dll, vlc)
    shutil.copytree(root / "plugins", vlc / "vlc/plugins", dirs_exist_ok=True)


def release_windows_libs(source: Path, openssl: OpenSsl, out: Path) -> Path:
    """The library release, with the import library of libsimplex.dll; returns the libraries directory of the app."""
    libs = build_windows_lib(source, openssl)
    run(["dlltool", "-d", "libsimplex.dll.def", "-l", "libsimplex.lib", "-D", "libsimplex.dll"], cwd=source)
    zip_desktop_libs(libs, [source / "libsimplex.lib"], out / WINDOWS_DESKTOP.libs)
    return libs


def build_windows_libs(source: Path, out: Path) -> None:
    release_windows_libs(source, host_openssl(openssl_work(source)), out)


def build_desktop_windows(source: Path, out: Path) -> None:
    host = WINDOWS_DESKTOP
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp).resolve()
        openssl_dir = openssl_work(source)
        openssl = host_openssl(openssl_dir)
        static = OpenSsl(openssl.include, static_libcrypto(openssl.lib, openssl_dir))
        cli = build_host_cli(source, static.cabal_local(f"{OPENSSL_WINDOWS_STATIC_LINK} {WINDOWS_NO_TIMESTAMP}"))
        check_static_openssl(cli, run(["objdump", "-p", cli], capture=True).stdout)
        shutil.copy(cli, out / host.cli)
        libs = release_windows_libs(source, openssl, out)
        add_vlc_windows(libs, work)
        link_desktop_libs(source, host.libs_dir, "windows-x64")
        assets = f"-Psimplex.assets.dir={DESKTOP_ENV['ASSETS_DIR']}"
        # gradlew.bat, as Windows finds its WSL bash.exe before MSYS2's
        multiplatform = source / "apps/multiplatform"
        # CMake links libapp-lib.dll with LDFLAGS
        run([multiplatform / "gradlew.bat", assets, "packageMsi"], cwd=multiplatform, env={"LDFLAGS": LINKER_NO_TIMESTAMP})
        shutil.copy(single_file(source / DESKTOP_RELEASE_DIR / "msi", "*imple*.msi"), out / host.desktop)
