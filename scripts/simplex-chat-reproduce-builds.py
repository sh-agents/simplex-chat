#!/usr/bin/env python3

"""Builds SimpleX Chat release files and reproduces releases.

Reproduce a release (desktop is the default target):
    simplex-chat-reproduce-builds.py <tag> [desktop] [android] [--arches aarch64,armv7a] [--nix-cache]

Build one commit's release files, as CI does:
    simplex-chat-reproduce-builds.py build android-apk <aarch64|armv7a> --out <dir> [--source <dir>] [--nix-cache]
    simplex-chat-reproduce-builds.py build android-bundle --libs <dir> --out <dir> [--source <dir>]
    simplex-chat-reproduce-builds.py build desktop-linux <22.04|24.04> --version <x.y.z> --out <dir> [--source <dir>]
    simplex-chat-reproduce-builds.py build ios-archive --art <simplex-chat-art> --out <dir> [--source <dir>] [--install-nix]

Needs Python 3.12+, git and Docker; ios-archive needs macOS with Xcode and Nix. Results go to ./<tag>-simplex-chat.
The Android builds are signed by Gradle when ANDROID_KEYSTORE, ANDROID_KEYSTORE_PASSWORD, ANDROID_KEY_ALIAS and
ANDROID_KEY_PASSWORD are set, and unsigned otherwise.
"""

import sys

if sys.version_info < (3, 12):  # noqa: UP036 # runs on the older Pythons it rejects
    sys.exit("simplex-chat-reproduce-builds.py needs Python 3.12 or newer")

import argparse
import contextlib
import enum
import hashlib
import os
import platform
import plistlib
import shutil
import struct
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

REPO = "https://github.com/simplex-chat/simplex-chat"
GHC = "9.6.3"


class ReproduceError(Exception):
    pass


class Target(enum.StrEnum):
    DESKTOP = "desktop"
    ANDROID = "android"


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
SOURCE_DATE_EPOCH = "1704067200"  # as make-deb-linux.sh and make-appimage-linux.sh set it

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

# SHA-256 of the certificate that signs the released APKs
SIMPLEX_KEY = "3C:52:C4:FD:3C:AD:1C:07:C9:B0:0A:70:80:E3:58:FA:B9:FE:FC:B8:AF:5A:EC:14:77:65:F1:6D:0F:21:AD:85"
ANDROID_LIBS_DIR = "apps/multiplatform/common/src/commonMain/cpp/android/libs"
ANDROID_LIBS = ("libsimplex", "libsupport")
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
        self.source = source
        self.mounts = dict(mounts)
        self.build_args = {"USER_UID": str(os.getuid()), "USER_GID": str(os.getgid()), **(build_args or {})}
        self.run_args = list(run_args)

    def __enter__(self) -> "Container":
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
    tarball = work / f"openssl-{OPENSSL_VERSION}.tar.gz"
    download(OPENSSL_URL, tarball)
    if sha256(tarball) != OPENSSL_SHA256:
        raise ReproduceError(f"{OPENSSL_URL} doesn't match its pinned SHA-256 {OPENSSL_SHA256}")
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

OPENSSL_CONTAINER_SOURCE = f"{OUT}/openssl-{OPENSSL_VERSION}"


