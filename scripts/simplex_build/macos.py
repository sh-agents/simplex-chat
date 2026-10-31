"""The macOS CLI, dmg and core libraries, signed and notarized with Apple's keys when they are given."""

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import struct
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from .apk_zip import LOCAL_HEADER, central_directory
from .common import GHC, BuildError, edit_lines, fetch_pinned, run, single_dir, single_file, touch_tree, warn
from .desktop import (
    COMPOSE_ARCHES,
    DESKTOP_ENV,
    DESKTOP_RELEASE_DIR,
    SOURCE_DATE_EPOCH,
    VLC_RELEASE,
    VLC_SHA256,
    Database,
    build_host_cli,
    check_exports,
    client_library_constraints,
    desktop_libs,
    link_desktop_libs,
    macos_desktop,
    zip_desktop_libs,
)
from .dmg import build_dmg, build_libdmg
from .openssl import OpenSsl, check_static_openssl, host_openssl, openssl_work, static_libcrypto

APPLE_TEAM = "5NN7GUYB6T"


APPLE_IDENTITY = f"Developer ID Application: SimpleX Chat Ltd ({APPLE_TEAM})"


LIBFFI_URL = "https://github.com/libffi/libffi/releases/download/v3.5.2/libffi-3.5.2.tar.gz"


LIBFFI_SHA256 = "f3a3082a23b37c293a4fcd1053147b371f2ff91fa7ea1b2a52e335676bac82dc"


# the jars that Compose copies into the app, named with a hash of the jar it copied, and the classpath that lists them
COMPOSE_JAR_NAME = re.compile(r"(?P<name>.+)-[0-9a-f]{1,32}\.jar")


CLASSPATH_PREFIX = "app.classpath=$APPDIR/"


DOS_EPOCH_YEAR = 1980


# the metadata of Compose's signature that the app keeps when it is signed again
PRESERVED_SIGNATURE = "--preserve-metadata=entitlements,flags,runtime"


APPLE_SIGNING_ENV = ("APPLE_SIMPLEX_SIGNING_KEYCHAIN", "APPLE_SIMPLEX_NOTARIZATION_APPLE_ID", "APPLE_SIMPLEX_NOTARIZATION_PASSWORD")


def apple_signing(require: bool) -> bool:
    """Whether the APPLE_SIGNING_ENV variables, which the keychain, signing and notarization read, are all set."""
    present = [name for name in APPLE_SIGNING_ENV if os.environ.get(name)]
    if len(present) == len(APPLE_SIGNING_ENV):
        return True
    names = ", ".join(APPLE_SIGNING_ENV)
    if present:
        raise BuildError(f"set all of {names} to sign, or none; set: {', '.join(present)}")
    if require:
        raise BuildError(f"{names} are not set, and --require-signing is given")
    warn(f"{names} are not set: building an unsigned, not notarized dmg and libraries")
    return False


def otool(flag: str, lib: Path) -> list[str]:
    """What otool prints about lib, without its first line, which is lib's path."""
    return run(["otool", flag, lib], capture=True).stdout.splitlines()[1:]


def rpath_dependencies(lib: Path) -> list[str]:
    """The names of the libraries that lib loads from its rpaths; the first @rpath line is lib's own id."""
    return [line.strip().split(" ")[0].split("/")[1] for line in otool("-L", lib) if "@rpath" in line][1:]


def rpaths(lib: Path) -> list[str]:
    return [line.split()[1] for line in otool("-l", lib) if "path " in line]


def libcrypto_path(lib: Path) -> str | None:
    return next((line.split()[1] for line in otool("-l", lib) if "libcrypto" in line), None)


def build_libffi(work: Path) -> Path:
    """libffi.dylib, which GHC's libraries load but macOS doesn't ship."""
    with tarfile.open(fetch_pinned(LIBFFI_URL, LIBFFI_SHA256, work / "libffi.tar.gz")) as tar:
        tar.extractall(work, filter="data")
    source = work / "libffi-3.5.2"
    run(["./configure"], cwd=source)
    run(["make"], cwd=source)
    return single_file(source, "*-apple-darwin*/.libs/libffi.dylib")


