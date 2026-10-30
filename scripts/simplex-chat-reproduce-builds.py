#!/usr/bin/env python3

"""Reproduces SimpleX Chat release builds in Docker and compares them with the released files.

Reproduce a release (desktop is the default target):
    simplex-chat-reproduce-builds.py <tag> [desktop] [android] [--arches aarch64,armv7a] [--nix-cache]

Build one commit's apps unsigned, as CI's build-apps workflow does:
    simplex-chat-reproduce-builds.py build android-apk <aarch64|armv7a> --out <dir> [--source <dir>] [--nix-cache]
    simplex-chat-reproduce-builds.py build android-bundle --libs <dir> --out <dir> [--source <dir>]
    simplex-chat-reproduce-builds.py build desktop-linux <22.04|24.04> --version <x.y.z> --out <dir> [--source <dir>]
    simplex-chat-reproduce-builds.py build ios-archive --art <simplex-chat-art> --out <dir> [--source <dir>] [--install-nix]

Needs Python 3.9+, git and Docker; ios-archive needs macOS with Xcode and Nix. Results go to ./<tag>-simplex-chat.
"""

from __future__ import annotations

import argparse
import contextlib
import enum
import hashlib
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Union

REPO = "https://github.com/simplex-chat/simplex-chat"
GHC = "9.6.3"


class ReproduceError(Exception):
    pass


class Target(str, enum.Enum):
    DESKTOP = "desktop"
    ANDROID = "android"

    def __str__(self) -> str:
        return self.value


class Arch(str, enum.Enum):
    AARCH64 = "aarch64"
    ARMV7A = "armv7a"

    def __str__(self) -> str:
        return self.value

    @property
    def abi(self) -> str:
        return {Arch.AARCH64: "arm64-v8a", Arch.ARMV7A: "armeabi-v7a"}[self]


@dataclass(frozen=True)
class UbuntuImage:
    version: str
    digest: str

    @property
    def suffix(self) -> str:
        return self.version.replace(".", "_")


# The Ubuntu images of build.yml's release builds
DESKTOP_IMAGES = (
    UbuntuImage("22.04", "sha256:5c8b2c0a6c745bc177669abfaa716b4bc57d58e2ea3882fb5da67f4d59e3dda5"),
    UbuntuImage("24.04", "sha256:98ff7968124952e719a8a69bb3cccdd217f5fe758108ac4f21ad22e1df44d237"),
)
APPIMAGE_IMAGE = "22.04"
DESKTOP_CABAL_LOCAL = "ignore-project: False\npackage direct-sqlcipher\n    flags: +openssl"

# SHA-256 of the certificate that signs the released APKs
SIMPLEX_KEY = "3C:52:C4:FD:3C:AD:1C:07:C9:B0:0A:70:80:E3:58:FA:B9:FE:FC:B8:AF:5A:EC:14:77:65:F1:6D:0F:21:AD:85"
ANDROID_LIBS_DIR = "apps/multiplatform/common/src/commonMain/cpp/android/libs"
ANDROID_LIBS = ("libsimplex", "libsupport")
# The local.properties of release-mobile.py's builds
ANDROID_LOCAL_PROPERTIES = {
    "compression.level": "5",
    "simplex.assets.dir": "../../assets",
}
# zw3rk's Hydra builds the Android jobs of master and IOG caches the haskell.nix toolchains; without them the core
# library takes 12-24 hours. Substituted paths are the same as built ones, but come from these caches' builders.
NIX_CACHE_CONFIG = (
    "extra-substituters = https://cache.zw3rk.com https://cache.iog.io\n"
    "extra-trusted-public-keys = loony-tools:pr9m4BkM/5/eSTZlkQyRt57Jz7OMBxNSUiMC4FkcNfk= "
    "hydra.iohk.io:f/Ea+s+dFdN+3Y/G+FDgSq+a5NEWhJGzdjvKNGv0/EQ=\n"
    "cores = 0"
)

# the 2.22 installer gives build users UIDs from 301, which macOS 15 and later reserve
NIX_INSTALLER = "https://releases.nixos.org/nix/nix-2.34.6/install"
NIX_INSTALLER_SHA256 = "bf6d12da4aeaae38ab509dc736df648597bcdee051046ea76d3d53d5b18fa54a"
NIX_PROFILE = "/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh"
IOS_CORE_JOB = ".#hydraJobs.aarch64-darwin.aarch64-darwin-ios:lib:simplex-chat"
IOS_CORE_ZIP = "pkg-ios-aarch64-swift-json.zip"
IOS_PROJECT = "apps/ios/SimpleX.xcodeproj"
IOS_SCHEME = "SimpleX (iOS)"
IOS_TOOLS = ("sh", "xcodebuild", "codesign", "otool", "plutil", "ditto")

