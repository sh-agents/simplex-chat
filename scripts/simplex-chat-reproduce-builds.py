#!/usr/bin/env python3

"""Reproduces SimpleX Chat release builds in Docker and compares them with the released files.

Reproduce a release (desktop is the default target):
    simplex-chat-reproduce-builds.py <tag> [desktop] [android] [--arches aarch64,armv7a] [--nix-cache]

Build one commit's apps unsigned, as CI's build-apps workflow does:
    simplex-chat-reproduce-builds.py build android-apk <aarch64|armv7a> --out <dir> [--source <dir>] [--nix-cache]
    simplex-chat-reproduce-builds.py build android-bundle --libs <dir> --out <dir> [--source <dir>]
    simplex-chat-reproduce-builds.py build ios-archive --art <simplex-chat-art> --out <dir> [--source <dir>] [--install-nix]

Needs Python 3.9+, git and Docker; ios-archive needs macOS with Xcode and Nix. Results go to ./<tag>-simplex-chat.
"""

import argparse
import contextlib
import hashlib
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = "https://github.com/simplex-chat/simplex-chat"
GHC = "9.6.3"
# The Ubuntu images of build.yml's release builds
DESKTOP_OSES = {
    "22.04": "sha256:5c8b2c0a6c745bc177669abfaa716b4bc57d58e2ea3882fb5da67f4d59e3dda5",
    "24.04": "sha256:98ff7968124952e719a8a69bb3cccdd217f5fe758108ac4f21ad22e1df44d237",
}
DESKTOP_CABAL_LOCAL = "ignore-project: False\npackage direct-sqlcipher\n    flags: +openssl"
# SHA-256 of the certificate that signs the released APKs
SIMPLEX_KEY = "3C:52:C4:FD:3C:AD:1C:07:C9:B0:0A:70:80:E3:58:FA:B9:FE:FC:B8:AF:5A:EC:14:77:65:F1:6D:0F:21:AD:85"
ANDROID_ABIS = {"aarch64": "arm64-v8a", "armv7a": "armeabi-v7a"}
ANDROID_LIBS_DIR = "apps/multiplatform/common/src/commonMain/cpp/android/libs"
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

LOCAL_HEADER = b"PK\x03\x04"
CENTRAL_HEADER = b"PK\x01\x02"
END_OF_CENTRAL_DIRECTORY = b"PK\x05\x06"
APKSIGNER_ALIGNMENT_EXTRA = 0xD935
V1_SIGNATURE_SUFFIXES = (".SF", ".RSA", ".DSA", ".EC")


class ReproduceError(Exception):
    pass


def log(message):
    print(f"\033[36m{message}\033[0m", flush=True)


def run(args, cwd=None, check=True, capture=False):
    result = subprocess.run(args, cwd=cwd, check=False, text=True, stdout=subprocess.PIPE if capture else None)
    if check and result.returncode != 0:
        raise ReproduceError(f"failed ({result.returncode}): {' '.join(map(str, args))}")
    return result


# ---------------------------------------------------------------------------------------------------------------------
# APK ZIP details that apksigcopier and repro-apk don't handle


class ZipEntry:
    def __init__(self, name, central_offset, local_offset, flags):
        self.name = name
        self.central_offset = central_offset
        self.local_offset = local_offset
        self.flags = flags


def central_directory(data):
    end = data.rfind(END_OF_CENTRAL_DIRECTORY)
    if end < 0:
        raise ReproduceError("no end of central directory record")
    count, _, offset = struct.unpack("<HII", data[end + 10 : end + 20])
    entries = []
    for _ in range(count):
        if data[offset : offset + 4] != CENTRAL_HEADER:
            raise ReproduceError(f"no central directory header at {offset}")
        flags = struct.unpack("<H", data[offset + 8 : offset + 10])[0]
        name_len, extra_len, comment_len = struct.unpack("<HHH", data[offset + 28 : offset + 34])
        local_offset = struct.unpack("<I", data[offset + 42 : offset + 46])[0]
        name = data[offset + 46 : offset + 46 + name_len].decode("utf-8")
        entries.append(ZipEntry(name, offset, local_offset, flags))
        offset += 46 + name_len + extra_len + comment_len
    return entries