def copy_mac_dependencies(lib: Path, deps: Path, ghc_libdir: Path, copied: set[Path]) -> None:
    """lib and the libraries it loads from its rpaths, recursively; GHC's libraries find each other at @loader_path."""
    if lib in copied:
        return
    copied.add(lib)
    own_rpaths = rpaths(lib)
    shutil.copy(lib, deps)
    if any("@loader_path/.." in rpath for rpath in own_rpaths):
        run(["install_name_tool", "-add_rpath", "@loader_path", deps / lib.name])
    search = [Path(rpath.replace("@loader_path/..", str(ghc_libdir), 1)) for rpath in own_rpaths]
    for name in rpath_dependencies(lib):
        for directory in search:
            dependency = directory / name
            if dependency.is_file():
                if not (deps / name).is_file():
                    shutil.copy(dependency, deps)
                copy_mac_dependencies(dependency, deps, ghc_libdir, copied)


def bundle_mac_libcrypto(libs: Path, database: Database) -> None:
    """Copies the libcrypto that the libraries link into libs, and points them at the copy and their rpaths at libs."""
    smplxmq = single_file(libs, "libHSsmplxmq-*.dylib")
    libcrypto = libcrypto_path(smplxmq)
    if libcrypto is None:
        raise BuildError(f"{smplxmq.name} doesn't load libcrypto")
    bundled = "libcrypto.3.0.dylib"
    shutil.copy(libcrypto, libs / bundled)
    (libs / bundled).chmod(0o755)
    run(["install_name_tool", "-id", bundled, libs / bundled])
    run(["install_name_tool", "-id", "libffi.8.dylib", libs / "libffi.dylib"])
    patterns = ["libHSsmplxmq-*.dylib", "libsimplex.dylib"]
    if database == Database.SQLITE:
        patterns += ["libHSdrct-*.dylib", "libHSsqlcphr-*.dylib"]
    for lib in (lib for pattern in patterns for lib in libs.glob(pattern)):
        linked = libcrypto_path(lib)
        if linked:
            run(["install_name_tool", "-change", linked, f"@rpath/{bundled}", lib])
    dylibs = sorted(p for p in libs.rglob("*.dylib") if p.is_file())
    for lib in dylibs:
        for rpath in rpaths(lib):
            if rpath.startswith(("/Users/", "/usr/local", "/opt/")):
                run(["install_name_tool", "-delete_rpath", rpath, lib])
    local = [lib.name for lib in dylibs if any(re.search("/Users|/opt/|/usr/local", line) for line in otool("-l", lib))]
    if local:
        message = f"these libraries still point to local directories: {', '.join(local)}"
        # the postgres libraries are not distributed
        if database == Database.SQLITE:
            raise BuildError(message)
        warn(message)