LOCAL_HEADER = b"PK\x03\x04"
CENTRAL_HEADER = b"PK\x01\x02"
END_OF_CENTRAL_DIRECTORY = b"PK\x05\x06"
APKSIGNER_ALIGNMENT_EXTRA = 0xD935
V1_SIGNATURE_SUFFIXES = (".SF", ".RSA", ".DSA", ".EC")


def log(message: str) -> None:
    print(f"\033[36m{message}\033[0m", flush=True)


def require_tools(tools: Sequence[str]) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        raise ReproduceError(f"not found on PATH, please install: {', '.join(missing)}")
    if "docker" in tools and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise ReproduceError("docker is installed but its daemon is not reachable: start it, or check your user's access")


def run(
    args: Sequence[str | Path], cwd: Path | None = None, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(a) for a in args], cwd=cwd, check=False, text=True, stdout=subprocess.PIPE if capture else None
    )
    if check and result.returncode != 0:
        raise ReproduceError(f"failed ({result.returncode}): {' '.join(map(str, args))}")
    return result


# ---------------------------------------------------------------------------------------------------------------------
# APK ZIP details that apksigcopier and repro-apk don't handle


@dataclass(frozen=True)
class ZipEntry:
    name: str
    central_offset: int
    local_offset: int
    flags: int


def central_directory(data: bytes) -> list[ZipEntry]:
    end = data.rfind(END_OF_CENTRAL_DIRECTORY)
    if end < 0:
        raise ReproduceError("no end of central directory record")
    count, _, offset = struct.unpack("<HII", data[end + 10 : end + 20])
    entries = []
    for _ in range(count):
        if data[offset : offset + 4] != CENTRAL_HEADER:
            raise ReproduceError(f"no central directory header at {offset}")
        (flags,) = struct.unpack("<H", data[offset + 8 : offset + 10])
        name_len, extra_len, comment_len = struct.unpack("<HHH", data[offset + 28 : offset + 34])
        (local_offset,) = struct.unpack("<I", data[offset + 42 : offset + 46])
        name = data[offset + 46 : offset + 46 + name_len].decode("utf-8")
        entries.append(ZipEntry(name, offset, local_offset, flags))
        offset += 46 + name_len + extra_len + comment_len
    return entries


def local_extra_fields(data: bytes, entry: ZipEntry) -> list[int]:
    offset = entry.local_offset
    if data[offset : offset + 4] != LOCAL_HEADER:
        raise ReproduceError(f"no local header for {entry.name} at {offset}")
    name_len, extra_len = struct.unpack("<HH", data[offset + 26 : offset + 30])
    extra = data[offset + 30 + name_len : offset + 30 + name_len + extra_len]
    fields = []
    while len(extra) >= 4:
        field_id, size = struct.unpack("<HH", extra[:4])
        fields.append(field_id)
        extra = extra[4 + size :]
    return fields


def apk_padding(data: bytes) -> str:
    """apksigner >= 35.0.0-rc1 pads entries with its 0xd935 extra field; AGP and older apksigner with zeros."""
    for entry in central_directory(data):
        if APKSIGNER_ALIGNMENT_EXTRA in local_extra_fields(data, entry):
            return "apksigner"
    return "zero"


def is_v1_signature_file(name: str) -> bool:
    if not name.startswith("META-INF/") or "/" in name[len("META-INF/") :]:
        return False
    return name == "META-INF/MANIFEST.MF" or name.endswith(V1_SIGNATURE_SUFFIXES)


def copy_v1_flags(source: bytes, target: bytes) -> bytes:
    """apksigner >= 35.0.0-rc1 sets the UTF-8 flag on the v1 signature files, which apksigcopier 1.1.1 drops."""
    flags = {e.name: e.flags for e in central_directory(source) if is_v1_signature_file(e.name)}
    patched = bytearray(target)
    for entry in central_directory(target):
        if entry.name in flags:
            packed = struct.pack("<H", flags[entry.name])
            patched[entry.central_offset + 8 : entry.central_offset + 10] = packed
            patched[entry.local_offset + 6 : entry.local_offset + 8] = packed
    return bytes(patched)