def local_extra_fields(data, entry):
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


def apk_padding(data):
    """apksigner >= 35.0.0-rc1 pads entries with its 0xd935 extra field; AGP and older apksigner with zeros."""
    for entry in central_directory(data):
        if APKSIGNER_ALIGNMENT_EXTRA in local_extra_fields(data, entry):
            return "apksigner"
    return "zero"


def is_v1_signature_file(name):
    if not name.startswith("META-INF/") or "/" in name[len("META-INF/") :]:
        return False
    return name == "META-INF/MANIFEST.MF" or name.endswith(V1_SIGNATURE_SUFFIXES)


def copy_v1_flags(source, target):
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
# Files and checksums


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256sums(directory, target=None):
    files = sorted(p for p in Path(directory).iterdir() if p.is_file() and p.name not in ("SHA256SUMS", "_sha256sums"))
    lines = "".join(f"{sha256(p)}  {p.name}\n" for p in files)
    Path(target or Path(directory) / "SHA256SUMS").write_text(lines)
    print(lines, end="")


def compare_dirs(built, released):
    """Returns the names of the built files that differ from or are missing in the released ones."""
    mismatched = []
    for path in sorted(Path(built).iterdir()):
        other = Path(released) / path.name
        if not other.is_file() or sha256(path) != sha256(other):
            mismatched.append(path.name)
    return mismatched


def download(url, target):
    log(f"downloading {url}")
    try:
        with urllib.request.urlopen(url) as response, open(target, "wb") as f:
            shutil.copyfileobj(response, f)
    except OSError as e:
        raise ReproduceError(f"cannot download {url}: {e}") from e


def tag_version(tag):
    """v7.1.0-beta.7 -> 7.1.0, as the desktop CLI deb is versioned"""
    return tag.lstrip("v").split("-")[0]


def android_build_tag(tag, existing_tags, arch):
    """Releases before armv7a was built from master have it in a separate <tag>-armv7a tag."""
    if arch == "armv7a" and f"{tag}-armv7a" in existing_tags:
        return f"{tag}-armv7a"
    return tag


def android_vercode(source):
    for line in (Path(source) / "apps/multiplatform/gradle.properties").read_text().splitlines():
        if line.startswith("android.version_code="):
            return line.split("=", 1)[1].strip()
    raise ReproduceError("android.version_code not found in apps/multiplatform/gradle.properties")


# ---------------------------------------------------------------------------------------------------------------------
# Docker


class Container:
    """A container of an image built from Dockerfile.build, removed with the image on exit."""

    def __init__(self, source, mounts, build_args=None, extra_run_args=()):
        self.name = f"simplex-reproduce-{os.getpid()}-{id(self)}"
        self.source = Path(source)
        self.mounts = mounts
        self.build_args = {"USER_UID": str(os.getuid()), "USER_GID": str(os.getgid()), **(build_args or {})}
        self.extra_run_args = list(extra_run_args)

    def __enter__(self):
        build = ["docker", "build", "--no-cache", "-f", str(self.source / "Dockerfile.build"), "-t", self.name]
        for key, value in self.build_args.items():
            build += ["--build-arg", f"{key}={value}"]
        run(build + [str(self.source)])
        args = ["docker", "run", "-t", "-d", "--name", self.name] + self.extra_run_args
        for host, container in self.mounts.items():
            args += ["-v", f"{host}:{container}"]
        try:
            run(args + [self.name], capture=True)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        run(["docker", "rm", "--force", self.name], check=False, capture=True)
        run(["docker", "image", "rm", self.name], check=False, capture=True)

    def exec(self, script, env=None, user=None, check=True, capture=False):
        args = ["docker", "exec"]
        if user:
            args += ["-u", user]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        return run(args + [self.name, "sh", "-eu", "-c", script], check=check, capture=capture)


