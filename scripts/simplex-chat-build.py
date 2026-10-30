#!/usr/bin/env python3

"""Builds SimpleX Chat release files and reproduces releases.

Reproduce a release (desktop is the default target):
    simplex-chat-build.py reproduce <tag> [desktop] [android] [--arches aarch64,armv7a] [--nix-cache]

Build one commit's release files, as CI does; desktop, libs and openssl build for the machine they run on:
    simplex-chat-build.py build desktop linux <22.04|24.04> --version <x.y.z> --out <dir>
    simplex-chat-build.py build desktop macos [--require-signing] --out <dir>
    simplex-chat-build.py build desktop windows --out <dir>
    simplex-chat-build.py build libs linux [--ubuntu <22.04|24.04>] [--database <sqlite|postgres>] [--dist-dir <dir>]
        [--host | --container <name>] --out <dir>
    simplex-chat-build.py build libs macos [--database <sqlite|postgres>] [--require-signing] --out <dir>
    simplex-chat-build.py build libs windows --out <dir>
    simplex-chat-build.py build libs android [--arch <aarch64|armv7a>] [--install-nix] --out <dir>
    simplex-chat-build.py build libs ios-sim --out <dir>
    simplex-chat-build.py build openssl <macos|windows> --out <dir>
    simplex-chat-build.py build apk <aarch64|armv7a> [--nix-cache] --out <dir>
    simplex-chat-build.py build aab --libs <dir> --out <dir>
    simplex-chat-build.py build xcarchive --art <simplex-chat-art> [--install-nix] --out <dir>
Each build takes --source <dir>, the simplex-chat checkout, by default the current directory.

Install prebuilt core libraries where the apps' builds take them, as the test workflows do:
    simplex-chat-build.py install libs linux <libs zip>
    simplex-chat-build.py install libs android <dir> [--arch <aarch64|armv7a>]
    simplex-chat-build.py install libs ios <pkg-ios-aarch64-swift-json.zip>

Needs Python 3.12+; the Linux and Android builds need Docker, xcarchive macOS with Xcode and Nix, and reproduce git.
reproduce writes its results to ./<tag>-simplex-chat.
The Android builds are signed by Gradle when ANDROID_KEYSTORE, ANDROID_KEYSTORE_PASSWORD, ANDROID_KEY_ALIAS and
ANDROID_KEY_PASSWORD are set, and unsigned otherwise.
"""

import sys

if sys.version_info < (3, 12):  # noqa: UP036 # runs on the older Pythons it rejects
    sys.exit("simplex-chat-build.py needs Python 3.12 or newer")

import argparse
import base64
import contextlib
import enum
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Protocol

REPO = "https://github.com/simplex-chat/simplex-chat"
GHC = "9.6.3"


class ReproduceError(Exception):
    pass


class Target(enum.StrEnum):
    DESKTOP = "desktop"
    ANDROID = "android"


class Platform(enum.StrEnum):
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"


def host_platform() -> Platform:
    if os.name == "nt":
        return Platform.WINDOWS
    return Platform.MACOS if sys.platform == "darwin" else Platform.LINUX


def require_platform(required: Platform) -> None:
    if host_platform() != required:
        raise ReproduceError(f"this build runs on {required}, not on {host_platform()}")


class Arch(enum.StrEnum):
    AARCH64 = "aarch64"
    ARMV7A = "armv7a"

    @property
    def abi(self) -> str:
        return {Arch.AARCH64: "arm64-v8a", Arch.ARMV7A: "armeabi-v7a"}[self]


@dataclass(frozen=True, slots=True)
class UbuntuImage:
    version: str
    arch: str
    digest: str

    @property
    def suffix(self) -> str:
        return self.version.replace(".", "_")


# The Ubuntu images of build.yml's release builds, for each version and machine
DESKTOP_IMAGES = (
    UbuntuImage("22.04", "x86_64", "sha256:5c8b2c0a6c745bc177669abfaa716b4bc57d58e2ea3882fb5da67f4d59e3dda5"),
    UbuntuImage("24.04", "x86_64", "sha256:98ff7968124952e719a8a69bb3cccdd217f5fe758108ac4f21ad22e1df44d237"),
    UbuntuImage("22.04", "aarch64", "sha256:6a62a4157b8775eaf4959cb629e757d32d39d1f4c8ac1b0ddc2510b555cf72f3"),
    UbuntuImage("24.04", "aarch64", "sha256:68434214381cb38287104e629fe8ee720167dd98cbb36ab1cbbab342515fa6ab"),
)
APPIMAGE_IMAGE = "22.04"
DESKTOP_CABAL_LOCAL = "ignore-project: False\npackage direct-sqlcipher\n    flags: +openssl"
DESKTOP_ENV = {"ASSETS_DIR": "../../assets"}
DESKTOP_RELEASE_DIR = "apps/multiplatform/release/main"
SOURCE_DATE_EPOCH = "1704067200"  # the desktop releases' timestamps since v5.4

# The OpenSSL bundled with the apps; pinOpenssl in flake.nix pins the same for the Android and iOS core libraries
OPENSSL_VERSION = "3.5.9"
OPENSSL_URL = f"https://github.com/openssl/openssl/releases/download/openssl-{OPENSSL_VERSION}/openssl-{OPENSSL_VERSION}.tar.gz"
OPENSSL_SHA256 = "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
# OpenSSL loads openssl.cnf from OPENSSLDIR and modules from the prefix, and the file can load any module: only
# administrators can write to these directories on users' machines
OPENSSL_UNIX_PREFIX = "/usr/local"
OPENSSL_UNIX_DIR = "/etc/ssl"
OPENSSL_WINDOWS_PREFIX = "C:/Program Files/OpenSSL"
OPENSSL_WINDOWS_DIR = "C:/Program Files/Common Files/SSL"
# The system libraries that the static libcrypto of a MinGW build needs (ex_libs of OpenSSL's mingw-common)
OPENSSL_WINDOWS_STATIC_LINK = "-optl-lws2_32 -optl-lgdi32 -optl-lcrypt32"
DESKTOP_LIBS_DIR = "apps/multiplatform/common/src/commonMain/cpp/desktop/libs"
APPLE_TEAM = "5NN7GUYB6T"
APPLE_IDENTITY = f"Developer ID Application: SimpleX Chat Ltd ({APPLE_TEAM})"
LIBFFI_URL = "https://github.com/libffi/libffi/releases/download/v3.5.2/libffi-3.5.2.tar.gz"
LIBFFI_SHA256 = "f3a3082a23b37c293a4fcd1053147b371f2ff91fa7ea1b2a52e335676bac82dc"
APPLE_SIGNING_ENV = ("APPLE_SIMPLEX_SIGNING_KEYCHAIN", "APPLE_SIMPLEX_NOTARIZATION_APPLE_ID", "APPLE_SIMPLEX_NOTARIZATION_PASSWORD")

# SHA-256 of the certificate that signs the released APKs
SIMPLEX_KEY = "3C:52:C4:FD:3C:AD:1C:07:C9:B0:0A:70:80:E3:58:FA:B9:FE:FC:B8:AF:5A:EC:14:77:65:F1:6D:0F:21:AD:85"
ANDROID_LIBS_DIR = "apps/multiplatform/common/src/commonMain/cpp/android/libs"
ANDROID_LIBS = ("libsimplex", "libsupport")
# The flake.nix jobs of the Android core libraries
ANDROID_LIB_JOBS = {"libsimplex": "lib:simplex-chat", "libsupport": "lib:support"}
ANDROID_BUNDLE_DIR = "apps/multiplatform/android/build/outputs/bundle/googleRelease"
# The release key that android/build.gradle.kts signs with: the keystore's path and its passwords and alias
ANDROID_SIGNING_ENV = ("ANDROID_KEYSTORE", "ANDROID_KEYSTORE_PASSWORD", "ANDROID_KEY_ALIAS", "ANDROID_KEY_PASSWORD")
ANDROID_KEYSTORE_MOUNT = "/keystore/release.keystore"
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
NIX_PROFILE = Path("/nix/var/nix/profiles/default")
# The certificate bundles nix-daemon.sh looks for, in its order
NIX_CERT_FILES = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/ssl/ca-bundle.pem"),
    Path("/etc/ssl/certs/ca-bundle.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    NIX_PROFILE / "etc/ssl/certs/ca-bundle.crt",
    NIX_PROFILE / "etc/ca-bundle.crt",
)
IOS_CORE_JOB = ".#hydraJobs.aarch64-darwin.aarch64-darwin-ios:lib:simplex-chat"
IOS_CORE_ZIP = "pkg-ios-aarch64-swift-json.zip"
IOS_PROJECT = "apps/ios/SimpleX.xcodeproj"
IOS_SCHEME = "SimpleX (iOS)"
IOS_TOOLS = ("xcodebuild", "codesign", "otool", "plutil", "ditto")
IOS_LIBRARIES = "apps/ios/Libraries"
IOS_PBXPROJ = "apps/ios/SimpleX.xcodeproj/project.pbxproj"
# LC_BUILD_VERSION platforms of the iOS simulator, as otool prints them
IOS_SIMULATOR_PLATFORMS = ("7", "IOSSIMULATOR")
MAC2IOS_SOURCE = "https://raw.githubusercontent.com/zw3rk/mobile-core-tools/4dcb77d5ea896d749381806dfab5358851b08951"
MAC2IOS_SHA256 = {
    "mac2ios.c": "2e55f956eb255ac2b523698d69cb9822ace4df357182e70df97107075b27f934",
    "ar.h": "fe0451cc7c5b92c59662db81cf6519576f5bc7b29c0cc34077e540cd293c5a3f",
    "macho.h": "1d49fd7f0c8d40f4cf3d26d83b7f391ae8f270173fba06e43c2cd55ed09860fd",
}
# The iOS core as flake.nix's aarch64-darwin-ios:lib:simplex-chat builds it, built with cabal for the simulator tests
IOS_SIM_CABAL_LOCAL = """ignore-project: False

package simplex-chat
    flags: +swift +client_library
    static: True
    shared: False
    ghc-options: -threaded -DIOS

package simplexmq
    flags: +swift +client_library +commoncrypto
    extra-include-dirs: {include}
    extra-lib-dirs: {lib}

package direct-sqlcipher
    flags: +commoncrypto

package entropy
    flags: +DoNotGetEntropy
"""
IOS_LOCAL_XCCONFIG = "SIMPLEX_ASSETS_DIR = {art}\nSWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) SIMPLEX_ASSETS\n"