# ---------------------------------------------------------------------------------------------------------------------
# Files, releases and git


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256sums(directory: Path, target: Path | None = None) -> None:
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.name not in ("SHA256SUMS", "_sha256sums"))
    lines = "".join(f"{sha256(p)}  {p.name}\n" for p in files)
    (target or directory / "SHA256SUMS").write_text(lines)
    print(lines, end="")


def compare_dirs(built: Path, released: Path) -> list[str]:
    """Returns the names of the built files that differ from or are missing in the released ones."""
    mismatched = []
    for path in sorted(built.iterdir()):
        other = released / path.name
        if not other.is_file() or sha256(path) != sha256(other):
            mismatched.append(path.name)
    return mismatched


def download(url: str, target: Path) -> None:
    log(f"downloading {url}")
    try:
        with urllib.request.urlopen(url) as response, open(target, "wb") as f:
            shutil.copyfileobj(response, f)
    except OSError as e:
        raise ReproduceError(f"cannot download {url}: {e}") from e


def release_url(tag: str, name: str) -> str:
    return f"{REPO}/releases/download/{tag}/{name}"


def tag_version(tag: str) -> str:
    """v7.1.0-beta.7 -> 7.1.0, as the desktop CLI deb is versioned"""
    return tag.lstrip("v").split("-")[0]


def android_build_tag(tag: str, existing_tags: Sequence[str], arch: Arch) -> str:
    """Releases before armv7a was built from master have it in a separate <tag>-armv7a tag."""
    if arch == Arch.ARMV7A and f"{tag}-armv7a" in existing_tags:
        return f"{tag}-armv7a"
    return tag


def android_vercode(source: Path) -> str:
    for line in (source / "apps/multiplatform/gradle.properties").read_text().splitlines():
        if line.startswith("android.version_code="):
            return line.split("=", 1)[1].strip()
    raise ReproduceError("android.version_code not found in apps/multiplatform/gradle.properties")


def clone(workdir: Path) -> Path:
    run(["git", "-C", workdir, "clone", f"{REPO}.git", "simplex-chat"])
    return workdir / "simplex-chat"


def checkout(source: Path, tag: str) -> None:
    run(["git", "-C", source, "reset", "--hard", "--quiet"])
    run(["git", "-C", source, "clean", "-dfxq"])
    run(["git", "-C", source, "checkout", "--quiet", tag])
    run(["git", "-C", source, "submodule", "update", "--init", "--recursive", "--quiet"])


# ---------------------------------------------------------------------------------------------------------------------
# Docker


class Container:
    """A container of an image built from Dockerfile.build, removed with the image on exit."""

    def __init__(
        self,
        source: Path,
        mounts: Mapping[Path, str],
        build_args: Mapping[str, str] | None = None,
        run_args: Sequence[str] = (),
    ) -> None:
        self.name = f"simplex-reproduce-{os.getpid()}-{id(self)}"
        self.source = source
        self.mounts = dict(mounts)
        self.build_args = {"USER_UID": str(os.getuid()), "USER_GID": str(os.getgid()), **(build_args or {})}
        self.run_args = list(run_args)

    def __enter__(self) -> Container:
        build: list[str | Path] = ["docker", "build", "--no-cache", "-f", self.source / "Dockerfile.build", "-t", self.name]
        for key, value in self.build_args.items():
            build += ["--build-arg", f"{key}={value}"]
        run(build + [self.source])
        start: list[str | Path] = ["docker", "run", "-t", "-d", "--name", self.name, *self.run_args]
        for host, container in self.mounts.items():
            start += ["-v", f"{host}:{container}"]
        try:
            run(start + [self.name], capture=True)
        except BaseException:
            self.remove()
            raise
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.remove()

    def remove(self) -> None:
        run(["docker", "rm", "--force", self.name], check=False, capture=True)
        run(["docker", "image", "rm", self.name], check=False, capture=True)

    def exec(
        self, script: str, env: Mapping[str, str] | None = None, user: str | None = None, capture: bool = False
    ) -> subprocess.CompletedProcess[str]:
        args = ["docker", "exec"]
        if user:
            args += ["-u", user]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        return run(args + [self.name, "sh", "-eu", "-c", script], capture=capture)

    def copy_out(self, path: str, target: Path) -> None:
        run(["docker", "cp", f"{self.name}:{path}", target])


# ---------------------------------------------------------------------------------------------------------------------
# Android