def build_mac_lib(source: Path, arch: str, database: Database, work: Path) -> Path:
    """libsimplex.dylib and the libraries it loads, in the desktop app's libraries directory, which it returns."""
    check_exports(source)
    ghc_libdir = Path(run(["ghc", "--print-libdir"], capture=True).stdout.strip())
    rts_dir = ghc_libdir / f"{arch}-osx-ghc-{GHC}"
    for old in source.glob(f"dist-newstyle/build/{arch}-*/ghc-*/simplex-chat-*"):
        shutil.rmtree(old)
    options = (
        f"-optl-Wl,-rpath,@loader_path -optl-Wl,-install_name,@rpath/libsimplex.dylib -optl-Wl,-L{rts_dir} "
        f"-optl-lHSrts_thr-ghc{GHC} -optl-lffi"
    )
    run(["cabal", "build", "lib:simplex-chat", f"--ghc-options={options}", *client_library_constraints(database)], cwd=source)
    build = single_dir(source, f"dist-newstyle/build/{arch}-*/ghc-*/simplex-chat-*/build")
    for lib in build.glob("libHSsimplex-chat-*-inplace-ghc*.dylib"):
        lib.rename(build / "libsimplex.dylib")
    deps = build / "deps"
    deps.mkdir(exist_ok=True)
    shutil.copy(build_libffi(work), deps / "libffi.dylib")
    copy_mac_dependencies(build / "libsimplex.dylib", deps, ghc_libdir, set())
    shutil.copy(rts_dir / f"libHSghc-boot-th-{GHC}-ghc{GHC}.dylib", deps)
    (deps / "libsimplex.dylib").unlink()
    libs = desktop_libs(source, f"mac-{arch}")
    shutil.rmtree(libs, ignore_errors=True)
    shutil.rmtree(source / "apps/multiplatform/desktop/build/cmake", ignore_errors=True)
    libs.mkdir(parents=True)
    for path in [*deps.iterdir(), build / "libsimplex.dylib"]:
        shutil.copy(path, libs)
    bundle_mac_libcrypto(libs, database)
    return libs


def add_vlc_mac(libs: Path, arch: str, work: Path) -> None:
    vlc = libs / "vlc"
    (vlc / "vlc").mkdir(parents=True, exist_ok=True)
    name = f"vlc-macos-{arch}.zip"
    extracted = work / "vlc"
    extracted.mkdir()
    run(["unzip", "-oqq", fetch_pinned(f"{VLC_RELEASE}/{name}", VLC_SHA256[name], work / name)], cwd=extracted)
    run(["install_name_tool", "-add_rpath", "@loader_path/VLC.app/Contents/MacOS/lib", "vlc-cache-gen"], cwd=extracted)
    macos = extracted / "VLC.app/Contents/MacOS"
    for lib in sorted((macos / "lib").glob("*.dylib")):
        run(["install_name_tool", "-add_rpath", "@loader_path", lib], check=False, capture=True)
    for lib in sorted((macos / "plugins").glob("*.dylib")):
        run(["install_name_tool", "-add_rpath", "@loader_path/../../", lib], check=False, capture=True)
    # the plugin cache records each plugin's modification time
    touch_tree(macos / "plugins", SOURCE_DATE_EPOCH)
    run([extracted / "vlc-cache-gen", "plugins"], cwd=macos)
    for path in (macos / "lib").iterdir():
        shutil.copy(path, vlc)
    shutil.copytree(macos / "plugins", vlc / "vlc/plugins", symlinks=True, dirs_exist_ok=True)


@contextlib.contextmanager
def apple_keychain(work: Path) -> Iterator[Path]:
    """The keychain of APPLE_SIMPLEX_SIGNING_KEYCHAIN, unlocked and in the search list, removed on exit."""
    keychain = work / "simplex.keychain"
    keychain.write_bytes(base64.b64decode(os.environ["APPLE_SIMPLEX_SIGNING_KEYCHAIN"]))
    searched = [line.strip().strip('"') for line in run(["security", "list-keychains"], capture=True).stdout.splitlines()]
    run(["security", "unlock-keychain", "-p", "", keychain])
    # codesign finds the certificate in a keychain outside the search list, then fails to sign with it
    run(["security", "list-keychains", "-s", *searched, keychain])
    try:
        yield keychain
    finally:
        run(["security", "list-keychains", "-s", *searched])
        keychain.unlink()