# ---------------------------------------------------------------------------------------------------------------------
# Android


@contextlib.contextmanager
def android_container(source, out, set_pid_max):
    # FUSE and SYS_ADMIN are what Dockerfile.build's builds need; systempaths=unconfined makes /proc/sys writable
    with Container(
        source,
        {str(Path(source).resolve()): "/project", str(Path(out).resolve()): "/out"},
        extra_run_args=[
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


def retry_once(action):
    # Gradle's downloads fail now and then
    try:
        action()
    except ReproduceError as e:
        log(f"{e}; running again")
        action()


def build_apk(container, source, arch, out_name, nix_cache, keep_libs):
    """The deterministic unsigned APK of scripts/android/build-android.sh, to /out/<out_name>."""
    env = {"ARCH": arch, "ABI": ANDROID_ABIS[arch], "VERCODE": android_vercode(source), "OUT": out_name}
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


def build_bundle(container, source, libs):
    # Gradle reads compression.level only from local.properties; a non-zero level extracts the native libraries at
    # install, as the APKs do.
    properties = "".join(f"{key}={value}\n" for key, value in ANDROID_LOCAL_PROPERTIES.items())
    (Path(source) / "apps/multiplatform/local.properties").write_text(properties)
    for arch, abi in ANDROID_ABIS.items():
        target = Path(source) / ANDROID_LIBS_DIR / abi
        target.mkdir(parents=True, exist_ok=True)
        for lib in ("libsimplex", "libsupport"):
            name = f"pkg-{arch}-android-{lib}.zip"
            found = sorted(Path(libs).rglob(name))
            if not found:
                raise ReproduceError(f"{name} not found in {libs}")
            run(["unzip", "-o", str(found[0]), "-d", str(target)])
    retry_once(
        lambda: container.exec(
            "cd apps/multiplatform\n"
            "./gradlew --no-daemon :android:bundleGoogleRelease\n"
            "cp android/build/outputs/bundle/googleRelease/*.aab /out/simplex.aab\n"
        )
    )


def release_signer(container, apk_name):
    output = container.exec(f'apksigner verify --print-certs "/out/{apk_name}"', capture=True).stdout
    digest = next(line.split()[-1] for line in output.splitlines() if "SHA-256" in line)
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()


def verify_apk(container, out, released, built):
    """Copies the released APK's signature onto the built one, padded as the release is, and compares them."""
    released_data = (Path(out) / released).read_bytes()
    pad = "--pad-like-apksigner" if apk_padding(released_data) == "apksigner" else ""
    container.exec(
        f'repro-apk zipalign --page-size 16 {pad} --replace "/out/{built}" "/out/{built}.aligned"\n'
        f'apksigcopier copy "/out/{released}" "/out/{built}.aligned" "/out/{built}.signed"\n'
    )
    signed = Path(out) / f"{built}.signed"
    signed.write_bytes(copy_v1_flags(released_data, signed.read_bytes()))
    return signed.read_bytes() == released_data


def reproduce_android(tag, arches, nix_cache, workdir, results):
    out = results / "android"
    out.mkdir(parents=True, exist_ok=True)
    source = workdir / "simplex-chat"
    tags = run(["git", "-C", str(source), "tag", "--list"], capture=True).stdout.split()
    for arch in arches:
        download(f"{REPO}/releases/download/{tag}/simplex-{arch}.apk", out / f"simplex-{arch}.apk")
    failed = []
    with android_container(source, out, "armv7a" in arches) as container:
        for arch in arches:
            released = f"simplex-{arch}.apk"
            signer = release_signer(container, released)
            if signer != SIMPLEX_KEY:
                raise ReproduceError(f"{released} is signed by {signer}, not by the SimpleX key")
            log(f"{released} is signed by the SimpleX key")
            checkout(source, android_build_tag(tag, tags, arch))
            log(f"building {arch}")
            build_apk(container, source, arch, f"simplex-{arch}.apk.built", nix_cache, keep_libs=False)
            if verify_apk(container, out, released, f"simplex-{arch}.apk.built"):
                log(f"{released} is reproduced")
            else:
                log(f"{released} is NOT reproduced")
                failed.append(released)
    return failed


# ---------------------------------------------------------------------------------------------------------------------
# iOS


def version_tuple(version):
    return tuple(int(part) for part in version.split("."))


def minimum_versions(otool_output):
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


def build_settings(output, names):
    settings = {}
    for line in output.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key in names and key not in settings:
            settings[key] = value
    missing = [name for name in names if not settings.get(name)]
    if missing:
        raise ReproduceError(f"build settings not found: {', '.join(missing)}")
    return settings


def resolve_entitlements(text, team):
    # $(AppIdentifierPrefix) is the team id: simplex.chat's apple-app-site-association lists 5NN7GUYB6T.chat.simplex.app
    return text.replace("$(AppIdentifierPrefix)", f"{team}.")


def nix_shell(script, cwd):
    return run(["sh", "-c", f". {NIX_PROFILE} && {script}"], cwd=cwd)


def install_nix(work):
    installer = work / "nix-install"
    download(NIX_INSTALLER, installer)
    if sha256(installer) != NIX_INSTALLER_SHA256:
        raise ReproduceError("the Nix installer's sha256 doesn't match")
    conf = work / "nix.conf"
    conf.write_text("experimental-features = nix-command flakes\nmax-jobs = auto\n" + NIX_CACHE_CONFIG + "\n")
    run(["sh", str(installer), "--daemon", "--yes", "--no-channel-add", "--nix-extra-conf-file", str(conf)])


def check_minimum_versions(source, target):
    # mac2ios relabels macOS objects as iOS and keeps their minimum version, which the deployment target must cover
    for lib in sorted((source / "apps/ios/Libraries/ios").glob("*.a")):
        versions = minimum_versions(run(["otool", "-l", str(lib)], capture=True).stdout)
        newest = max(versions, key=version_tuple, default=None)
        print(f"{lib.name}: newest minimum version {newest or 'none'}")
        if newest and version_tuple(newest) > version_tuple(target):
            raise ReproduceError(f"{lib.name} has objects for iOS {newest}, newer than the deployment target {target}")


def sign_bundle(bundle, entitlements, team, work):
    """Ad-hoc signs a bundle with its entitlements: the export keeps only the entitlements a signature carries."""
    if not entitlements.is_file():
        raise ReproduceError(f"no entitlements for {bundle.name}: {entitlements}")
    resolved = work / f"{bundle.name}.entitlements"
    resolved.write_text(resolve_entitlements(entitlements.read_text(), team))
    frameworks = bundle / "Frameworks"
    if frameworks.is_dir():
        for code in sorted(frameworks.iterdir()):
            if code.suffix in (".framework", ".dylib"):
                run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(code)])
    run(["codesign", "--force", "--sign", "-", "--timestamp=none", "--entitlements", str(resolved), str(bundle)])
    signed = run(["codesign", "-d", "--entitlements", "-", "--xml", str(bundle)], capture=True).stdout
    if plistlib.loads(signed.encode()) != plistlib.loads(resolved.read_bytes()):
        raise ReproduceError(f"the signature of {bundle.name} carries other entitlements than {entitlements}")
    print(f"== {bundle.name}\n{resolved.read_text()}")