@contextlib.contextmanager
def android_container(source: Path, out: Path, set_pid_max: bool) -> Iterator[Container]:
    # FUSE and SYS_ADMIN are what Dockerfile.build's builds need; systempaths=unconfined makes /proc/sys writable
    with Container(
        source,
        {source.resolve(): "/project", out.resolve(): "/out"},
        run_args=[
            "--device", "/dev/fuse",
            "--cap-add", "SYS_ADMIN",
            "--security-opt", "apparmor:unconfined",
            "--security-opt", "seccomp:unconfined",
            "--security-opt", "systempaths=unconfined",
        ],
    ) as container:
        if set_pid_max:
            # 32-bit bionic aborts above pid 65535, which the armv7a GHC runs under qemu. Since Linux 6.14 pid_max
            # belongs to each pid namespace, copied when it is created, so it is set in the container's.
            container.exec("echo 65535 > /proc/sys/kernel/pid_max", user="root")
        # build-android.sh needs the gradle of the wrapper's version on PATH
        container.exec("cd apps/multiplatform && ./gradlew --version")
        yield container


def retry_once(action: Callable[[], object]) -> None:
    # Gradle's downloads fail now and then
    try:
        action()
    except ReproduceError as e:
        log(f"{e}; running again")
        action()


def build_apk(container: Container, source: Path, arch: Arch, out_name: str, nix_cache: bool, keep_libs: bool) -> None:
    """The deterministic unsigned APK of scripts/android/build-android.sh, to /out/<out_name>."""
    env = {"ARCH": arch.value, "ABI": arch.abi, "VERCODE": android_vercode(source), "OUT": out_name}
    if nix_cache:
        env["NIX_CONFIG"] = NIX_CACHE_CONFIG
    retry_once(
        lambda: container.exec(
            'gradle_bin=$(find "$HOME/.gradle/wrapper/dists" -name gradle -type f -executable | head -1)\n'
            'export PATH="$(dirname "$gradle_bin"):$PATH"\n'
            'ARCHES="$ARCH" ./scripts/android/build-android.sh -gs "$VERCODE"\n'
            'cp "simplex-chat-$ABI.apk" "/out/$OUT"\n',
            env=env,
        )
    )
    if keep_libs:
        container.exec(
            'cp "result-$ARCH-libsimplex/pkg-$ARCH-android-libsimplex.zip" '
            '"result-$ARCH-libsupport/pkg-$ARCH-android-libsupport.zip" /out/',
            env=env,
        )


def build_bundle(container: Container, source: Path, libs: Path) -> None:
    # Gradle reads compression.level only from local.properties; a non-zero level extracts the native libraries at
    # install, as the APKs do.
    properties = "".join(f"{key}={value}\n" for key, value in ANDROID_LOCAL_PROPERTIES.items())
    (source / "apps/multiplatform/local.properties").write_text(properties)
    for arch in Arch:
        target = source / ANDROID_LIBS_DIR / arch.abi
        target.mkdir(parents=True, exist_ok=True)
        for lib in ANDROID_LIBS:
            name = f"pkg-{arch.value}-android-{lib}.zip"
            found = sorted(libs.rglob(name))
            if not found:
                raise ReproduceError(f"{name} not found in {libs}")
            with zipfile.ZipFile(found[0]) as z:
                z.extractall(target)
    retry_once(
        lambda: container.exec(
            "cd apps/multiplatform\n"
            "./gradlew --no-daemon :android:bundleGoogleRelease\n"
            "cp android/build/outputs/bundle/googleRelease/*.aab /out/simplex.aab\n"
        )
    )


def release_signer(container: Container, apk_name: str) -> str:
    output = container.exec(f'apksigner verify --print-certs "/out/{apk_name}"', capture=True).stdout
    digest = next(line.split()[-1] for line in output.splitlines() if "SHA-256" in line)
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()


def verify_apk(container: Container, out: Path, released: str, built: str) -> bool:
    """Copies the released APK's signature onto the built one, padded as the release is, and compares them."""
    released_data = (out / released).read_bytes()
    pad = "--pad-like-apksigner" if apk_padding(released_data) == "apksigner" else ""
    container.exec(
        f'repro-apk zipalign --page-size 16 {pad} --replace "/out/{built}" "/out/{built}.aligned"\n'
        f'apksigcopier copy "/out/{released}" "/out/{built}.aligned" "/out/{built}.signed"\n'
    )
    signed = out / f"{built}.signed"
    signed.write_bytes(copy_v1_flags(released_data, signed.read_bytes()))
    return signed.read_bytes() == released_data