def notarize(path: Path, work: Path) -> None:
    """Submits a dmg, or a zip of another path, to Apple's notary service, fails unless it is accepted, and staples the
    ticket to a dmg."""
    dmg = path.suffix == ".dmg"
    archive = path if dmg else work / f"{path.name}.zip"
    if not dmg:
        run(["ditto", "-c", "-k", "--keepParent", path, archive])
    credentials = [
        "--apple-id",
        os.environ["APPLE_SIMPLEX_NOTARIZATION_APPLE_ID"],
        "--password",
        os.environ["APPLE_SIMPLEX_NOTARIZATION_PASSWORD"],
        "--team-id",
        APPLE_TEAM,
    ]
    submit: list[str | Path] = ["xcrun", "notarytool", "submit", archive, "--wait", "--timeout", "1h", "--output-format", "json"]
    result = json.loads(run([*submit, *credentials], capture=True, check=False, title=" ".join(map(str, submit))).stdout)
    run(["xcrun", "notarytool", "log", result["id"], *credentials], title=f"xcrun notarytool log {result['id']}")
    if result["status"] != "Accepted":
        raise BuildError(f"notarization of {path.name} is {result['status']}")
    if dmg:
        run(["xcrun", "stapler", "staple", path])


def sign_mac_libs(libs: Path, keychain: Path, work: Path) -> None:
    for lib in sorted(libs.glob("*.dylib")):
        run(["codesign", "--remove-signature", lib])
        run(["codesign", "--timestamp", "--options", "runtime", "--force", "--sign", APPLE_IDENTITY, "--keychain", keychain, lib])
    notarize(libs, work)


def build_app(source: Path, keychain: Path | None) -> Path:
    """The desktop app, SimpleX.app, signed by Compose with the keychain or else as Compose signs unsigned apps."""
    multiplatform = source / "apps/multiplatform"
    properties = multiplatform / "local.properties"
    if keychain:
        # Compose signs only when the notarization properties are also set
        properties.write_text(
            f"desktop.mac.signing.identity={APPLE_IDENTITY}\n"
            f"desktop.mac.signing.keychain={keychain}\n"
            f"desktop.mac.notarization.apple_id={os.environ['APPLE_SIMPLEX_NOTARIZATION_APPLE_ID']}\n"
            f"desktop.mac.notarization.password={os.environ['APPLE_SIMPLEX_NOTARIZATION_PASSWORD']}\n"
            f"desktop.mac.notarization.team_id={APPLE_TEAM}\n"
        )
    try:
        run(["./gradlew", f"-Psimplex.assets.dir={DESKTOP_ENV['ASSETS_DIR']}", "createDistributable"], cwd=multiplatform)
    finally:
        properties.unlink(missing_ok=True)
    return single_dir(source / DESKTOP_RELEASE_DIR / "app", "*.app")


def dos_date_time(epoch: str) -> bytes:
    t = time.gmtime(int(epoch))
    dos_time = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    dos_date = ((t.tm_year - DOS_EPOCH_YEAR) << 9) | (t.tm_mon << 5) | t.tm_mday
    return struct.pack("<HH", dos_time, dos_date)


def normalize_jar(jar: Path, epoch: str) -> None:
    """Dates each entry of jar with epoch, in its local and central headers."""
    data = bytearray(jar.read_bytes())
    date_time = dos_date_time(epoch)
    for entry in central_directory(bytes(data)):
        if data[entry.local_offset : entry.local_offset + 4] != LOCAL_HEADER:
            raise BuildError(f"no local header for {entry.name} in {jar.name}")
        data[entry.central_offset + 12 : entry.central_offset + 16] = date_time
        data[entry.local_offset + 10 : entry.local_offset + 14] = date_time
    jar.write_bytes(data)


def normalize_app(app: Path) -> None:
    """Dates the entries of the jars, which Compose packs again with the current time, with SOURCE_DATE_EPOCH, and names
    each jar, in the app and its classpath, with a hash of its content in place of Compose's hash."""
    jars = app / "Contents/app"
    renamed = {}
    for jar in sorted(jars.glob("*.jar")):
        normalize_jar(jar, SOURCE_DATE_EPOCH)
        match = COMPOSE_JAR_NAME.fullmatch(jar.name)
        if match:
            with jar.open("rb") as f:
                digest = hashlib.file_digest(f, lambda: hashlib.md5(usedforsecurity=False)).hexdigest()
            renamed[jar.name] = f"{match['name']}-{digest}.jar"
            jar.rename(jars / renamed[jar.name])

    def rename_classpath(line: str) -> str:
        name = line.removeprefix(CLASSPATH_PREFIX)
        return CLASSPATH_PREFIX + renamed[name] if line.startswith(CLASSPATH_PREFIX) and name in renamed else line

    edit_lines(jars / f"{app.stem}.cfg", rename_classpath)