LOCAL_HEADER = b"PK\x03\x04"
CENTRAL_HEADER = b"PK\x01\x02"
END_OF_CENTRAL_DIRECTORY = b"PK\x05\x06"
APKSIGNER_ALIGNMENT_EXTRA = 0xD935
V1_SIGNATURE_SUFFIXES = (".SF", ".RSA", ".DSA", ".EC")


def log(message: str) -> None:
    print(f"\033[36m{message}\033[0m", flush=True)


def on_github() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def github_escape(message: str) -> str:
    # https://github.com/actions/toolkit/blob/main/packages/core/src/command.ts
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def warn(message: str) -> None:
    """An annotation on the run's page on GitHub Actions, yellow text elsewhere."""
    if on_github():
        print(f"::warning::{github_escape(message)}", flush=True)
    else:
        print(f"\033[33mwarning: {message}\033[0m", file=sys.stderr, flush=True)


def error(message: str) -> None:
    if on_github():
        print(f"::error::{github_escape(message)}", flush=True)
    else:
        print(f"\033[31merror: {message}\033[0m", file=sys.stderr, flush=True)


@contextlib.contextmanager
def log_group(title: str) -> Iterator[None]:
    """A collapsible section of the log on GitHub Actions."""
    if not on_github():
        log(f"$ {title}")
        yield
        return
    print(f"::group::{github_escape(title)}", flush=True)
    try:
        yield
    finally:
        print("::endgroup::", flush=True)


def add_summary(markdown: str) -> None:
    """Appended to the job's summary page on GitHub Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if on_github() and path:
        with open(path, "a") as f:
            f.write(markdown)


def require_tools(tools: Sequence[str]) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        raise ReproduceError(f"not found on PATH, please install: {', '.join(missing)}")
    if "docker" in tools and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise ReproduceError("docker is installed but its daemon is not reachable: start it, or check your user's access")


def run(
    args: Sequence[str | Path],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
    check: bool = True,
    capture: bool = False,
    title: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Commands with output to show run in their own log group, titled with the command unless title is given."""
    with contextlib.nullcontext() if capture else log_group(title or " ".join(map(str, args))):
        result = subprocess.run(
            [str(a) for a in args],
            cwd=cwd,
            env={**os.environ, **env} if env else None,
            input=input,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else None,
        )
    if check and result.returncode != 0:
        raise ReproduceError(f"failed ({result.returncode}): {' '.join(map(str, args))}")
    return result


# ---------------------------------------------------------------------------------------------------------------------
# APK ZIP details that apksigcopier and repro-apk don't handle


@dataclass(frozen=True, slots=True)
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
    if not name.startswith("META-INF/") or "/" in name.removeprefix("META-INF/"):
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
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def write_sha256sums(directory: Path, target: Path | None = None) -> None:
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.name not in ("SHA256SUMS", "_sha256sums"))
    hashes = {p.name: sha256(p) for p in files}
    lines = "".join(f"{digest}  {name}\n" for name, digest in hashes.items())
    (target or directory / "SHA256SUMS").write_text(lines)
    print(lines, end="")
    rows = "".join(f"| `{p.name}` | {p.stat().st_size:,} | `{hashes[p.name]}` |\n" for p in files)
    add_summary(f"| File | Bytes | SHA-256 |\n|---|--:|---|\n{rows}\n")


def compare_dirs(built: Path, released: Path) -> list[str]:
    """Returns the names of the built files that differ from or are missing in the released ones."""
    mismatched = []
    for path in sorted(built.iterdir()):
        other = released / path.name
        if not other.is_file() or sha256(path) != sha256(other):
            mismatched.append(path.name)
    return mismatched


def normalize_permissions(root: Path) -> None:
    """Directories and executable files 755, other files 644, so that checkouts on any machine build the same."""
    for directory, dirs, files in os.walk(root):
        for name in dirs:
            path = Path(directory, name)
            if not path.is_symlink():
                path.chmod(0o755)
        for name in files:
            path = Path(directory, name)
            if not path.is_symlink():
                path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)


def single_file(directory: Path, pattern: str) -> Path:
    found = sorted(directory.glob(pattern))
    if len(found) != 1:
        raise ReproduceError(f"expected one {pattern} in {directory}, found {len(found)}")
    return found[0]


def download(url: str, target: Path) -> None:
    log(f"downloading {url}")
    try:
        with urllib.request.urlopen(url) as response, open(target, "wb") as f:
            shutil.copyfileobj(response, f)
    except OSError as e:
        raise ReproduceError(f"cannot download {url}: {e}") from e


def fetch_pinned(url: str, digest: str, target: Path) -> Path:
    download(url, target)
    if sha256(target) != digest:
        raise ReproduceError(f"{url} doesn't match its pinned SHA-256 {digest}")
    return target


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

PROJECT = "/project"
OUT = "/out"


@dataclass(frozen=True, slots=True)
class Step:
    """A command run in a container, without a shell."""

    argv: tuple[str, ...]
    workdir: str = PROJECT
    env: Mapping[str, str] = field(default_factory=dict)
    user: str | None = None
    input: str | None = None


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
        self.owned = True
        self.source = source
        self.mounts = dict(mounts)
        self.build_args = {"USER_UID": str(os.getuid()), "USER_GID": str(os.getgid()), **(build_args or {})}
        self.run_args = list(run_args)

    @classmethod
    def running(cls, name: str, source: Path) -> "Container":
        """A container that runs already, such as CI's builder, with source at /project; kept on exit."""
        container = cls(source, {source: PROJECT})
        container.name = name
        container.owned = False
        return container

    def __enter__(self) -> "Container":
        if not self.owned:
            return self
        build: list[str | Path] = ["docker", "build", "--no-cache", "-f", self.source / "Dockerfile.build", "-t", self.name]
        for key, value in self.build_args.items():
            build += ["--build-arg", f"{key}={value}"]
        run([*build, self.source])
        start: list[str | Path] = ["docker", "run", "-t", "-d", "--name", self.name, *self.run_args]
        for host, container in self.mounts.items():
            start += ["-v", f"{host}:{container}"]
        try:
            run([*start, self.name], capture=True)
        except BaseException:
            self.remove()
            raise
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if self.owned:
            self.remove()

    def remove(self) -> None:
        run(["docker", "rm", "--force", self.name], check=False, capture=True)
        run(["docker", "image", "rm", self.name], check=False, capture=True)

    def run(self, step: Step, capture: bool = False, secrets: Mapping[str, str] | None = None) -> str:
        """Secrets are environment variables passed to docker by name, so that their values are in no command line."""
        args = ["docker", "exec", "-w", step.workdir]
        if step.input is not None:
            args.append("-i")
        if step.user:
            args += ["-u", step.user]
        for key, value in step.env.items():
            args += ["-e", f"{key}={value}"]
        for key in secrets or {}:
            args += ["-e", key]
        title = " ".join(step.argv)
        return run([*args, self.name, *step.argv], env=secrets, input=step.input, capture=capture, title=title).stdout or ""

    def run_all(self, steps: Sequence[Step]) -> None:
        for step in steps:
            self.run(step)

    def copy_out(self, path: str, target: Path) -> None:
        # -L copies the targets of symlinks, such as Nix's out-links into the container's store
        run(["docker", "cp", "-L", f"{self.name}:{path}", target])

    def path(self, host: Path) -> str:
        """Where a host path under one of the mounts is in the container."""
        for mount, target in self.mounts.items():
            if host.is_relative_to(mount):
                return str(PurePosixPath(target.removesuffix(":ro")) / host.relative_to(mount).as_posix())
        raise ReproduceError(f"{host} is in no mount of the container")


class HostRunner:
    """Runs steps on this machine, with the build tools installed on it."""

    def run(self, step: Step, capture: bool = False, secrets: Mapping[str, str] | None = None) -> str:
        env = {**step.env, **(secrets or {})}
        return run(step.argv, cwd=Path(step.workdir), env=env, input=step.input, capture=capture, title=" ".join(step.argv)).stdout or ""

    def path(self, host: Path) -> str:
        return str(host)


class Runner(Protocol):
    def run(self, step: Step, capture: bool = False, secrets: Mapping[str, str] | None = None) -> str: ...

    def path(self, host: Path) -> str: ...


def retry_once(action: Callable[[], object]) -> None:
    # Gradle's downloads fail now and then
    try:
        action()
    except ReproduceError as e:
        warn(f"{e}; running it again")
        action()


# ---------------------------------------------------------------------------------------------------------------------
# Android


@dataclass(frozen=True, slots=True)
class AndroidSigning:
    keystore: Path
    store_password: str = field(repr=False)
    key_alias: str
    key_password: str = field(repr=False)

    @staticmethod
    def from_env(environ: Mapping[str, str]) -> "AndroidSigning | None":
        """The release key of the ANDROID_SIGNING_ENV variables, or None when none of them is set."""
        values = [environ.get(name, "") for name in ANDROID_SIGNING_ENV]
        if not any(values):
            return None
        missing = [name for name, value in zip(ANDROID_SIGNING_ENV, values, strict=True) if not value]
        if missing:
            raise ReproduceError(f"set all of {', '.join(ANDROID_SIGNING_ENV)} to sign, or none; missing: {', '.join(missing)}")
        keystore = Path(values[0])
        if not keystore.is_file():
            raise ReproduceError(f"the keystore {keystore} is not found")
        return AndroidSigning(keystore.resolve(), values[1], values[2], values[3])

    @property
    def env(self) -> dict[str, str]:
        return {"ANDROID_KEYSTORE": ANDROID_KEYSTORE_MOUNT, "ANDROID_KEY_ALIAS": self.key_alias}

    @property
    def secrets(self) -> dict[str, str]:
        return {"ANDROID_KEYSTORE_PASSWORD": self.store_password, "ANDROID_KEY_PASSWORD": self.key_password}


def android_signing() -> AndroidSigning | None:
    signing = AndroidSigning.from_env(os.environ)
    if signing is None:
        warn(f"{', '.join(ANDROID_SIGNING_ENV)} are not set: building unsigned")
    return signing