def reproduce_android(tag: str, arches: Sequence[Arch], nix_cache: bool, source: Path, results: Path) -> list[str]:
    out = results / "android"
    out.mkdir(parents=True, exist_ok=True)
    tags = run(["git", "-C", source, "tag", "--list"], capture=True).stdout.split()
    for arch in arches:
        download(release_url(tag, f"simplex-{arch.value}.apk"), out / f"simplex-{arch.value}.apk")
    failed = []
    with android_container(source, out, Arch.ARMV7A in arches) as container:
        for arch in arches:
            released = f"simplex-{arch.value}.apk"
            signer = release_signer(container, released)
            if signer != SIMPLEX_KEY:
                raise ReproduceError(f"{released} is signed by {signer}, not by the SimpleX key")
            log(f"{released} is signed by the SimpleX key")
            checkout(source, android_build_tag(tag, tags, arch))
            log(f"building {arch.value}")
            build_apk(container, source, arch, f"{released}.built", nix_cache, keep_libs=False)
            if verify_apk(container, out, released, f"{released}.built"):
                log(f"{released} is reproduced")
            else:
                log(f"{released} is NOT reproduced")
                failed.append(released)
    return failed


# ---------------------------------------------------------------------------------------------------------------------
# iOS


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def minimum_versions(otool_output: str) -> list[str]:
    """Minimum OS versions of the objects in otool -l output: LC_BUILD_VERSION's minos, LC_VERSION_MIN_*'s version."""
    versions = []
    in_version_min = False
    for line in otool_output.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        if fields[0] == "minos":
            versions.append(fields[1])
        elif fields[0] == "cmd":
            in_version_min = fields[1].startswith("LC_VERSION_MIN_")
        elif in_version_min and fields[0] == "version":
            versions.append(fields[1])
            in_version_min = False
    return versions


def build_settings(output: str, names: Sequence[str]) -> dict[str, str]:
    settings: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key in names and key not in settings:
            settings[key] = value
    missing = [name for name in names if not settings.get(name)]
    if missing:
        raise ReproduceError(f"build settings not found: {', '.join(missing)}")
    return settings


def resolve_entitlements(text: str, team: str) -> str:
    # $(AppIdentifierPrefix) is the team id: simplex.chat's apple-app-site-association lists 5NN7GUYB6T.chat.simplex.app
    return text.replace("$(AppIdentifierPrefix)", f"{team}.")


def nix_shell(script: str, cwd: Path) -> None:
    run(["sh", "-c", f". {NIX_PROFILE} && {script}"], cwd=cwd)


def install_nix(work: Path) -> None:
    installer = work / "nix-install"
    download(NIX_INSTALLER, installer)
    if sha256(installer) != NIX_INSTALLER_SHA256:
        raise ReproduceError("the Nix installer's sha256 doesn't match")
    conf = work / "nix.conf"
    conf.write_text(f"experimental-features = nix-command flakes\nmax-jobs = auto\n{NIX_CACHE_CONFIG}\n")
    run(["sh", installer, "--daemon", "--yes", "--no-channel-add", "--nix-extra-conf-file", conf])


def check_minimum_versions(source: Path, target: str) -> None:
    # mac2ios relabels macOS objects as iOS and keeps their minimum version, which the deployment target must cover
    for lib in sorted((source / "apps/ios/Libraries/ios").glob("*.a")):
        versions = minimum_versions(run(["otool", "-l", lib], capture=True).stdout)
        newest = max(versions, key=version_tuple, default=None)
        print(f"{lib.name}: newest minimum version {newest or 'none'}")
        if newest and version_tuple(newest) > version_tuple(target):
            raise ReproduceError(f"{lib.name} has objects for iOS {newest}, newer than the deployment target {target}")


def sign_bundle(bundle: Path, entitlements: Path, team: str, work: Path) -> None:
    """Ad-hoc signs a bundle with its entitlements: the export keeps only the entitlements a signature carries."""
    if not entitlements.is_file():
        raise ReproduceError(f"no entitlements for {bundle.name}: {entitlements}")
    resolved = work / f"{bundle.name}.entitlements"
    resolved.write_text(resolve_entitlements(entitlements.read_text(), team))
    frameworks = bundle / "Frameworks"
    if frameworks.is_dir():
        for code in sorted(frameworks.iterdir()):
            if code.suffix in (".framework", ".dylib"):
                run(["codesign", "--force", "--sign", "-", "--timestamp=none", code])
    run(["codesign", "--force", "--sign", "-", "--timestamp=none", "--entitlements", resolved, bundle])
    signed = run(["codesign", "-d", "--entitlements", "-", "--xml", bundle], capture=True).stdout
    if plistlib.loads(signed.encode()) != plistlib.loads(resolved.read_bytes()):
        raise ReproduceError(f"the signature of {bundle.name} carries other entitlements than {entitlements}")
    print(f"== {bundle.name}\n{resolved.read_text()}")