def sign_app(app: Path, keychain: Path | None) -> None:
    """Signs the bundle again after normalize_app, with the keychain's identity or, when Compose signed it ad hoc, ad hoc,
    keeping the entitlements and options of Compose's signature."""
    if keychain:
        run(["codesign", "--force", PRESERVED_SIGNATURE, "--timestamp", "--sign", APPLE_IDENTITY, "--keychain", keychain, app])
    elif (app / "Contents/_CodeSignature").is_dir():
        run(["codesign", "--force", PRESERVED_SIGNATURE, "--sign", "-", app])


def sign_dmg(dmg: Path, keychain: Path, work: Path) -> None:
    """Signs dmg with the keychain's identity, then notarizes it."""
    run(["codesign", "--timestamp", "--sign", APPLE_IDENTITY, "--keychain", keychain, dmg])
    notarize(dmg, work)


def java_home() -> Path:
    home = os.environ.get("JAVA_HOME")
    if not home:
        raise BuildError("JAVA_HOME is not set: set it to the JDK that builds the app")
    return Path(home)


def release_macos_libs(
    source: Path, openssl: OpenSsl, database: Database, keychain: Path | None, work: Path, out: Path
) -> Path:
    """The library release, signed and notarized with the keychain; returns the libraries directory of the app,
    linked for its Gradle build."""
    host = macos_desktop()
    # bundle_mac_libcrypto copies the libcrypto that the libraries link into the app
    (source / "cabal.project.local").write_text(openssl.cabal_local())
    libs = build_mac_lib(source, host.arch, database, work)
    link_desktop_libs(source, host.libs_dir, f"macos-{COMPOSE_ARCHES[host.arch]}")
    released = work / "libs"
    shutil.copytree(libs, released)
    if keychain:
        sign_mac_libs(released, keychain, work)
    name = host.libs.replace(".zip", "-postgres.zip") if database == Database.POSTGRES else host.libs
    zip_desktop_libs(released, [], out / name)
    return libs


def build_macos_libs(source: Path, database: Database, out: Path, require_signing: bool) -> None:
    signed = apple_signing(require_signing)
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        work = Path(tmp).resolve()
        keychain = stack.enter_context(apple_keychain(work)) if signed else None
        release_macos_libs(source, host_openssl(openssl_work(source)), database, keychain, work, out)


def build_desktop_macos(source: Path, out: Path, require_signing: bool) -> None:
    host = macos_desktop()
    signed = apple_signing(require_signing)
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        work = Path(tmp).resolve()
        keychain = stack.enter_context(apple_keychain(work)) if signed else None
        openssl_dir = openssl_work(source)
        openssl = host_openssl(openssl_dir)
        static = OpenSsl(openssl.include, static_libcrypto(openssl.lib, openssl_dir))
        cli = build_host_cli(source, static.cabal_local())
        check_static_openssl(cli, run(["otool", "-L", cli], capture=True).stdout)
        shutil.copy(cli, out / host.cli)
        libs = release_macos_libs(source, openssl, Database.SQLITE, keychain, work, out)
        add_vlc_mac(libs, host.arch, work)
        app = build_app(source, keychain)
        normalize_app(app)
        sign_app(app, keychain)
        dmg = work / host.desktop
        build_dmg(build_libdmg(work), app, java_home(), work, dmg)
        if keychain:
            sign_dmg(dmg, keychain, work)
        shutil.copy(dmg, out / host.desktop)