@contextlib.contextmanager
def android_container(source: Path, out: Path, set_pid_max: bool, signing: AndroidSigning | None = None) -> Iterator[Container]:
    mounts = {source.resolve(): PROJECT, out.resolve(): OUT}
    if signing:
        mounts[signing.keystore] = f"{ANDROID_KEYSTORE_MOUNT}:ro"
    # FUSE and SYS_ADMIN are what Dockerfile.build's builds need; systempaths=unconfined makes /proc/sys writable
    with Container(
        source,
        mounts,
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
            container.run(Step(("tee", "/proc/sys/kernel/pid_max"), user="root", input="65535\n"))
        yield container


def gradle_path(container: Container) -> str:
    """PATH with the gradle of the project's wrapper first, which build-android.sh needs as `gradle`."""
    container.run(Step(("./gradlew", "--version"), workdir=f"{PROJECT}/apps/multiplatform"))
    home = container.run(Step(("printenv", "HOME")), capture=True).strip()
    found = container.run(
        Step(("find", f"{home}/.gradle/wrapper/dists", "-name", "gradle", "-type", "f", "-executable")), capture=True
    ).split()
    if not found:
        raise ReproduceError("the gradle wrapper's distribution is not found")
    path = container.run(Step(("printenv", "PATH")), capture=True).strip()
    return f"{Path(found[0]).parent}:{path}"


def build_apk(
    container: Container,
    source: Path,
    arch: Arch,
    out: Path,
    out_name: str,
    nix_cache: bool,
    keep_libs: bool,
    signing: AndroidSigning | None = None,
) -> None:
    """The deterministic APK of scripts/android/build-android.sh, signed by Gradle with signing, as <out>/<out_name>."""
    env = {"ARCHES": arch.value, "PATH": gradle_path(container)}
    if nix_cache:
        env["NIX_CONFIG"] = NIX_CACHE_CONFIG
    secrets = None
    if signing:
        env |= signing.env
        secrets = signing.secrets
    build = Step(("./scripts/android/build-android.sh", "-gs", android_vercode(source)), env=env)
    retry_once(lambda: container.run(build, secrets=secrets))
    shutil.copy(source / f"simplex-chat-{arch.abi}.apk", out / out_name)
    if keep_libs:
        for lib in ANDROID_LIBS:
            container.copy_out(f"{PROJECT}/result-{arch}-{lib}/pkg-{arch}-android-{lib}.zip", out)


def build_bundle(container: Container, source: Path, libs: Path, out: Path, signing: AndroidSigning | None = None) -> None:
    # Gradle reads compression.level only from local.properties; a non-zero level extracts the native libraries at
    # install, as the APKs do.
    properties = "".join(f"{key}={value}\n" for key, value in ANDROID_LOCAL_PROPERTIES.items())
    (source / "apps/multiplatform/local.properties").write_text(properties)
    for arch in Arch:
        target = source / ANDROID_LIBS_DIR / arch.abi
        target.mkdir(parents=True, exist_ok=True)
        for lib in ANDROID_LIBS:
            name = f"pkg-{arch}-android-{lib}.zip"
            found = sorted(libs.rglob(name))
            if not found:
                raise ReproduceError(f"{name} not found in {libs}")
            with zipfile.ZipFile(found[0]) as z:
                z.extractall(target)
    build = Step(
        ("./gradlew", "--no-daemon", ":android:bundleGoogleRelease"),
        workdir=f"{PROJECT}/apps/multiplatform",
        env=signing.env if signing else {},
    )
    retry_once(lambda: container.run(build, secrets=signing.secrets if signing else None))
    shutil.copy(single_file(source / ANDROID_BUNDLE_DIR, "*.aab"), out / "simplex.aab")


def build_android_libs(source: Path, arch: Arch, out: Path, install: bool) -> None:
    """The core libraries of the APK, built with Nix on this machine, as the emulator tests use them."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if install:
            install_nix(work)
        for lib, job in ANDROID_LIB_JOBS.items():
            result = work / f"result-{lib}"
            run(["nix", "build", "-L", "--out-link", result, f".#hydraJobs.x86_64-linux.{arch}-android:{job}"], cwd=source, env=nix_env())
            shutil.copy(result / f"pkg-{arch}-android-{lib}.zip", out)


def extract_flat(package: Path, target: Path, prefix: str = "") -> None:
    """The files of a zip under prefix, in target without their directories."""
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package) as z:
        for member in z.infolist():
            if not member.is_dir() and member.filename.startswith(prefix):
                (target / Path(member.filename).name).write_bytes(z.read(member))


def install_android_libs(source: Path, arch: Arch, packages: Path) -> None:
    for lib in ANDROID_LIBS:
        extract_flat(packages / f"pkg-{arch}-android-{lib}.zip", source / ANDROID_LIBS_DIR / arch.abi)


def install_linux_libs(source: Path, package: Path) -> None:
    arch = platform.machine()
    extract_flat(package, desktop_libs(source, f"linux-{arch}"), "libs/")
    link_desktop_libs(source, f"linux-{arch}", f"linux-{COMPOSE_ARCHES[arch]}")


def release_signer(container: Container, apk_name: str) -> str:
    output = container.run(Step(("apksigner", "verify", "--print-certs", f"{OUT}/{apk_name}")), capture=True)
    digest = next(line.split()[-1] for line in output.splitlines() if "SHA-256" in line)
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()


def copy_signature(container: Container, out: Path, released: str, unsigned: str) -> bytes:
    """The unsigned APK with the released APK's signature, and the v1 signature flags that apksigcopier drops."""
    container.run(Step(("apksigcopier", "copy", f"{OUT}/{released}", f"{OUT}/{unsigned}", f"{OUT}/{unsigned}.signed")))
    signed = out / f"{unsigned}.signed"
    signed.write_bytes(copy_v1_flags((out / released).read_bytes(), signed.read_bytes()))
    return signed.read_bytes()


def verify_apk(container: Container, out: Path, released: str, built: str) -> bool:
    """Copies the released APK's signature onto the built one and compares them: onto the APK as built, as AGP signs
    releases, then onto it aligned and padded as apksigner signed the releases before."""
    released_data = (out / released).read_bytes()
    if copy_signature(container, out, released, built) == released_data:
        return True
    pad = ("--pad-like-apksigner",) if apk_padding(released_data) == "apksigner" else ()
    container.run(Step(("repro-apk", "zipalign", "--page-size", "16", *pad, "--replace", f"{OUT}/{built}", f"{OUT}/{built}.aligned")))
    return copy_signature(container, out, released, f"{built}.aligned") == released_data


def reproduce_android(tag: str, arches: Sequence[Arch], nix_cache: bool, source: Path, results: Path) -> list[str]:
    out = results / "android"
    out.mkdir(parents=True, exist_ok=True)
    tags = run(["git", "-C", source, "tag", "--list"], capture=True).stdout.split()
    for arch in arches:
        download(release_url(tag, f"simplex-{arch}.apk"), out / f"simplex-{arch}.apk")
    failed = []
    with android_container(source, out, Arch.ARMV7A in arches) as container:
        for arch in arches:
            released = f"simplex-{arch}.apk"
            signer = release_signer(container, released)
            if signer != SIMPLEX_KEY:
                raise ReproduceError(f"{released} is signed by {signer}, not by the SimpleX key")
            log(f"{released} is signed by the SimpleX key")
            checkout(source, android_build_tag(tag, tags, arch))
            log(f"building {arch}")
            build_apk(container, source, arch, out, f"{released}.built", nix_cache, keep_libs=False)
            if verify_apk(container, out, released, f"{released}.built"):
                log(f"{released} is reproduced")
            else:
                log(f"{released} is NOT reproduced")
                failed.append(released)
    return failed


# ---------------------------------------------------------------------------------------------------------------------
# OpenSSL


def fetch_openssl(work: Path) -> Path:
    """The OpenSSL source tree, from the tarball checked against OPENSSL_SHA256."""
    tarball = fetch_pinned(OPENSSL_URL, OPENSSL_SHA256, work / f"openssl-{OPENSSL_VERSION}.tar.gz")
    with tarfile.open(tarball) as tar:
        tar.extractall(work, filter="data")
    return work / f"openssl-{OPENSSL_VERSION}"


def openssl_build(target: str, prefix: str, openssldir: str, install: bool) -> list[tuple[str, ...]]:
    """The commands that build OpenSSL in its source tree, then install it to prefix when install is set."""
    commands = [
        ("perl", "Configure", target, f"--prefix={prefix}", f"--openssldir={openssldir}", "--libdir=lib", "no-docs", "no-tests"),
        ("make", f"-j{os.cpu_count() or 1}"),
    ]
    if install:
        commands.append(("make", "install_sw"))
    return commands


# ---------------------------------------------------------------------------------------------------------------------
# Desktop and CLI

CLI_DEB_SOURCE_DATE_EPOCH = "1764547200"
DEB_ARCHES = {"x86_64": "amd64", "aarch64": "arm64"}
COMPOSE_ARCHES = {"x86_64": "x64", "aarch64": "arm64"}
# Android only: Mobile.hs exports them, the desktop library doesn't
ANDROID_ONLY_EXPORTS = ("chat_migrate_init_key", "chat_reopen_store")
LINUX_LIB_GHC_OPTIONS = "-optl-Wl,-rpath,$ORIGIN -optl-Wl,-soname,libsimplex.so -flink-rts -threaded"
VLC_RELEASE = "https://github.com/simplex-chat/vlc/releases/download/v3.0.23-2"
VLC_SHA256 = {
    "vlc-linux-x86_64.appimage": "594fc3a603167a977cc0de519e1d5f785037df0db60c57518f88e46606d5bb98",
    "vlc-linux-aarch64.appimage": "697e3d1d0eaad0729529826021fd78b10eee411ba413866c5ab24ac4ac754ba4",
    "vlc-macos-aarch64.zip": "ee537e339b4b40546dc9374db4a2e74eeb46d4b76fb7f23b201e7beb144decb8",
    "vlc-macos-x86_64.zip": "a4a782b8d2f5475ae7a7dda84a2ba4989d415cd4c0a7d19c68e75a90920a0d6b",
    "vlc-win-x86_64.zip": "09993fc4e6bf8239cf847f943ae593dd62ba9abbe5f2f18c42bfca1bc9e94443",
}
# The builds of the continuous releases of simplex-chat/appimagetool and simplex-chat/type2-runtime of 2025-06-04
APPIMAGE_TOOLS = {
    "x86_64": (
        ("appimagetool", "appimagetool-x86_64.AppImage", "ad30156bd2c0a364e742b13bd205021e2408399a9090bc2d76048dd504b23f66"),
        ("type2-runtime", "runtime-x86_64", "df90eadfa40b83a4183bf7a7f6fdf9c293eebea83a614fe48ccd2f849a75ad58"),
    ),
    "aarch64": (
        ("appimagetool", "appimagetool-aarch64.AppImage", "1401a0557ed979b32ca82fb136b2f328ff2fafbb024b2b61e155980605cb3ccb"),
        ("type2-runtime", "runtime-aarch64", "75ad8a177eef47943328e541a57911a79a88446c3a1a0c7a172f2046eabebb9e"),
    ),
}
# jpackage's desktop entry passes no URL and handles no MIME type; this registers the simplexchat: and simplex: schemes
DESKTOP_MIME_TYPES = "MimeType=x-scheme-handler/simplexchat;x-scheme-handler/simplex;"
# Compose extracts the skiko runtime jar's library at build time, with the build time as its timestamp; the app loads
# the library without it (https://youtrack.jetbrains.com/issue/CMP-1971)
SKIKO_RUNTIME = "skiko-awt-runtime-linux"


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
            raise ReproduceError(f'wrong exports in libsimplex.dll.def: add "{name}" to it')
        if sum(f'"{name}"' in line for line in flake) != 1:
            raise ReproduceError(f'wrong exports in flake.nix: add "{name}" to aarch64-android:lib:simplex-chat')


def single_dir(directory: Path, pattern: str) -> Path:
    found = [p for p in sorted(directory.glob(pattern)) if p.is_dir()]
    if len(found) != 1:
        raise ReproduceError(f"expected one {pattern} directory in {directory}, found {len(found)}")
    return found[0]


def touch_tree(root: Path, epoch: str) -> None:
    """Sets the times of root and everything in it, through symlinks as touch does, for deterministic archives."""
    paths = [root]
    for directory, dirs, files in os.walk(root):
        paths += [Path(directory, name) for name in dirs + files]
    for path in paths:
        if path.exists():
            os.utime(path, (int(epoch), int(epoch)))


def edit_lines(path: Path, edit: Callable[[str], str | None]) -> None:
    """Rewrites each line of path, without its newline, with edit; a line that edit maps to None is removed."""
    lines = []
    for line in path.read_text().splitlines(keepends=True):
        text = line.rstrip("\n")
        edited = edit(text)
        if edited is not None:
            lines.append(edited + line[len(text) :])
    path.write_text("".join(lines))


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


def build_linux_lib(runner: Runner, source: Path, arch: str, database: Database, dist_dir: str = "dist-newstyle") -> Path:
    """libsimplex.so and the GHC libraries it loads, in the desktop app's libraries directory, which it returns."""
    check_exports(source)
    cabal = ("cabal", "build", f"--builddir={dist_dir}", "lib:simplex-chat", f"--ghc-options={LINUX_LIB_GHC_OPTIONS}")
    runner.run(Step((*cabal, *client_library_constraints(database)), runner.path(source)))
    build = single_dir(source / dist_dir / "build" / f"{arch}-linux" / f"ghc-{GHC}", "simplex-chat-*") / "build"
    for lib in build.glob(f"libHSsimplex-chat-*-inplace-ghc{GHC}.so"):
        lib.rename(build / "libsimplex.so")
    # Ubuntu 20.04 runners linked libffi.so.7, which users' systems may lack; libffi.so.8 ships with the libraries
    runner.run(Step(("patchelf", "--replace-needed", "libffi.so.7", "libffi.so.8", "libsimplex.so"), runner.path(build)))
    ldd = runner.run(Step(("ldd", "libsimplex.so"), runner.path(build)), capture=True)
    ghc_libs = [line.split()[2] for line in ldd.splitlines() if "ghc" in line]
    deps = build / "deps"
    deps.mkdir(exist_ok=True)
    if ghc_libs:
        runner.run(Step(("cp", *ghc_libs, runner.path(deps)), runner.path(build)))
    libs = desktop_libs(source, f"linux-{arch}")
    shutil.rmtree(libs, ignore_errors=True)
    shutil.rmtree(source / "apps/multiplatform/desktop/build/cmake", ignore_errors=True)
    libs.mkdir(parents=True)
    for path in [*deps.iterdir(), build / "libsimplex.so"]:
        shutil.copy(path, libs)
    return libs


def add_vlc_linux(runner: Runner, libs: Path, arch: str, work: Path) -> None:
    vlc = libs / "vlc"
    if vlc.exists():
        return
    name = f"vlc-linux-{arch}.appimage"
    appimage = fetch_pinned(f"{VLC_RELEASE}/{name}", VLC_SHA256[name], work / name)
    appimage.chmod(0o755)
    runner.run(Step((runner.path(appimage), "--appimage-extract"), runner.path(work)))
    shutil.copytree(work / "squashfs-root/usr/lib", vlc, symlinks=True)


def build_cli_deb(runner: Runner, binary: Path, version: str, arch: str, work: Path) -> Path:
    """The CLI's deb, with the release's control file and fixed timestamps."""
    build = work / "deb-build"
    package = build / "simplex-chat"
    for directory in (build, package, package / "DEBIAN", package / "usr", package / "usr/bin"):
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
    size = f"{(binary.stat().st_size + 1023) / 1024:.0f}"
    control = package / "DEBIAN/control"
    control.write_text(
        "Package: simplex-chat\n"
        f"Version: {version}\n"
        "Section: Messenger\n"
        "Priority: optional\n"
        f"Architecture: {DEB_ARCHES[arch]}\n"
        "Maintainer: SimpleX Chat <chat@simplex.chat>\n"
        "Description: SimpleX - the first messaging platform that has no user identifiers of any kind - 100% private by design! (CLI)\n"
        f"Installed-Size: {size}\n"
    )
    control.chmod(0o644)
    shutil.copy(binary, package / "usr/bin/simplex-chat")
    (package / "usr/bin/simplex-chat").chmod(0o755)
    touch_tree(package, CLI_DEB_SOURCE_DATE_EPOCH)
    epoch = {"SOURCE_DATE_EPOCH": CLI_DEB_SOURCE_DATE_EPOCH}
    runner.run(Step(("dpkg-deb", "--build", "--root-owner-group", "--uniform-compression", "./simplex-chat"), runner.path(build), epoch))
    runner.run(Step(("strip-nondeterminism", "./simplex-chat.deb"), runner.path(build), epoch))
    return build / "simplex-chat.deb"


def remove_skiko_runtime(app: Path) -> None:
    for jar in app.glob(f"lib/app/*{SKIKO_RUNTIME}*"):
        jar.unlink()
    edit_lines(app / "lib/app/simplex.cfg", lambda line: None if SKIKO_RUNTIME in line else line)


def register_url_schemes(line: str) -> str:
    if line.startswith("Exec=") and "%u" not in line and "%U" not in line:
        return f"{line} %u"
    return DESKTOP_MIME_TYPES if line.startswith("MimeType=") else line


def package_desktop_deb(runner: Runner, source: Path, arch: str, codename: str) -> Path:
    """jpackage's deb without the skiko runtime jar, versioned for the Ubuntu release and with fixed timestamps."""
    multiplatform = source / "apps/multiplatform"
    runner.run(Step(("./gradlew", f"-Psimplex.assets.dir={DESKTOP_ENV['ASSETS_DIR']}", "packageDeb"), runner.path(multiplatform)))
    release = multiplatform / "release/main/deb"
    final = release / f"simplex_{arch}.deb"
    jpackage_debs = [p for p in sorted(release.glob("simplex*.deb")) if p != final]
    if len(jpackage_debs) != 1:
        raise ReproduceError(f"expected one deb of jpackage in {release}, found {len(jpackage_debs)}")
    extracted = multiplatform / "extracted"
    shutil.rmtree(extracted, ignore_errors=True)
    runner.run(Step(("dpkg-deb", "-R", runner.path(jpackage_debs[0]), "./extracted"), runner.path(multiplatform)))
    app = single_dir(extracted / "opt", "*imple*")
    remove_skiko_runtime(app)
    edit_lines(extracted / "DEBIAN/control", lambda line: f"{line}~{codename}" if "Version" in line else line)
    for entry in app.glob("lib/*.desktop"):
        edit_lines(entry, register_url_schemes)
        if DESKTOP_MIME_TYPES not in entry.read_text().splitlines():
            raise ReproduceError(f"{entry.name} does not register simplexchat: and simplex:")
    touch_tree(extracted, SOURCE_DATE_EPOCH)
    epoch = {"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}
    build = ("dpkg-deb", "--build", "--root-owner-group", "--uniform-compression", "./extracted", runner.path(final))
    runner.run(Step(build, runner.path(multiplatform), epoch))
    runner.run(Step(("strip-nondeterminism", runner.path(final)), runner.path(multiplatform), epoch))
    shutil.rmtree(extracted)
    return final


def ubuntu_codename(runner: Runner) -> str:
    release = runner.run(Step(("cat", "/etc/os-release")), capture=True)
    for line in release.splitlines():
        if line.startswith("VERSION_CODENAME="):
            return line.split("=", 1)[1].strip('"')
    raise ReproduceError("VERSION_CODENAME is not in /etc/os-release")


def package_appimage(runner: Runner, source: Path, arch: str, libcrypto: Path, work: Path) -> Path:
    """The AppImage of the desktop app, with libcrypto bundled and fixed timestamps."""
    multiplatform = source / "apps/multiplatform"
    libs = desktop_libs(source, f"linux-{arch}")
    bundled = libs / libcrypto.name
    shutil.copy(libcrypto, bundled)
    try:
        runner.run(
            Step(("./gradlew", f"-Psimplex.assets.dir={DESKTOP_ENV['ASSETS_DIR']}", "createDistributable"), runner.path(multiplatform))
        )
    finally:
        bundled.unlink()
    app = multiplatform / "release/main/app"
    appdir = app / "AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    (appdir / "usr").mkdir(parents=True)
    distributable = single_dir(app, "*imple*")
    for part in ("bin", "lib"):
        shutil.copytree(distributable / part, appdir / "usr" / part, symlinks=True)
    shutil.copy(appdir / "usr/lib/simplex.png", appdir)
    for share in ("icons", "metainfo", "applications"):
        (appdir / "usr/share" / share).mkdir(parents=True)
    # AppImageLauncher shows this icon
    shutil.copy(appdir / "usr/lib/simplex.png", appdir / "usr/share/icons")
    (appdir / "AppRun").symlink_to(Path("usr/bin") / single_file(appdir / "usr/bin", "*imple*").name)
    distribute = multiplatform / "desktop/src/jvmMain/resources/distribute"
    entry = appdir / "chat.simplex.app.desktop"
    shutil.copy(single_file(distribute, "*imple*.desktop"), entry)
    edit_lines(entry, lambda line: re.sub(r"^Exec=[^ ]*", "Exec=simplex", re.sub(r"Icon=.*", "Icon=simplex", line)))
    shutil.copy(entry, appdir / "usr/share/applications")
    for appdata in distribute.glob("*.appdata.xml"):
        shutil.copy(appdata, appdir / "usr/share/metainfo")
    tool, runtime = (
        fetch_pinned(f"https://github.com/simplex-chat/{repo}/releases/download/continuous/{name}", digest, work / name)
        for repo, name, digest in APPIMAGE_TOOLS[arch]
    )
    tool.chmod(0o755)
    remove_skiko_runtime(appdir / "usr")
    touch_tree(appdir, SOURCE_DATE_EPOCH)
    epoch = {"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}
    appimagetool = (runner.path(tool), "--verbose", "--no-appstream", "--runtime-file", runner.path(runtime), ".")
    runner.run(Step(appimagetool, runner.path(appdir), epoch))
    image = shutil.move(single_file(appdir, "*imple*.AppImage"), multiplatform / "release/main")
    runner.run(Step(("strip-nondeterminism", runner.path(Path(image))), runner.path(multiplatform), epoch))
    return Path(image)


CLI_BUILD_STEPS = (Step(("cabal", "clean")), Step(("cabal", "update")), Step(("cabal", "build", "-j")))
# Releases made before this script built their Linux files with these scripts of their own, which reproduce runs
TAG_CLI_DEB_SCRIPT = "scripts/desktop/build-cli-deb.sh"
TAG_DESKTOP_DEB_SCRIPT = "scripts/desktop/make-deb-linux.sh"
TAG_APPIMAGE_SCRIPT = "scripts/desktop/make-appimage-linux.sh"


def desktop_files(image: UbuntuImage) -> list[str]:
    cli = f"simplex-chat-ubuntu-{image.suffix}-{image.arch}"
    files = [cli, f"{cli}.deb", f"simplex-desktop-ubuntu-{image.suffix}-{image.arch}.deb"]
    if image.version == APPIMAGE_IMAGE:
        files.append(f"simplex-desktop-{image.arch}.AppImage")
    return files


def linux_libs_zip(arch: str, database: Database) -> str:
    return f"simplex-chat-libs-linux-{arch}{'-postgres' if database == Database.POSTGRES else ''}.zip"


def linux_image(version: str, machine: str) -> UbuntuImage:
    """The image of an Ubuntu version for this machine: Docker runs images of the host's architecture."""
    for image in DESKTOP_IMAGES:
        if (image.version, image.arch) == (version, machine):
            return image
    raise ReproduceError(f"no Ubuntu {version} image for {machine}")


def linux_container(source: Path, image: UbuntuImage, work: Path) -> Container:
    return Container(
        source,
        {source: PROJECT, work: OUT},
        build_args={"TAG": image.version, "HASH": image.digest, "GHC": GHC},
        run_args=["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN", "--security-opt", "apparmor:unconfined"],
    )


def build_desktop_linux(source: Path, image: UbuntuImage, version: str, out: Path) -> None:
    """The Linux release files of an Ubuntu image, with their released names, and on APPIMAGE_IMAGE the libraries."""
    (source / "cabal.project.local").write_text(DESKTOP_CABAL_LOCAL)
    normalize_permissions(source)
    log(f"building Ubuntu {image.version} {image.arch} CLI and desktop apps")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp).resolve()
        with linux_container(source, image, work) as container:
            for step in CLI_BUILD_STEPS:
                container.run(Step(step.argv, container.path(source)))
            cli = container.run(Step(("cabal", "list-bin", "exe:simplex-chat"), container.path(source)), capture=True).strip()
            container.run(Step(("cp", cli, container.path(work / "simplex-chat")), container.path(source)))
            container.run(Step(("strip", container.path(work / "simplex-chat")), container.path(source)))
            shutil.copy(work / "simplex-chat", out / desktop_files(image)[0])
            if (source / TAG_DESKTOP_DEB_SCRIPT).exists():
                build_desktop_linux_with_tag_scripts(container, source, image, version, work, out)
                return
            shutil.copy(build_cli_deb(container, work / "simplex-chat", version, image.arch, work), out / desktop_files(image)[1])
            libs = build_linux_lib(container, source, image.arch, Database.SQLITE)
            add_vlc_linux(container, libs, image.arch, work)
            link_desktop_libs(source, f"linux-{image.arch}", f"linux-{COMPOSE_ARCHES[image.arch]}")
            deb = package_desktop_deb(container, source, image.arch, ubuntu_codename(container))
            shutil.copy(deb, out / desktop_files(image)[2])
            if image.version == APPIMAGE_IMAGE:
                zip_desktop_libs(libs, [], out / linux_libs_zip(image.arch, Database.SQLITE))
                openssl = fetch_openssl(work)
                build = openssl_build(f"linux-{image.arch}", OPENSSL_UNIX_PREFIX, OPENSSL_UNIX_DIR, install=False)
                for argv in build:
                    container.run(Step(argv, container.path(openssl), {"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}))
                appimage = package_appimage(container, source, image.arch, openssl / "libcrypto.so.3", work)
                shutil.copy(appimage, out / desktop_files(image)[3])


def build_desktop_linux_with_tag_scripts(
    container: Container, source: Path, image: UbuntuImage, version: str, work: Path, out: Path
) -> None:
    """The CLI deb, the desktop deb and the AppImage of a release made before this script, by its own scripts."""
    files = desktop_files(image)
    deb_build = work / "deb-build"
    cli_deb = (TAG_CLI_DEB_SCRIPT, version, container.path(work / "simplex-chat"), container.path(deb_build))
    container.run(Step(cli_deb, container.path(source)))
    shutil.copy(deb_build / "simplex-chat.deb", out / files[1])
    container.run(Step((TAG_DESKTOP_DEB_SCRIPT,), container.path(source), DESKTOP_ENV))
    release = source / DESKTOP_RELEASE_DIR
    shutil.copy(release / f"deb/simplex_{image.arch}.deb", out / files[2])
    if image.version == APPIMAGE_IMAGE:
        container.run(Step((TAG_APPIMAGE_SCRIPT,), container.path(source), DESKTOP_ENV))
        shutil.copy(single_file(release, "*imple*.AppImage"), out / files[3])


@dataclass(frozen=True, slots=True)
class LibsBuilder:
    """Where the library release is built: this machine, a running container, or else a new one of the image."""

    on_host: bool = False
    container: str | None = None


def build_linux_libs(source: Path, image: UbuntuImage, database: Database, dist_dir: str, builder: LibsBuilder, out: Path) -> None:
    """The library release: the desktop libraries without VLC."""
    (source / "cabal.project.local").write_text(DESKTOP_CABAL_LOCAL)
    with contextlib.ExitStack() as stack:
        runner: Runner
        if builder.on_host:
            runner = HostRunner()
        elif builder.container:
            runner = Container.running(builder.container, source)
        else:
            normalize_permissions(source)
            work = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
            runner = stack.enter_context(linux_container(source, image, work))
        runner.run(Step(("cabal", "update"), runner.path(source)))
        libs = build_linux_lib(runner, source, image.arch, database, dist_dir)
    zip_desktop_libs(libs, [], out / linux_libs_zip(image.arch, database))


def reproduce_desktop(tag: str, source: Path, results: Path) -> list[str]:
    built = results / "from-source"
    released = results / "prebuilt"
    built.mkdir(parents=True, exist_ok=True)
    released.mkdir(parents=True, exist_ok=True)
    for image in (image for image in DESKTOP_IMAGES if image.arch == platform.machine()):
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
# macOS and Windows desktop


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


def openssl_cabal_local(include: str, lib: str, simplex_chat_ghc_options: str = "") -> str:
    """cabal.project.local that builds simplexmq and direct-sqlcipher with the OpenSSL in include and lib."""
    text = "ignore-project: False\n"
    for package, flags in (("simplexmq", ""), ("direct-sqlcipher", "    flags: +openssl\n")):
        text += f"package {package}\n    extra-include-dirs: {include}\n    extra-lib-dirs: {lib}\n{flags}"
    if simplex_chat_ghc_options:
        text += f"package simplex-chat\n    ghc-options: {simplex_chat_ghc_options}\n"
    return text


def build_openssl(source: Path, target: str, prefix: str, openssldir: str, install: bool) -> None:
    for argv in openssl_build(target, prefix, openssldir, install):
        run(argv, cwd=source, env={"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH})


@dataclass(frozen=True, slots=True)
class OpenSsl:
    """A build of the pinned OpenSSL: its headers, and the directory with its libraries."""

    include: Path
    lib: Path

    def cabal_local(self, simplex_chat_ghc_options: str = "") -> str:
        return openssl_cabal_local(self.include.as_posix(), self.lib.as_posix(), simplex_chat_ghc_options)


def macos_desktop() -> HostDesktop:
    host = MACOS_DESKTOPS.get(platform.machine())
    if host is None:
        raise ReproduceError(f"no macOS build for {platform.machine()}")
    return host


def host_openssl(work: Path) -> OpenSsl:
    """The pinned OpenSSL built in work for this Mac, installed, or for Windows, in its source tree."""
    match host_platform():
        case Platform.LINUX:
            raise ReproduceError("Linux builds use the system's OpenSSL")
        case Platform.WINDOWS:
            source = fetch_openssl(work)
            build_openssl(source, WINDOWS_DESKTOP.openssl_target, OPENSSL_WINDOWS_PREFIX, OPENSSL_WINDOWS_DIR, install=False)
            return OpenSsl(source / "include", source)
    source = fetch_openssl(work)
    prefix = work / "openssl"
    build_openssl(source, macos_desktop().openssl_target, str(prefix), OPENSSL_UNIX_DIR, install=True)
    return OpenSsl(prefix / "include", prefix / "lib")


def static_libcrypto(lib: Path, work: Path) -> Path:
    """A directory with only the static libcrypto, so that the linker can't choose the shared one."""
    static = work / "openssl-static"
    static.mkdir()
    shutil.copy(lib / "libcrypto.a", static)
    return static


def build_host_cli(source: Path, cabal_local: str) -> Path:
    (source / "cabal.project.local").write_text(cabal_local)
    for step in CLI_BUILD_STEPS:
        run(step.argv, cwd=source)
    return Path(run(["cabal", "list-bin", "exe:simplex-chat"], cwd=source, capture=True).stdout.strip().splitlines()[-1])


def check_static_openssl(binary: Path, dependencies: str) -> None:
    """dependencies: what otool -L or objdump -p prints of the binary."""
    if "libcrypto" in dependencies:
        raise ReproduceError(f"{binary} loads libcrypto, instead of linking the pinned OpenSSL statically")


def zip_desktop_libs(libs: Path, extra: Sequence[Path], target: Path) -> None:
    """The library release: the files directly in the desktop app's libraries directory, under libs/."""
    files = sorted([p for p in libs.iterdir() if p.is_file()] + list(extra))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files:
            z.write(path, f"libs/{path.name}")


def apple_signing(require: bool) -> bool:
    """Whether the APPLE_SIGNING_ENV variables, which the keychain, signing and notarization read, are all set."""
    present = [name for name in APPLE_SIGNING_ENV if os.environ.get(name)]
    if len(present) == len(APPLE_SIGNING_ENV):
        return True
    names = ", ".join(APPLE_SIGNING_ENV)
    if present:
        raise ReproduceError(f"set all of {names} to sign, or none; set: {', '.join(present)}")
    if require:
        raise ReproduceError(f"{names} are not set, and --require-signing is given")
    warn(f"{names} are not set: building an unsigned, not notarized dmg and libraries")
    return False


def otool(flag: str, lib: Path) -> list[str]:
    return run(["otool", flag, lib], capture=True).stdout.splitlines()


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
        raise ReproduceError(f"{smplxmq.name} doesn't load libcrypto")
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
            raise ReproduceError(message)
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
    """Submits a zip of path to Apple's notary service, and fails unless it is accepted."""
    archive = work / f"{path.name}.zip"
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
        raise ReproduceError(f"notarization of {path.name} is {result['status']}")


def sign_mac_libs(libs: Path, keychain: Path, work: Path) -> None:
    for lib in sorted(libs.glob("*.dylib")):
        run(["codesign", "--remove-signature", lib])
        run(["codesign", "--timestamp", "--options", "runtime", "--force", "--sign", APPLE_IDENTITY, "--keychain", keychain, lib])
    notarize(libs, work)


def package_dmg(source: Path, keychain: Path | None) -> Path:
    """The desktop app's dmg, signed and notarized by Gradle with the keychain."""
    multiplatform = source / "apps/multiplatform"
    properties = multiplatform / "local.properties"
    tasks = ["packageDmg"]
    if keychain:
        properties.write_text(
            f"desktop.mac.signing.identity={APPLE_IDENTITY}\n"
            f"desktop.mac.signing.keychain={keychain}\n"
            f"desktop.mac.notarization.apple_id={os.environ['APPLE_SIMPLEX_NOTARIZATION_APPLE_ID']}\n"
            f"desktop.mac.notarization.password={os.environ['APPLE_SIMPLEX_NOTARIZATION_PASSWORD']}\n"
            f"desktop.mac.notarization.team_id={APPLE_TEAM}\n"
        )
        tasks.append("notarizeDmg")
    try:
        for task in tasks:
            run(["./gradlew", f"-Psimplex.assets.dir={DESKTOP_ENV['ASSETS_DIR']}", task], cwd=multiplatform)
    finally:
        properties.unlink(missing_ok=True)
    return single_file(source / DESKTOP_RELEASE_DIR / "dmg", "SimpleX-*.dmg")


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
        release_macos_libs(source, host_openssl(work), database, keychain, work, out)


def build_desktop_macos(source: Path, out: Path, require_signing: bool) -> None:
    host = macos_desktop()
    signed = apple_signing(require_signing)
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        work = Path(tmp).resolve()
        keychain = stack.enter_context(apple_keychain(work)) if signed else None
        openssl = host_openssl(work)
        static = OpenSsl(openssl.include, static_libcrypto(openssl.lib, work))
        cli = build_host_cli(source, static.cabal_local())
        check_static_openssl(cli, run(["otool", "-L", cli], capture=True).stdout)
        shutil.copy(cli, out / host.cli)
        libs = release_macos_libs(source, openssl, Database.SQLITE, keychain, work, out)
        add_vlc_mac(libs, host.arch, work)
        shutil.copy(package_dmg(source, keychain), out / host.desktop)


def build_host_openssl(source: Path, out: Path) -> None:
    """The pinned OpenSSL in out, and cabal.project.local that links it, for CI's test builds on macOS and Windows."""
    (source / "cabal.project.local").write_text(host_openssl(out).cabal_local())


def remove_cabal_sources(source: Path) -> None:
    """cabal fails on Windows when these checkouts exist, and can't delete simplexmq's read-only git pack files."""

    def make_writable(function: Callable[[str], object], path: str, error: BaseException) -> None:
        # Windows refuses to delete read-only files, POSIX to delete entries of read-only directories
        os.chmod(os.path.dirname(path), stat.S_IRWXU)
        os.chmod(path, stat.S_IRWXU)
        function(path)

    for path in [*source.glob("dist-newstyle/src/direct-sq*"), *source.glob("dist-newstyle/src/simplexmq*")]:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, onexc=make_writable)
        else:
            path.unlink()


def disable_ghc_lld() -> None:
    """With GHC's lld, linking libsimplex.dll can't find the exported symbols; a missing linker makes GHC use ld."""
    settings = Path(run(["ghc", "--print-libdir"], capture=True).stdout.strip()) / "settings"
    edit_lines(settings, lambda line: line.replace("ld.lld.exe", "abracadabra.exe", 1))


def build_windows_lib(source: Path, openssl: OpenSsl) -> Path:
    """libsimplex.dll and the libcrypto it loads, in the desktop app's libraries directory, which it returns."""
    check_exports(source)
    for old in source.glob("dist-newstyle/build/x86_64-windows/ghc-*/simplex-chat-*"):
        shutil.rmtree(old)
    remove_cabal_sources(source)
    options = f"-shared -threaded -optl-L{openssl.lib.as_posix()} -optl-lcrypto-3-x64 -o libsimplex.dll libsimplex.dll.def"
    (source / "cabal.project.local").write_text(openssl.cabal_local(options))
    disable_ghc_lld()
    run(["cabal", "build", "lib:simplex-chat", *client_library_constraints(Database.SQLITE)], cwd=source)
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
    with tempfile.TemporaryDirectory() as tmp:
        release_windows_libs(source, host_openssl(Path(tmp).resolve()), out)


def build_desktop_windows(source: Path, out: Path) -> None:
    host = WINDOWS_DESKTOP
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp).resolve()
        openssl = host_openssl(work)
        static = OpenSsl(openssl.include, static_libcrypto(openssl.lib, work))
        remove_cabal_sources(source)
        cli = build_host_cli(source, static.cabal_local(OPENSSL_WINDOWS_STATIC_LINK))
        remove_cabal_sources(source)
        check_static_openssl(cli, run(["objdump", "-p", cli], capture=True).stdout)
        shutil.copy(cli, out / host.cli)
        libs = release_windows_libs(source, openssl, out)
        add_vlc_windows(libs, work)
        link_desktop_libs(source, host.libs_dir, "windows-x64")
        assets = f"-Psimplex.assets.dir={DESKTOP_ENV['ASSETS_DIR']}"
        run(["bash", "./gradlew", assets, "packageMsi"], cwd=source / "apps/multiplatform")
        shutil.copy(single_file(source / DESKTOP_RELEASE_DIR / "msi", "*imple*.msi"), out / host.desktop)


# ---------------------------------------------------------------------------------------------------------------------
# iOS


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def minimum_versions(otool_output: str) -> list[str]:
    """Minimum OS versions of the objects in otool -l output: LC_BUILD_VERSION's minos, LC_VERSION_MIN_*'s version."""
    versions = []
    in_version_min = False
    for line in otool_output.splitlines():
        match line.split():
            case ["minos", version, *_]:
                versions.append(version)
            case ["cmd", command, *_]:
                in_version_min = command.startswith("LC_VERSION_MIN_")
            case ["version", version, *_] if in_version_min:
                versions.append(version)
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


def nix_env() -> dict[str, str]:
    """What nix-daemon.sh sets for running nix: its bin on PATH and a certificate bundle."""
    env = {"PATH": f"{NIX_PROFILE / 'bin'}:{os.environ.get('PATH', '')}"}
    if "NIX_SSL_CERT_FILE" not in os.environ:
        cert = next((c for c in NIX_CERT_FILES if c.is_file()), None)
        if cert is None:
            raise ReproduceError("no certificate bundle for Nix found")
        env["NIX_SSL_CERT_FILE"] = str(cert)
    return env


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


def build_mac2ios(work: Path) -> Path:
    """zw3rk's mac2ios, which relabels the objects of macOS libraries as iOS or iOS simulator ones."""
    for name, digest in MAC2IOS_SHA256.items():
        fetch_pinned(f"{MAC2IOS_SOURCE}/{name}", digest, work / name)
    run(["cc", "-O2", "-o", work / "mac2ios", work / "mac2ios.c"])
    return work / "mac2ios"


def check_simulator_libs(sim: Path) -> None:
    for lib in sorted(sim.glob("*.a")):
        output = run(["otool", "-l", lib], capture=True, check=False).stdout
        platforms = {line.split()[1] for line in output.splitlines() if line.split()[:1] == ["platform"]}
        if platforms - set(IOS_SIMULATOR_PLATFORMS):
            raise ReproduceError(f"{lib.name} has objects for platforms {sorted(platforms)}, not only the iOS simulator")


def library_names(names: Sequence[str]) -> tuple[str, str] | None:
    """The plain and the -ghc library of libHSsimplex-chat among names."""
    plain = sorted({name for name in names if "-ghc" not in name})
    ghc = sorted({name for name in names if "-ghc" in name})
    return (plain[-1], ghc[-1]) if plain and ghc else None


def update_pbxproj(source: Path) -> None:
    """Points project.pbxproj, which names the libHSsimplex-chat libraries with their versions, at the installed ones."""
    pbxproj = source / IOS_PBXPROJ
    text = pbxproj.read_text()
    new = library_names([lib.name for lib in (source / IOS_LIBRARIES / "ios").glob("libHSsimplex-chat-*.a")])
    old = library_names(re.findall(r'libHSsimplex-chat-[^ "/]+\.a', text))
    if new is None or old is None:
        raise ReproduceError(f"no libHSsimplex-chat libraries in {IOS_LIBRARIES}/ios or in {IOS_PBXPROJ}")
    if old == new:
        return

    def references(name: str, text: str) -> int:
        return sum(name in line for line in text.splitlines())

    if [references(name, text) for name in old] != [4, 4]:
        raise ReproduceError(f"expected 4 references to each of {', '.join(old)} in {IOS_PBXPROJ}")
    # the -ghc name contains the plain one's version, so it is replaced first
    text = text.replace(old[1], new[1]).replace(old[0], new[0])
    if [references(name, text) for name in new] != [4, 4]:
        raise ReproduceError(f"expected 4 references to each of {', '.join(new)} after updating {IOS_PBXPROJ}")
    pbxproj.write_text(text)
    log(f"{IOS_PBXPROJ}: {old[0]} -> {new[0]}")


def install_ios_libs(source: Path, package: Path, work: Path) -> None:
    """The libraries of an iOS core package for macOS, iOS and the iOS simulator, where the Xcode project links them."""
    libraries = source / IOS_LIBRARIES
    mac2ios = build_mac2ios(work)
    mac, ios, sim = (libraries / name for name in ("mac", "ios", "sim"))
    for directory in (mac, ios, sim):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
    with zipfile.ZipFile(package) as z:
        z.extractall(mac)
    for lib in sorted(mac.iterdir()):
        lib.chmod(lib.stat().st_mode | stat.S_IWUSR)
        shutil.copy(lib, ios)
        shutil.copy(lib, sim)
    for lib in sorted(ios.iterdir()):
        run([mac2ios, lib], capture=True)
    for lib in sorted(sim.iterdir()):
        run([mac2ios, "-s", lib], capture=True)
    check_simulator_libs(sim)
    update_pbxproj(source)


def cabal_version(source: Path) -> str:
    match = re.search(r"^version:\s*(\S+)", (source / "simplex-chat.cabal").read_text(), re.MULTILINE)
    if match is None:
        raise ReproduceError("no version in simplex-chat.cabal")
    return match.group(1)


def build_ios_sim_libs(source: Path, out: Path) -> None:
    """The iOS core package built with cabal, which the simulator tests use instead of the slower Nix build."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp).resolve()
        openssl = host_openssl(work)
        cabal_local = IOS_SIM_CABAL_LOCAL.format(include=openssl.include, lib=openssl.lib)
        (source / "cabal.project.local").write_text(cabal_local)
        run(["cabal", "build", "-j", "lib:simplex-chat"], cwd=source)
        ghc = run(["ghc", "--numeric-version"], capture=True).stdout.strip()
        build = source / f"dist-newstyle/build/aarch64-osx/ghc-{ghc}/simplex-chat-{cabal_version(source)}/build"
        ffi, gmp = (Path(run(["brew", "--prefix", name], capture=True).stdout.strip()) / "lib" for name in ("libffi", "gmp"))
        package = work / "package"
        package.mkdir()
        for lib in [*build.glob("libHSsimplex-chat-*.a"), ffi / "libffi.a", gmp / "libgmp.a", gmp / "libgmpxx.a"]:
            shutil.copy(lib, package)
            (package / lib.name).chmod(0o644)
        for lib in package.glob("libHSsimplex-chat-*.a"):
            run(["strip", "-x", lib])
        with zipfile.ZipFile(out / IOS_CORE_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for lib in sorted(package.iterdir()):
                z.write(lib, lib.name)


def build_ios_archive(source: Path, art: Path, out: Path, install: bool) -> None:
    if not (art / "ios/Assets.xcassets").is_dir():
        raise ReproduceError(f"no ios/Assets.xcassets in {art}")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if install:
            install_nix(work)
        run(["nix", "build", "-L", "--out-link", work / "core", IOS_CORE_JOB], cwd=source, env=nix_env())
        install_ios_libs(source, work / "core" / IOS_CORE_ZIP, work)

        # The brand assets, as release-mobile.py adds them: Release.xcconfig includes Local.xcconfig, which turns on
        # SIMPLEX_ASSETS. copy-assets.sh runs first, as its build phase runs after the extensions' catalogs are built.
        (source / "apps/ios/Local.xcconfig").write_text(IOS_LOCAL_XCCONFIG.format(art=art))
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
# Commands


@dataclass(frozen=True, slots=True)
class Reproduce:
    tag: str
    targets: tuple[Target, ...]
    arches: tuple[Arch, ...]
    nix_cache: bool


@dataclass(frozen=True, slots=True)
class BuildApk:
    arch: Arch
    out: Path
    source: Path
    nix_cache: bool


@dataclass(frozen=True, slots=True)
class BuildBundle:
    libs: Path
    out: Path
    source: Path


@dataclass(frozen=True, slots=True)
class BuildDesktopLinux:
    image: UbuntuImage
    version: str
    out: Path
    source: Path


@dataclass(frozen=True, slots=True)
class BuildLinuxLibs:
    image: UbuntuImage
    database: Database
    dist_dir: str
    builder: LibsBuilder
    out: Path
    source: Path


@dataclass(frozen=True, slots=True)
class BuildMacosLibs:
    database: Database
    out: Path
    source: Path
    require_signing: bool


@dataclass(frozen=True, slots=True)
class BuildWindowsLibs:
    out: Path
    source: Path


@dataclass(frozen=True, slots=True)
class BuildOpenssl:
    platform: Platform
    out: Path
    source: Path


@dataclass(frozen=True, slots=True)
class BuildAndroidLibs:
    arch: Arch
    out: Path
    source: Path
    install_nix: bool


@dataclass(frozen=True, slots=True)
class BuildIosSimLibs:
    out: Path
    source: Path


@dataclass(frozen=True, slots=True)
class InstallLibs:
    """Prebuilt core libraries put where the apps' builds take them."""

    platform: str
    package: Path
    arch: Arch
    source: Path


@dataclass(frozen=True, slots=True)
class BuildDesktopMacos:
    out: Path
    source: Path
    require_signing: bool


@dataclass(frozen=True, slots=True)
class BuildDesktopWindows:
    out: Path
    source: Path


@dataclass(frozen=True, slots=True)
class BuildIosArchive:
    art: Path
    out: Path
    source: Path
    install_nix: bool


# An assignment rather than a type statement, so that older Pythons parse the file and print the version check
Build = (
    BuildApk
    | BuildBundle
    | BuildDesktopLinux
    | BuildLinuxLibs
    | BuildMacosLibs
    | BuildWindowsLibs
    | BuildOpenssl
    | BuildAndroidLibs
    | BuildIosSimLibs
    | BuildDesktopMacos
    | BuildDesktopWindows
    | BuildIosArchive
)
Command = Reproduce | Build | InstallLibs


def reproduce(command: Reproduce) -> None:
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
        add_summary(f"{command.tag} is not reproduced: {', '.join(f'`{name}`' for name in failed)}\n")
        raise ReproduceError(f"{command.tag} is not reproduced: {', '.join(failed)}")
    add_summary(f"{command.tag} is reproduced: {', '.join(command.targets)}\n")
    log(f"{command.tag} is reproduced: {', '.join(command.targets)}")


def prepare_out(out: Path) -> Path:
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out


def build(command: Build) -> None:
    out = prepare_out(command.out)
    source = command.source.resolve()
    match command:
        case BuildApk(arch=arch, nix_cache=nix_cache):
            require_tools(["docker"])
            signing = android_signing()
            with android_container(source, out, arch == Arch.ARMV7A, signing) as container:
                build_apk(container, source, arch, out, f"simplex-{arch}.apk", nix_cache, keep_libs=True, signing=signing)
        case BuildBundle(libs=libs):
            require_tools(["docker"])
            signing = android_signing()
            with android_container(source, out, set_pid_max=False, signing=signing) as container:
                build_bundle(container, source, libs.resolve(), out, signing)
        case BuildDesktopLinux(image=image, version=version):
            require_platform(Platform.LINUX)
            require_tools(["docker"])
            build_desktop_linux(source, image, version, out)
        case BuildLinuxLibs(image=image, database=database, dist_dir=dist_dir, builder=builder):
            require_platform(Platform.LINUX)
            require_tools(["cabal", "ghc", "patchelf", "ldd"] if builder.on_host else ["docker"])
            build_linux_libs(source, image, database, dist_dir, builder, out)
        case BuildMacosLibs(database=database, require_signing=require_signing):
            require_platform(Platform.MACOS)
            require_tools(["cabal", "ghc", "perl", "make", "otool", "install_name_tool"])
            build_macos_libs(source, database, out, require_signing)
        case BuildWindowsLibs():
            require_platform(Platform.WINDOWS)
            require_tools(["bash", "cabal", "ghc", "perl", "make", "gcc", "dlltool"])
            build_windows_libs(source, out)
        case BuildAndroidLibs(arch=arch, install_nix=install):
            require_platform(Platform.LINUX)
            if not install:
                require_tools(["nix"])
            build_android_libs(source, arch, out, install)
        case BuildIosSimLibs():
            require_platform(Platform.MACOS)
            require_tools(["cabal", "ghc", "brew", "perl", "make", "strip"])
            build_ios_sim_libs(source, out)
        case BuildOpenssl(platform=openssl_platform):
            require_platform(openssl_platform)
            require_tools(["perl", "make"])
            build_host_openssl(source, out)
        case BuildDesktopMacos(require_signing=require_signing):
            require_platform(Platform.MACOS)
            require_tools(["cabal", "ghc", "perl", "make", "otool"])
            build_desktop_macos(source, out, require_signing)
        case BuildDesktopWindows():
            require_platform(Platform.WINDOWS)
            require_tools(["bash", "cabal", "ghc", "perl", "make", "gcc", "objdump", "dlltool"])
            build_desktop_windows(source, out)
        case BuildIosArchive(art=art, install_nix=install):
            require_platform(Platform.MACOS)
            require_tools(IOS_TOOLS)
            if not install and not (NIX_PROFILE / "bin/nix").exists():
                raise ReproduceError(f"Nix is not installed ({NIX_PROFILE / 'bin/nix'} not found): install it, or pass --install-nix")
            build_ios_archive(source, art.resolve(), out, install)
    write_sha256sums(out)


def install(command: InstallLibs) -> None:
    source = command.source.resolve()
    package = command.package.resolve()
    match command.platform:
        case "linux":
            install_linux_libs(source, package)
        case "android":
            install_android_libs(source, command.arch, package)
        case _:
            require_platform(Platform.MACOS)
            require_tools(["cc", "otool"])
            with tempfile.TemporaryDirectory() as tmp:
                install_ios_libs(source, package, Path(tmp))


def execute(command: Command) -> None:
    match command:
        case Reproduce():
            reproduce(command)
        case InstallLibs():
            install(command)
        case _:
            build(command)


def parse_build_args(argv: Sequence[str]) -> Command:
    parser = argparse.ArgumentParser(prog="simplex-chat-build.py build", description="Build one commit's release files.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", type=Path, required=True)
    common.add_argument("--source", type=Path, default=Path("."), help="the simplex-chat checkout (default: current directory)")
    kinds = parser.add_subparsers(dest="kind", required=True)
    ubuntu_versions = sorted({image.version for image in DESKTOP_IMAGES})

    desktop = kinds.add_parser("desktop", help="all release files of this machine's platform").add_subparsers(
        dest="platform", required=True
    )
    desktop_linux = desktop.add_parser("linux", parents=[common], help="the CLI, its deb, the desktop deb, the AppImage and libraries")
    desktop_linux.add_argument("image", choices=ubuntu_versions, help="the Ubuntu version")
    desktop_linux.add_argument("--version", required=True, help="the release version of the CLI deb, e.g. 7.1.0")
    desktop_macos = desktop.add_parser("macos", parents=[common], help="the CLI, dmg and libraries, signed when APPLE_SIMPLEX_* are set")
    desktop.add_parser("windows", parents=[common], help="the CLI, msi and libraries, in an MSYS2 UCRT64 shell")

    libs = kinds.add_parser("libs", help="the core library release of this machine's platform").add_subparsers(
        dest="platform", required=True
    )
    libs_linux = libs.add_parser("linux", parents=[common], help="without VLC, in Docker unless --host or --container")
    libs_linux.add_argument("--ubuntu", choices=ubuntu_versions, default=APPIMAGE_IMAGE)
    libs_linux.add_argument("--dist-dir", default="dist-newstyle", help="cabal's build directory")
    where = libs_linux.add_mutually_exclusive_group()
    where.add_argument("--host", action="store_true", help="build on this machine, with its GHC, rather than in Docker")
    where.add_argument("--container", help="build in this running container, which has the source at /project")
    libs_macos = libs.add_parser("macos", parents=[common], help="signed when APPLE_SIMPLEX_* are set, and linked for the app")
    for platform_libs in (libs_linux, libs_macos):
        platform_libs.add_argument("--database", type=Database, choices=list(Database), default=Database.SQLITE)
    libs.add_parser("windows", parents=[common], help="in an MSYS2 UCRT64 shell")
    libs_android = libs.add_parser("android", parents=[common], help="the APK's core libraries, built with Nix on this machine")
    libs_android.add_argument("--arch", type=Arch, choices=list(Arch), default=Arch.AARCH64)
    libs_android.add_argument("--install-nix", action="store_true", help="install Nix first, as on a CI runner")
    libs.add_parser("ios-sim", parents=[common], help="the iOS core package built with cabal, for the simulator tests")
    for signed in (desktop_macos, libs_macos):
        signed.add_argument("--require-signing", action="store_true", help="fail rather than build unsigned")

    openssl = kinds.add_parser("openssl", help="the pinned OpenSSL, and cabal.project.local linking it").add_subparsers(
        dest="platform", required=True
    )
    for name in (Platform.MACOS, Platform.WINDOWS):
        openssl.add_parser(name, parents=[common])

    apk = kinds.add_parser("apk", parents=[common], help="the deterministic APK and its core libraries, signed when ANDROID_* are set")
    apk.add_argument("arch", type=Arch, choices=list(Arch))
    apk.add_argument("--nix-cache", action="store_true", help="substitute from zw3rk's and IOG's caches")
    aab = kinds.add_parser("aab", parents=[common], help="the Play bundle, from both arches' core libraries")
    aab.add_argument("--libs", type=Path, required=True, help="directory with the pkg-<arch>-android-*.zip files")
    xcarchive = kinds.add_parser("xcarchive", parents=[common], help="the iOS archive, its bundles signed ad hoc with their entitlements")
    xcarchive.add_argument("--art", type=Path, required=True, help="checkout of simplex-chat-art, with the brand assets")
    xcarchive.add_argument("--install-nix", action="store_true", help="install Nix first, as on a CI runner")

    args = parser.parse_args(argv)
    match args.kind, getattr(args, "platform", None):
        case "desktop", Platform.LINUX:
            return BuildDesktopLinux(linux_image(args.image, platform.machine()), args.version, args.out, args.source)
        case "desktop", Platform.MACOS:
            return BuildDesktopMacos(args.out, args.source, args.require_signing)
        case "desktop", Platform.WINDOWS:
            return BuildDesktopWindows(args.out, args.source)
        case "libs", Platform.LINUX:
            image = linux_image(args.ubuntu, platform.machine())
            builder = LibsBuilder(args.host, args.container)
            return BuildLinuxLibs(image, args.database, args.dist_dir, builder, args.out, args.source)
        case "libs", Platform.MACOS:
            return BuildMacosLibs(args.database, args.out, args.source, args.require_signing)
        case "libs", Platform.WINDOWS:
            return BuildWindowsLibs(args.out, args.source)
        case "libs", "android":
            return BuildAndroidLibs(args.arch, args.out, args.source, args.install_nix)
        case "libs", "ios-sim":
            return BuildIosSimLibs(args.out, args.source)
        case "openssl", str(openssl_platform):
            return BuildOpenssl(Platform(openssl_platform), args.out, args.source)
        case "apk", _:
            return BuildApk(args.arch, args.out, args.source, args.nix_cache)
        case "aab", _:
            return BuildBundle(args.libs, args.out, args.source)
        case _:
            return BuildIosArchive(args.art, args.out, args.source, args.install_nix)


def parse_install_args(argv: Sequence[str]) -> InstallLibs:
    parser = argparse.ArgumentParser(prog="simplex-chat-build.py install", description="Install prebuilt core libraries.")
    kinds = parser.add_subparsers(dest="kind", required=True)
    libs = kinds.add_parser("libs", help="the core libraries, where the apps' builds take them").add_subparsers(
        dest="platform", required=True
    )
    for name, package, help in (
        ("linux", "the library zip of build libs linux", "the desktop app's libraries, linked for its Gradle build"),
        ("android", "the directory with the pkg-<arch>-android-*.zip of build libs android", "the APK's libraries"),
        ("ios", "the iOS core package", "for macOS, iOS and the simulator, with project.pbxproj pointing at them"),
    ):
        sub = libs.add_parser(name, help=help)
        sub.add_argument("package", type=Path, help=package)
        sub.add_argument("--source", type=Path, default=Path("."), help="the simplex-chat checkout (default: current directory)")
        if name == "android":
            sub.add_argument("--arch", type=Arch, choices=list(Arch), default=Arch.AARCH64)
    args = parser.parse_args(argv)
    return InstallLibs(args.platform, args.package, getattr(args, "arch", Arch.AARCH64), args.source)


def parse_arches(value: str) -> tuple[Arch, ...]:
    try:
        return tuple(Arch(arch) for arch in value.split(","))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{e}, choose from {', '.join(Arch)}") from e


def parse_args(argv: Sequence[str]) -> Command:
    match argv[:1]:
        case ["build"]:
            return parse_build_args(argv[1:])
        case ["reproduce"]:
            return parse_reproduce_args(argv[1:])
        case ["install"]:
            return parse_install_args(argv[1:])
    parser = argparse.ArgumentParser(usage="simplex-chat-build.py {build,install,reproduce} ...", description=__doc__)
    parser.error("the first argument is build, install or reproduce")


def parse_reproduce_args(argv: Sequence[str]) -> Reproduce:
    parser = argparse.ArgumentParser(
        prog="simplex-chat-build.py reproduce", description="Reproduce a SimpleX Chat release and compare it with the released files."
    )
    parser.add_argument("tag", help="release tag, e.g. v7.1.0")
    parser.add_argument("targets", nargs="*", type=Target, choices=list(Target), help="default: desktop")
    parser.add_argument("--arches", type=parse_arches, default=tuple(Arch), help="Android arches, e.g. aarch64,armv7a")
    parser.add_argument("--nix-cache", action="store_true", help="substitute the Android toolchains from zw3rk's and IOG's caches")
    args = parser.parse_args(argv)
    return Reproduce(args.tag, tuple(args.targets) or (Target.DESKTOP,), args.arches, args.nix_cache)


def main(argv: Sequence[str]) -> int:
    command = parse_args(argv)
    os.environ["DOCKER_BUILDKIT"] = "1"
    try:
        execute(command)
    except ReproduceError as e:
        error(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