def build_ios_archive(source: Path, art: Path, out: Path, install: bool) -> None:
    if not (art / "ios/Assets.xcassets").is_dir():
        raise ReproduceError(f"no ios/Assets.xcassets in {art}")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if install:
            install_nix(work)
        nix_shell(f'nix build -L --out-link "{work / "core"}" "{IOS_CORE_JOB}"', source)
        downloads = Path.home() / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        shutil.copy(work / "core" / IOS_CORE_ZIP, downloads / IOS_CORE_ZIP)
        run(["scripts/ci/ios-sim/prepare-libs.sh"], cwd=source)
        run(["scripts/ios/update-pbxproj.sh"], cwd=source)

        # The brand assets, as release-mobile.py adds them: Release.xcconfig includes Local.xcconfig, which turns on
        # SIMPLEX_ASSETS. copy-assets.sh runs first, as its build phase runs after the extensions' catalogs are built.
        (source / "apps/ios/Local.xcconfig").write_text(
            f"SIMPLEX_ASSETS_DIR = {art}\nSWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) SIMPLEX_ASSETS\n"
        )
        run(["scripts/ios/copy-assets.sh", art], cwd=source)
        if not list((source / "apps/ios/Shared/SimpleXAssets.xcassets").glob("*.imageset")):
            raise ReproduceError(f"copy-assets.sh copied no imagesets from {art}")

        xcode = ["-project", IOS_PROJECT, "-scheme", IOS_SCHEME, "-configuration", "Release"]
        settings_output = run(["xcodebuild", "-showBuildSettings", *xcode], cwd=source, capture=True).stdout
        settings = build_settings(settings_output, ["DEVELOPMENT_TEAM", "IPHONEOS_DEPLOYMENT_TARGET"])
        team = settings["DEVELOPMENT_TEAM"]
        check_minimum_versions(source, settings["IPHONEOS_DEPLOYMENT_TARGET"])

        # Xcode refuses ad-hoc signing for iOS devices ("Ad Hoc code signing is not allowed with SDK"), so the archive
        # is built unsigned and its bundles are signed after.
        archive = work / "SimpleX.xcarchive"
        run(
            [
                "xcodebuild", "archive", "-quiet", *xcode,
                "-destination", "generic/platform=iOS",
                "-archivePath", archive,
                "-onlyUsePackageVersionsFromResolvedFile",
                "-skipPackagePluginValidation",
                "-skipMacroValidation",
                "CODE_SIGNING_ALLOWED=NO",
            ],
            cwd=source,
        )
        app = archive / "Products/Applications/SimpleX.app"
        for appex in sorted((app / "PlugIns").glob("*.appex")):
            sign_bundle(appex, source / f"apps/ios/{appex.stem}/{appex.stem}.entitlements", team, work)
        sign_bundle(app, source / f"apps/ios/{IOS_SCHEME}.entitlements", team, work)
        run(["codesign", "--verify", "--deep", "--strict", app])
        # the team Xcode records when it signs an archive, which Organizer shows and distributes for
        run(["plutil", "-replace", "ApplicationProperties.Team", "-string", team, archive / "Info.plist"])
        run(["ditto", "-c", "-k", "--keepParent", archive, out / "SimpleX.xcarchive.zip"])


# ---------------------------------------------------------------------------------------------------------------------
# Desktop and CLI


def desktop_files(image: UbuntuImage) -> dict[str, str]:
    """Released file names of an Ubuntu image's builds, with their paths in the container."""
    cli = f"simplex-chat-ubuntu-{image.suffix}-x86_64"
    files = {
        cli: "/out/simplex-chat",
        f"{cli}.deb": "/out/deb-build/simplex-chat.deb",
        f"simplex-desktop-ubuntu-{image.suffix}-x86_64.deb": "/project/apps/multiplatform/release/main/deb/simplex_x86_64.deb",
    }
    if image.version == APPIMAGE_IMAGE:
        files["simplex-desktop-x86_64.AppImage"] = "/project/apps/multiplatform/release/main/simplex.appimage"
    return files