def desktop_steps(image: UbuntuImage, version: str) -> list[Step]:
    """After the CLI is built and copied to /out: its deb, the desktop deb and, on APPIMAGE_IMAGE, the AppImage with the
    pinned OpenSSL, whose source fetch_openssl put in /out."""
    steps = [
        Step(("./scripts/desktop/build-cli-deb.sh", version, f"{OUT}/simplex-chat", f"{OUT}/deb-build")),
        Step(("./scripts/desktop/make-deb-linux.sh",), env=DESKTOP_ENV),
    ]
    if image.version == APPIMAGE_IMAGE:
        openssl = openssl_build(f"linux-{image.arch}", OPENSSL_UNIX_PREFIX, OPENSSL_UNIX_DIR, install=False)
        steps += [Step(argv, OPENSSL_CONTAINER_SOURCE, env={"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}) for argv in openssl]
        libcrypto = f"{OPENSSL_CONTAINER_SOURCE}/libcrypto.so.3"
        steps.append(Step(("./scripts/desktop/make-appimage-linux.sh",), env={**DESKTOP_ENV, "LIBCRYPTO": libcrypto}))
    return steps


CLI_BUILD_STEPS = (Step(("cabal", "clean")), Step(("cabal", "update")), Step(("cabal", "build", "-j")))


def desktop_files(image: UbuntuImage) -> list[str]:
    cli = f"simplex-chat-ubuntu-{image.suffix}-{image.arch}"
    files = [cli, f"{cli}.deb", f"simplex-desktop-ubuntu-{image.suffix}-{image.arch}.deb"]
    if image.version == APPIMAGE_IMAGE:
        files.append(f"simplex-desktop-{image.arch}.AppImage")
    return files


def linux_image(version: str, machine: str) -> UbuntuImage:
    """The image of an Ubuntu version for this machine: Docker runs images of the host's architecture."""
    for image in DESKTOP_IMAGES:
        if (image.version, image.arch) == (version, machine):
            return image
    raise ReproduceError(f"no Ubuntu {version} image for {machine}")


def build_desktop_linux(source: Path, image: UbuntuImage, version: str, out: Path) -> None:
    """The Linux release files of an Ubuntu image, with their released names."""
    (source / "cabal.project.local").write_text(DESKTOP_CABAL_LOCAL)
    normalize_permissions(source)
    log(f"building Ubuntu {image.version} CLI and desktop apps")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if image.version == APPIMAGE_IMAGE:
            fetch_openssl(work)
        with Container(
            source,
            {source: PROJECT, work: OUT},
            build_args={"TAG": image.version, "HASH": image.digest, "GHC": GHC},
            run_args=["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN", "--security-opt", "apparmor:unconfined"],
        ) as container:
            container.run_all(CLI_BUILD_STEPS)
            cli = container.run(Step(("cabal", "list-bin", "exe:simplex-chat")), capture=True).strip()
            container.run_all([Step(("cp", cli, f"{OUT}/simplex-chat")), Step(("strip", f"{OUT}/simplex-chat"))])
            container.run_all(desktop_steps(image, version))
        release = source / DESKTOP_RELEASE_DIR
        built = [work / "simplex-chat", work / "deb-build/simplex-chat.deb", release / f"deb/simplex_{image.arch}.deb"]
        if image.version == APPIMAGE_IMAGE:
            built.append(single_file(release, "*imple*.AppImage"))
        for path, name in zip(built, desktop_files(image), strict=True):
            shutil.copy(path, out / name)


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


def build_ios_archive(source: Path, art: Path, out: Path, install: bool) -> None:
    if not (art / "ios/Assets.xcassets").is_dir():
        raise ReproduceError(f"no ios/Assets.xcassets in {art}")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if install:
            install_nix(work)
        run(["nix", "build", "-L", "--out-link", work / "core", IOS_CORE_JOB], cwd=source, env=nix_env())
        downloads = Path.home() / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        shutil.copy(work / "core" / IOS_CORE_ZIP, downloads / IOS_CORE_ZIP)
        run(["scripts/ci/ios-sim/prepare-libs.sh"], cwd=source)
        run(["scripts/ios/update-pbxproj.sh"], cwd=source)

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
class BuildIosArchive:
    art: Path
    out: Path
    source: Path
    install_nix: bool


# An assignment rather than a type statement, so that older Pythons parse the file and print the version check
Command = Reproduce | BuildApk | BuildBundle | BuildDesktopLinux | BuildIosArchive


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


def build(command: BuildApk | BuildBundle | BuildDesktopLinux | BuildIosArchive) -> None:
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
            require_tools(["docker"])
            build_desktop_linux(source, image, version, out)
        case BuildIosArchive(art=art, install_nix=install):
            require_tools(IOS_TOOLS)
            if not install and not (NIX_PROFILE / "bin/nix").exists():
                raise ReproduceError(f"Nix is not installed ({NIX_PROFILE / 'bin/nix'} not found): install it, or pass --install-nix")
            build_ios_archive(source, art.resolve(), out, install)
    write_sha256sums(out)


def execute(command: Command) -> None:
    match command:
        case Reproduce():
            reproduce(command)
        case _:
            build(command)


def parse_build_args(argv: Sequence[str]) -> Command:
    parser = argparse.ArgumentParser(prog="simplex-chat-reproduce-builds.py build", description="Build one commit's release files.")
    kinds = parser.add_subparsers(dest="kind", required=True)
    apk = kinds.add_parser("android-apk", help="the deterministic unsigned APK and its core libraries")
    apk.add_argument("arch", type=Arch, choices=list(Arch))
    apk.add_argument("--nix-cache", action="store_true", help="substitute from zw3rk's and IOG's caches")
    bundle = kinds.add_parser("android-bundle", help="the unsigned Play bundle, from both arches' core libraries")
    bundle.add_argument("--libs", type=Path, required=True, help="directory with the pkg-<arch>-android-*.zip files")
    desktop = kinds.add_parser("desktop-linux", help="the Linux CLI, its deb, the desktop deb and the AppImage")
    desktop.add_argument("image", choices=sorted({image.version for image in DESKTOP_IMAGES}), help="the Ubuntu version")
    desktop.add_argument("--version", required=True, help="the release version of the CLI deb, e.g. 7.1.0")
    ios = kinds.add_parser("ios-archive", help="the iOS archive, its bundles signed ad hoc with their entitlements")
    ios.add_argument("--art", type=Path, required=True, help="checkout of simplex-chat-art, with the brand assets")
    ios.add_argument("--install-nix", action="store_true", help="install Nix first, as on a CI runner")
    for sub in (apk, bundle, desktop, ios):
        sub.add_argument("--out", type=Path, required=True)
        sub.add_argument("--source", type=Path, default=Path("."), help="the simplex-chat checkout (default: current directory)")
    args = parser.parse_args(argv)
    match args.kind:
        case "android-apk":
            return BuildApk(args.arch, args.out, args.source, args.nix_cache)
        case "android-bundle":
            return BuildBundle(args.libs, args.out, args.source)
        case "desktop-linux":
            image = linux_image(args.image, platform.machine())
            return BuildDesktopLinux(image, args.version, args.out, args.source)
        case _:
            return BuildIosArchive(args.art, args.out, args.source, args.install_nix)


def parse_arches(value: str) -> tuple[Arch, ...]:
    try:
        return tuple(Arch(arch) for arch in value.split(","))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{e}, choose from {', '.join(Arch)}") from e


def parse_args(argv: Sequence[str]) -> Command:
    if argv[:1] == ["build"]:
        return parse_build_args(argv[1:])
    parser = argparse.ArgumentParser(description="Reproduce a SimpleX Chat release and compare it with the released files.")
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
