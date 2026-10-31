"""The Windows CLI, msi and core libraries, built in an MSYS2 UCRT64 shell."""

import os
import shutil
import struct
import tempfile
import uuid
import zipfile
from pathlib import Path, PureWindowsPath

from .common import REPO, BuildError, edit_lines, fetch_pinned, remove_tree, run, sha256, single_dir, single_file, touch_tree
from .desktop import (
    DESKTOP_ENV,
    DESKTOP_RELEASE_DIR,
    SOURCE_DATE_EPOCH,
    VLC_RELEASE,
    VLC_SHA256,
    WINDOWS_DESKTOP,
    Database,
    build_host_cli,
    check_exports,
    client_library_constraints,
    desktop_libs,
    link_desktop_libs,
    remove_skiko_runtime,
    run_cabal,
    zip_desktop_libs,
)
from .openssl import OpenSsl, check_static_openssl, host_openssl, openssl_work, static_libcrypto

# The system libraries that the static libcrypto of a MinGW build needs (ex_libs of OpenSSL's mingw-common)
OPENSSL_WINDOWS_STATIC_LINK = "-optl-lws2_32 -optl-lgdi32 -optl-lcrypt32"


# the linker writes the time of the link into each executable and DLL unless told not to
LINKER_NO_TIMESTAMP = "-Wl,--no-insert-timestamp"


WINDOWS_NO_TIMESTAMP = f"-optl{LINKER_NO_TIMESTAMP}"


# Compose's packageMsi makes an app image of its own; this makes it pack the one of createDistributable, as on macOS
MSI_FROM_APP_IMAGE_INIT = """gradle.allprojects { project ->
    project.tasks.configureEach { task ->
        if (task.name == "packageMsi") {
            def app = project.tasks.named("createDistributable")
            task.dependsOn(app)
            task.appImage.set(app.flatMap { it.destinationDir.dir(it.packageName) })
        }
    }
}
"""


# WiX gives each msi a random package code, which the build replaces with one derived from the msi's content
MSI_PLACEHOLDER_PACKAGE_CODE = "{00000000-0000-0000-0000-000000000000}"


MSI_PACKAGE_CODE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, REPO)


# The compound file header and the root storage's directory entry (MS-CFB 2.2, 2.6.1)
CFB_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")


CFB_HEADER_SIZE = 512


CFB_SECTOR_SHIFT_OFFSET = 0x1E


CFB_FIRST_DIRECTORY_SECTOR_OFFSET = 0x30


CFB_MODIFIED_TIME_OFFSET = 0x6C


# FILETIME counts 100 ns intervals from 1601
FILETIME_TICKS_PER_SECOND = 10_000_000


FILETIME_AT_UNIX_EPOCH = 11_644_473_600 * FILETIME_TICKS_PER_SECOND


# Windows Installer's automation interface has no type information for PowerShell, which reaches it with InvokeMember
MSI_SUMMARY_SCRIPT = """param([string]$Msi, [string]$PackageCode, [long]$Epoch)
$ErrorActionPreference = "Stop"
$RevisionNumber, $CreateTime, $LastSaveTime = 9, 12, 13
$time = [DateTimeOffset]::FromUnixTimeSeconds($Epoch).UtcDateTime
$properties = @(@($RevisionNumber, $PackageCode), @($CreateTime, $time), @($LastSaveTime, $time))
$installer = New-Object -ComObject WindowsInstaller.Installer
$summary = $installer.GetType().InvokeMember("SummaryInformation", "GetProperty", $null, $installer, @($Msi, $properties.Count))
foreach ($property in $properties) {
    [void]$summary.GetType().InvokeMember("Property", "SetProperty", $null, $summary, $property)
}
[void]$summary.GetType().InvokeMember("Persist", "InvokeMethod", $null, $summary, $null)
"""


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


def set_msi_summary(msi: Path, package_code: str, work: Path) -> None:
    """The msi's package code, and its creation and save times at SOURCE_DATE_EPOCH."""
    script = work / "msi-summary.ps1"
    script.write_text(MSI_SUMMARY_SCRIPT)
    msi_path = str(PureWindowsPath(msi))
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, msi_path, package_code, SOURCE_DATE_EPOCH])
    script.unlink()


def msi_package_code(msi: Path) -> str:
    """Windows Installer treats msi files with one package code as copies of one file, so the code is a UUID of the content."""
    return "{" + str(uuid.uuid5(MSI_PACKAGE_CODE_NAMESPACE, sha256(msi))).upper() + "}"


def set_msi_storage_time(msi: Path) -> None:
    """The modification time of the msi's root storage, which each commit sets to its own time, at SOURCE_DATE_EPOCH."""
    with msi.open("r+b") as f:
        header = f.read(CFB_HEADER_SIZE)
        if not header.startswith(CFB_SIGNATURE):
            raise BuildError(f"{msi} is not a compound file")
        sector_shift = struct.unpack_from("<H", header, CFB_SECTOR_SHIFT_OFFSET)[0]
        directory = struct.unpack_from("<I", header, CFB_FIRST_DIRECTORY_SECTOR_OFFSET)[0]
        # sector n starts after the header, which takes up sector -1
        f.seek(((directory + 1) << sector_shift) + CFB_MODIFIED_TIME_OFFSET)
        f.write(struct.pack("<Q", FILETIME_AT_UNIX_EPOCH + int(SOURCE_DATE_EPOCH) * FILETIME_TICKS_PER_SECOND))


def normalize_msi_summary(msi: Path, work: Path) -> None:
    set_msi_summary(msi, MSI_PLACEHOLDER_PACKAGE_CODE, work)
    set_msi_storage_time(msi)
    set_msi_summary(msi, msi_package_code(msi), work)
    set_msi_storage_time(msi)


def package_msi(source: Path, work: Path) -> Path:
    """jpackage's msi of the app image without the skiko runtime jar and with fixed times, which the msi's cabinet keeps."""
    multiplatform = source / "apps/multiplatform"
    # gradlew.bat, as Windows finds its WSL bash.exe before MSYS2's
    gradle: list[str | Path] = [multiplatform / "gradlew.bat", f"-Psimplex.assets.dir={DESKTOP_ENV['ASSETS_DIR']}"]
    # CMake links libapp-lib.dll with LDFLAGS
    env = {"LDFLAGS": LINKER_NO_TIMESTAMP}
    run([*gradle, "createDistributable"], cwd=multiplatform, env=env)
    app = single_dir(source / DESKTOP_RELEASE_DIR / "app", "*imple*")
    remove_skiko_runtime(app / "app")
    touch_tree(app, SOURCE_DATE_EPOCH)
    init = work / "msi-from-app-image.gradle"
    init.write_text(MSI_FROM_APP_IMAGE_INIT)
    # packageMsi would otherwise create the edited app image again
    run([*gradle, "--init-script", init, "packageMsi", "-x", "createDistributable"], cwd=multiplatform, env=env)
    init.unlink()
    msi = single_file(source / DESKTOP_RELEASE_DIR / "msi", "*imple*.msi")
    normalize_msi_summary(msi, work)
    return msi


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
        shutil.copy(package_msi(source, work), out / host.desktop)