def build_desktop_linux(source: Path, image: UbuntuImage, version: str, out: Path) -> None:
    """The Linux CLI, its deb, the desktop deb and, on APPIMAGE_IMAGE, the AppImage, with their released names."""
    (source / "cabal.project.local").write_text(DESKTOP_CABAL_LOCAL)
    log(f"building Ubuntu {image.version} CLI and desktop apps")
    with Container(
        source,
        {source: "/project"},
        build_args={"TAG": image.version, "HASH": image.digest, "GHC": GHC},
        run_args=["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN", "--security-opt", "apparmor:unconfined"],
    ) as container:
        container.exec(
            "find /project -type d -exec chmod 755 {} +\n"
            "find /project -type f -perm /111 -exec chmod 755 {} +\n"
            "find /project -type f ! -perm /111 -exec chmod 644 {} +\n"
            "cabal clean && cabal update && cabal build -j\n"
            "mkdir -p /out\n"
            "bin=$(find /project/dist-newstyle -name simplex-chat -type f -executable)\n"
            'mv "$bin" /out/simplex-chat && strip /out/simplex-chat\n'
            f'./scripts/desktop/build-cli-deb.sh "{version}"\n'
            "export ASSETS_DIR=../../assets\n"
            "./scripts/desktop/make-deb-linux.sh\n"
        )
        if image.version == APPIMAGE_IMAGE:
            container.exec(
                "export ASSETS_DIR=../../assets\n"
                "./scripts/desktop/make-appimage-linux.sh\n"
                "mv ./apps/multiplatform/release/main/*imple*.AppImage ./apps/multiplatform/release/main/simplex.appimage\n"
            )
        for name, path in desktop_files(image).items():
            container.copy_out(path, out / name)
        container.exec("rm -rf ./dist-newstyle ./apps/multiplatform")


def reproduce_desktop(tag: str, source: Path, results: Path) -> list[str]:
    built = results / "from-source"
    released = results / "prebuilt"
    built.mkdir(parents=True, exist_ok=True)
    released.mkdir(parents=True, exist_ok=True)
    for image in DESKTOP_IMAGES:
        checkout(source, tag)
        build_desktop_linux(source, image, tag_version(tag), built)
        for name in desktop_files(image):
            download(release_url(tag, name), released / name)
    mismatched = compare_dirs(built, released)
    for name in mismatched:
        log(f"{name}: sha256 doesn't match")
    if not mismatched:
        write_sha256sums(built, results / "_sha256sums")
        log(f"checksums: {results / '_sha256sums'}")
    return mismatched


# ---------------------------------------------------------------------------------------------------------------------
# Commands


@dataclass(frozen=True)
class Reproduce:
    tag: str
    targets: tuple[Target, ...]
    arches: tuple[Arch, ...]
    nix_cache: bool


@dataclass(frozen=True)
class BuildApk:
    arch: Arch
    out: Path
    source: Path
    nix_cache: bool


@dataclass(frozen=True)
class BuildBundle:
    libs: Path
    out: Path
    source: Path


@dataclass(frozen=True)
class BuildDesktopLinux:
    image: UbuntuImage
    version: str
    out: Path
    source: Path


@dataclass(frozen=True)
class BuildIosArchive:
    art: Path
    out: Path
    source: Path
    install_nix: bool


Command = Union[Reproduce, BuildApk, BuildBundle, BuildDesktopLinux, BuildIosArchive]


def reproduce(command: Reproduce) -> int:
    require_tools(["git", "docker"])
    results = Path.cwd() / f"{command.tag}-simplex-chat"
    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        source = clone(Path(tmp))
        if Target.DESKTOP in command.targets:
            failed += reproduce_desktop(command.tag, source, results)
        if Target.ANDROID in command.targets:
            failed += reproduce_android(command.tag, command.arches, command.nix_cache, source, results)
    if failed:
        log(f"{command.tag} is not reproduced: {', '.join(failed)}")
        return 1
    log(f"{command.tag} is reproduced: {', '.join(t.value for t in command.targets)}")
    return 0


def build_android(command: BuildApk | BuildBundle) -> int:
    require_tools(["docker"])
    source, out = command.source.resolve(), command.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    set_pid_max = isinstance(command, BuildApk) and command.arch == Arch.ARMV7A
    with android_container(source, out, set_pid_max) as container:
        if isinstance(command, BuildApk):
            build_apk(container, source, command.arch, f"simplex-{command.arch.value}.apk", command.nix_cache, keep_libs=True)
        else:
            build_bundle(container, source, command.libs.resolve())
    write_sha256sums(out)
    return 0