def build_ios_archive(source, art, out, install):
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
        run(["scripts/ios/copy-assets.sh", str(art)], cwd=source)
        if not list((source / "apps/ios/Shared/SimpleXAssets.xcassets").glob("*.imageset")):
            raise ReproduceError(f"copy-assets.sh copied no imagesets from {art}")

        xcode = ["-project", IOS_PROJECT, "-scheme", IOS_SCHEME, "-configuration", "Release"]
        settings_output = run(["xcodebuild", "-showBuildSettings"] + xcode, cwd=source, capture=True).stdout
        settings = build_settings(settings_output, ["DEVELOPMENT_TEAM", "IPHONEOS_DEPLOYMENT_TARGET"])
        team = settings["DEVELOPMENT_TEAM"]
        check_minimum_versions(source, settings["IPHONEOS_DEPLOYMENT_TARGET"])

        # Xcode refuses ad-hoc signing for iOS devices ("Ad Hoc code signing is not allowed with SDK"), so the archive
        # is built unsigned and its bundles are signed after.
        archive = work / "SimpleX.xcarchive"
        run(
            ["xcodebuild", "archive", "-quiet"] + xcode + [
                "-destination", "generic/platform=iOS",
                "-archivePath", str(archive),
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
        run(["codesign", "--verify", "--deep", "--strict", str(app)])
        # the team Xcode records when it signs an archive, which Organizer shows and distributes for
        run(["plutil", "-replace", "ApplicationProperties.Team", "-string", team, str(archive / "Info.plist")])
        run(["ditto", "-c", "-k", "--keepParent", str(archive), str(out / "SimpleX.xcarchive.zip")])


# ---------------------------------------------------------------------------------------------------------------------
# Desktop and CLI


def reproduce_desktop(tag, workdir, results):
    source = workdir / "simplex-chat"
    built = results / "from-source"
    released = results / "prebuilt"
    built.mkdir(parents=True, exist_ok=True)
    released.mkdir(parents=True, exist_ok=True)
    version = tag_version(tag)
    for os_version, image_hash in DESKTOP_OSES.items():
        suffix = os_version.replace(".", "_")
        cli = f"simplex-chat-ubuntu-{suffix}-x86_64"
        files = {
            cli: "/out/simplex-chat",
            f"{cli}.deb": "/out/deb-build/simplex-chat.deb",
            f"simplex-desktop-ubuntu-{suffix}-x86_64.deb": "/project/apps/multiplatform/release/main/deb/simplex_x86_64.deb",
        }
        if os_version == "22.04":
            files["simplex-desktop-x86_64.AppImage"] = "/project/apps/multiplatform/release/main/simplex.appimage"
        checkout(source, tag)
        (source / "cabal.project.local").write_text(DESKTOP_CABAL_LOCAL)
        log(f"building Ubuntu {os_version} CLI and desktop apps")
        with Container(
            source,
            {str(source): "/project"},
            build_args={"TAG": os_version, "HASH": image_hash, "GHC": GHC},
            extra_run_args=["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN", "--security-opt", "apparmor:unconfined"],
        ) as container:
            container.exec(
                "find /project -type d -exec chmod 755 {} +\n"
                "find /project -type f -perm /111 -exec chmod 755 {} +\n"
                "find /project -type f ! -perm /111 -exec chmod 644 {} +\n"
                "cabal clean && cabal update && cabal build -j\n"
                "mkdir -p /out\n"
                'bin=$(find /project/dist-newstyle -name simplex-chat -type f -executable)\n'
                'mv "$bin" /out/simplex-chat && strip /out/simplex-chat\n'
                f'./scripts/desktop/build-cli-deb.sh "{version}"\n'
                "export ASSETS_DIR=../../assets\n"
                "./scripts/desktop/make-deb-linux.sh\n"
            )
            if os_version == "22.04":
                container.exec(
                    "export ASSETS_DIR=../../assets\n"
                    "./scripts/desktop/make-appimage-linux.sh\n"
                    "mv ./apps/multiplatform/release/main/*imple*.AppImage ./apps/multiplatform/release/main/simplex.appimage\n"
                )
            for name, path in files.items():
                run(["docker", "cp", f"{container.name}:{path}", str(built / name)])
                download(f"{REPO}/releases/download/{tag}/{name}", released / name)
            container.exec("rm -rf ./dist-newstyle ./apps/multiplatform")
    mismatched = compare_dirs(built, released)
    for name in mismatched:
        log(f"{name}: sha256 doesn't match")
    if not mismatched:
        write_sha256sums(built, results / "_sha256sums")
        log(f"checksums: {results / '_sha256sums'}")
    return mismatched


# ---------------------------------------------------------------------------------------------------------------------
# Commands


def clone(workdir):
    run(["git", "-C", str(workdir), "clone", f"{REPO}.git", "simplex-chat"])


def checkout(source, tag):
    run(["git", "-C", str(source), "reset", "--hard", "--quiet"])
    run(["git", "-C", str(source), "clean", "-dfxq"])
    run(["git", "-C", str(source), "checkout", "--quiet", tag])
    run(["git", "-C", str(source), "submodule", "update", "--init", "--recursive", "--quiet"])


def reproduce(args):
    targets = args.targets or ["desktop"]
    results = Path.cwd() / f"{args.tag}-simplex-chat"
    failed = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        clone(workdir)
        if "desktop" in targets:
            failed += reproduce_desktop(args.tag, workdir, results)
        if "android" in targets:
            failed += reproduce_android(args.tag, args.arches, args.nix_cache, workdir, results)
    if failed:
        log(f"{args.tag} is not reproduced: {', '.join(failed)}")
        return 1
    log(f"{args.tag} is reproduced: {', '.join(targets)}")
    return 0


def build(args):
    source = Path(args.source).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.kind == "ios-archive":
        build_ios_archive(source, Path(args.art).resolve(), out, args.install_nix)
        write_sha256sums(out)
        return 0
    with android_container(source, out, args.kind == "android-apk" and args.arch == "armv7a") as container:
        if args.kind == "android-apk":
            build_apk(container, source, args.arch, f"simplex-{args.arch}.apk", args.nix_cache, keep_libs=True)
        else:
            build_bundle(container, source, Path(args.libs).resolve())
    write_sha256sums(out)
    return 0


def parse_args(argv):
    if argv[:1] == ["build"]:
        parser = argparse.ArgumentParser(prog="simplex-chat-reproduce-builds.py build", description="Build one commit's apps unsigned.")
        kinds = parser.add_subparsers(dest="kind", required=True)
        apk = kinds.add_parser("android-apk", help="the deterministic unsigned APK and its core libraries")
        apk.add_argument("arch", choices=sorted(ANDROID_ABIS))
        apk.add_argument("--nix-cache", action="store_true", help="substitute from zw3rk's and IOG's caches")
        bundle = kinds.add_parser("android-bundle", help="the unsigned Play bundle, from both arches' core libraries")
        bundle.add_argument("--libs", required=True, help="directory with the pkg-<arch>-android-*.zip files")
        ios = kinds.add_parser("ios-archive", help="the iOS archive, its bundles signed ad hoc with their entitlements")
        ios.add_argument("--art", required=True, help="checkout of simplex-chat-art, with the brand assets")
        ios.add_argument("--install-nix", action="store_true", help="install Nix first, as on a CI runner")
        for sub in (apk, bundle, ios):
            sub.add_argument("--out", required=True)
            sub.add_argument("--source", default=".", help="the simplex-chat checkout (default: current directory)")
        args = parser.parse_args(argv[1:])
        args.command = build
        return args
    parser = argparse.ArgumentParser(description="Reproduce a SimpleX Chat release and compare it with the released files.")
    parser.add_argument("tag", help="release tag, e.g. v7.1.0")
    parser.add_argument("targets", nargs="*", choices=["desktop", "android"], help="default: desktop")
    parser.add_argument("--arches", default="aarch64,armv7a", type=lambda s: s.split(","), help="Android arches")
    parser.add_argument("--nix-cache", action="store_true", help="substitute the Android toolchains from zw3rk's and IOG's caches")
    args = parser.parse_args(argv)
    for arch in args.arches:
        if arch not in ANDROID_ABIS:
            parser.error(f"unknown arch: {arch}")
    args.command = reproduce
    return args


def main(argv):
    args = parse_args(argv)
    os.environ["DOCKER_BUILDKIT"] = "1"
    try:
        return args.command(args)
    except ReproduceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