def build_ios(command: BuildIosArchive) -> int:
    require_tools(IOS_TOOLS)
    if not command.install_nix and not Path(NIX_PROFILE).is_file():
        raise ReproduceError(f"Nix is not installed ({NIX_PROFILE} not found): install it, or pass --install-nix")
    out = command.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    build_ios_archive(command.source.resolve(), command.art.resolve(), out, command.install_nix)
    write_sha256sums(out)
    return 0


def build_desktop(command: BuildDesktopLinux) -> int:
    require_tools(["docker"])
    out = command.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    build_desktop_linux(command.source.resolve(), command.image, command.version, out)
    write_sha256sums(out)
    return 0


def execute(command: Command) -> int:
    if isinstance(command, Reproduce):
        return reproduce(command)
    if isinstance(command, BuildIosArchive):
        return build_ios(command)
    if isinstance(command, BuildDesktopLinux):
        return build_desktop(command)
    return build_android(command)


def parse_build_args(argv: Sequence[str]) -> Command:
    parser = argparse.ArgumentParser(prog="simplex-chat-reproduce-builds.py build", description="Build one commit's apps unsigned.")
    kinds = parser.add_subparsers(dest="kind", required=True)
    apk = kinds.add_parser("android-apk", help="the deterministic unsigned APK and its core libraries")
    apk.add_argument("arch", type=Arch, choices=list(Arch))
    apk.add_argument("--nix-cache", action="store_true", help="substitute from zw3rk's and IOG's caches")
    bundle = kinds.add_parser("android-bundle", help="the unsigned Play bundle, from both arches' core libraries")
    bundle.add_argument("--libs", type=Path, required=True, help="directory with the pkg-<arch>-android-*.zip files")
    desktop = kinds.add_parser("desktop-linux", help="the Linux CLI, its deb, the desktop deb and the AppImage")
    desktop.add_argument("image", choices=[image.version for image in DESKTOP_IMAGES], help="the Ubuntu version")
    desktop.add_argument("--version", required=True, help="the release version of the CLI deb, e.g. 7.1.0")
    ios = kinds.add_parser("ios-archive", help="the iOS archive, its bundles signed ad hoc with their entitlements")
    ios.add_argument("--art", type=Path, required=True, help="checkout of simplex-chat-art, with the brand assets")
    ios.add_argument("--install-nix", action="store_true", help="install Nix first, as on a CI runner")
    for sub in (apk, bundle, desktop, ios):
        sub.add_argument("--out", type=Path, required=True)
        sub.add_argument("--source", type=Path, default=Path("."), help="the simplex-chat checkout (default: current directory)")
    args = parser.parse_args(argv)
    if args.kind == "android-apk":
        return BuildApk(args.arch, args.out, args.source, args.nix_cache)
    if args.kind == "android-bundle":
        return BuildBundle(args.libs, args.out, args.source)
    if args.kind == "desktop-linux":
        image = next(image for image in DESKTOP_IMAGES if image.version == args.image)
        return BuildDesktopLinux(image, args.version, args.out, args.source)
    return BuildIosArchive(args.art, args.out, args.source, args.install_nix)


def parse_arches(value: str) -> tuple[Arch, ...]:
    try:
        return tuple(Arch(arch) for arch in value.split(","))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{e}, choose from {', '.join(Arch)}") from e


def parse_target(value: str) -> Target:
    # a type rather than choices: argparse before 3.12 checks choices against the empty list of nargs="*"
    try:
        return Target(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{e}, choose from {', '.join(Target)}") from e


def parse_args(argv: Sequence[str]) -> Command:
    if argv[:1] == ["build"]:
        return parse_build_args(argv[1:])
    parser = argparse.ArgumentParser(description="Reproduce a SimpleX Chat release and compare it with the released files.")
    parser.add_argument("tag", help="release tag, e.g. v7.1.0")
    parser.add_argument("targets", nargs="*", type=parse_target, help="desktop (default), android or both")
    parser.add_argument("--arches", type=parse_arches, default=tuple(Arch), help="Android arches, e.g. aarch64,armv7a")
    parser.add_argument("--nix-cache", action="store_true", help="substitute the Android toolchains from zw3rk's and IOG's caches")
    args = parser.parse_args(argv)
    return Reproduce(args.tag, tuple(args.targets) or (Target.DESKTOP,), args.arches, args.nix_cache)


def main(argv: Sequence[str]) -> int:
    command = parse_args(argv)
    os.environ["DOCKER_BUILDKIT"] = "1"
    try:
        return execute(command)
    except ReproduceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
