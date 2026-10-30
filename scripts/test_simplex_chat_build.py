#!/usr/bin/env python3

import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import platform
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

spec = importlib.util.spec_from_file_location("build_script", pathlib.Path(__file__).with_name("simplex-chat-build.py"))
build_script = importlib.util.module_from_spec(spec)
# dataclasses look the module up by name
sys.modules[spec.name] = build_script
spec.loader.exec_module(build_script)

Arch, Step, Target = build_script.Arch, build_script.Step, build_script.Target
JAMMY, NOBLE, JAMMY_ARM, NOBLE_ARM = build_script.DESKTOP_IMAGES
UTF8_FLAG = 0x800
GITHUB = {"GITHUB_ACTIONS": "true"}
APKSIGNER_PADDING = struct.pack("<HHH", 0xD935, 2, 4)


def make_zip(entries):
    """entries: (name, data, flags, extra) tuples"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data, _, extra in entries:
            info = zipfile.ZipInfo(name, date_time=(2025, 12, 1, 0, 0, 0))
            info.extra = extra
            z.writestr(info, data)
    # zipfile resets flag_bits when it writes an entry, so the flags are set in the written headers
    data = bytearray(buffer.getvalue())
    flags = {name: f for name, _, f, _ in entries}
    for entry in build_script.central_directory(bytes(data)):
        packed = struct.pack("<H", flags[entry.name])
        data[entry.central_offset + 8 : entry.central_offset + 10] = packed
        data[entry.local_offset + 6 : entry.local_offset + 8] = packed
    return bytes(data)


def local_flags(data, name):
    entry = next(e for e in build_script.central_directory(data) if e.name == name)
    return struct.unpack("<H", data[entry.local_offset + 6 : entry.local_offset + 8])[0]


def central_flags(data, name):
    return next(e.flags for e in build_script.central_directory(data) if e.name == name)


SIGNED = make_zip(
    [
        ("classes.dex", b"dex", 0, b""),
        ("META-INF/KEY0.SF", b"sf", UTF8_FLAG, b""),
        ("META-INF/KEY0.RSA", b"rsa", UTF8_FLAG, b""),
        ("META-INF/MANIFEST.MF", b"mf", UTF8_FLAG, b""),
        ("META-INF/versions/9/OSGI-INF/MANIFEST.MF", b"osgi", 0, b""),
    ]
)
COPIED = make_zip(
    [
        ("classes.dex", b"dex", 0, b""),
        ("META-INF/KEY0.SF", b"sf", 0, b""),
        ("META-INF/KEY0.RSA", b"rsa", 0, b""),
        ("META-INF/MANIFEST.MF", b"mf", 0, b""),
        ("META-INF/versions/9/OSGI-INF/MANIFEST.MF", b"osgi", 0, b""),
    ]
)


class Recorder:
    """Stands in for run(): records commands, answers captured ones, fails the ones matching fail_once the first time."""

    def __init__(self, fail_once=(), answers=None):
        self.commands = []
        self.inputs = []
        self.envs = []
        self.fail_once = list(fail_once)
        self.answers = answers or {}

    def __call__(self, args, cwd=None, env=None, input=None, check=True, capture=False, title=None):
        args = [str(a) for a in args]
        self.commands.append(args)
        self.inputs.append(input)
        self.envs.append(env)
        joined = " ".join(args)
        for pattern in self.fail_once:
            if pattern in joined:
                self.fail_once.remove(pattern)
                if check:
                    raise build_script.ReproduceError(f"failed: {joined}")
        stdout = next((answer for pattern, answer in self.answers.items() if pattern in joined), "")
        return mock.Mock(returncode=0, stdout=stdout)

    def steps(self):
        """argv of the docker exec commands, without docker's own options"""
        result = []
        for command in self.commands:
            if command[:2] == ["docker", "exec"]:
                i = 2
                while command[i].startswith("-"):
                    i += 1 if command[i] == "-i" else 2
                result.append(command[i + 1 :])
        return result


class ApkZipTest(unittest.TestCase):
    def test_apksigner_padding_is_detected_on_any_entry(self):
        apk = make_zip([("classes.dex", b"dex", 0, b""), ("res/a.png", b"png", 0, APKSIGNER_PADDING)])
        self.assertEqual(build_script.apk_padding(apk), "apksigner")

    def test_zip_without_the_extra_field_has_zero_padding(self):
        apk = make_zip([("classes.dex", b"dex", 0, b""), ("res/a.png", b"png", 0, b"\x00\x00")])
        self.assertEqual(build_script.apk_padding(apk), "zero")

    def test_non_zip_is_rejected(self):
        with self.assertRaises(build_script.ReproduceError):
            build_script.apk_padding(b"not a zip")

    def test_v1_signature_files_get_the_flags_in_both_headers(self):
        patched = build_script.copy_v1_flags(SIGNED, COPIED)
        for name in ("META-INF/KEY0.SF", "META-INF/KEY0.RSA", "META-INF/MANIFEST.MF"):
            self.assertEqual(local_flags(patched, name), UTF8_FLAG, name)
            self.assertEqual(central_flags(patched, name), UTF8_FLAG, name)

    def test_other_entries_keep_their_flags(self):
        patched = build_script.copy_v1_flags(SIGNED, COPIED)
        for name in ("classes.dex", "META-INF/versions/9/OSGI-INF/MANIFEST.MF"):
            self.assertEqual(local_flags(patched, name), 0, name)
            self.assertEqual(central_flags(patched, name), 0, name)

    def test_patched_zip_matches_the_signed_one_byte_for_byte(self):
        self.assertNotEqual(COPIED, SIGNED)
        self.assertEqual(build_script.copy_v1_flags(SIGNED, COPIED), SIGNED)

    def test_patched_zip_still_reads(self):
        with zipfile.ZipFile(io.BytesIO(build_script.copy_v1_flags(SIGNED, COPIED))) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.read("META-INF/KEY0.SF"), b"sf")

    def test_v1_signature_file_names(self):
        self.assertTrue(build_script.is_v1_signature_file("META-INF/MANIFEST.MF"))
        self.assertTrue(build_script.is_v1_signature_file("META-INF/KEY0.SF"))
        self.assertTrue(build_script.is_v1_signature_file("META-INF/CERT.EC"))
        self.assertFalse(build_script.is_v1_signature_file("META-INF/versions/9/OSGI-INF/MANIFEST.MF"))
        self.assertFalse(build_script.is_v1_signature_file("META-INF/androidx.core_core.version"))
        self.assertFalse(build_script.is_v1_signature_file("assets/KEY0.SF"))


class ReleaseDetailsTest(unittest.TestCase):
    def test_tag_version(self):
        self.assertEqual(build_script.tag_version("v7.1.0-beta.7"), "7.1.0")
        self.assertEqual(build_script.tag_version("v6.4.8"), "6.4.8")

    def test_android_build_tag(self):
        tags = ["v6.4.0", "v6.4.0-armv7a", "v7.1.0"]
        self.assertEqual(build_script.android_build_tag("v6.4.0", tags, Arch.ARMV7A), "v6.4.0-armv7a")
        self.assertEqual(build_script.android_build_tag("v6.4.0", tags, Arch.AARCH64), "v6.4.0")
        self.assertEqual(build_script.android_build_tag("v7.1.0", tags, Arch.ARMV7A), "v7.1.0")

    def test_arches(self):
        self.assertEqual([a.abi for a in Arch], ["arm64-v8a", "armeabi-v7a"])
        self.assertEqual(f"simplex-{Arch.ARMV7A}.apk", "simplex-armv7a.apk")

    def test_android_vercode(self):
        with tempfile.TemporaryDirectory() as tmp:
            props = pathlib.Path(tmp) / "apps/multiplatform/gradle.properties"
            props.parent.mkdir(parents=True)
            props.write_text("android.version_name=7.1-beta.7\nandroid.version_code=387\n")
            self.assertEqual(build_script.android_vercode(pathlib.Path(tmp)), "387")
            props.write_text("android.version_name=7.1\n")
            with self.assertRaises(build_script.ReproduceError):
                build_script.android_vercode(pathlib.Path(tmp))


class FilesTest(unittest.TestCase):
    def test_sha256sums_list_every_file_but_the_sums(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "b.apk").write_bytes(b"b")
            (d / "a.zip").write_bytes(b"a")
            (d / "SHA256SUMS").write_text("old")
            with mock.patch("builtins.print"):
                build_script.write_sha256sums(d)
            self.assertEqual(
                (d / "SHA256SUMS").read_text(),
                f"{build_script.sha256(d / 'a.zip')}  a.zip\n{build_script.sha256(d / 'b.apk')}  b.apk\n",
            )

    def test_compare_dirs_reports_differing_and_missing_files(self):
        with tempfile.TemporaryDirectory() as built, tempfile.TemporaryDirectory() as released:
            for name, data in (("same", b"1"), ("differs", b"2"), ("missing", b"3")):
                (pathlib.Path(built) / name).write_bytes(data)
            (pathlib.Path(released) / "same").write_bytes(b"1")
            (pathlib.Path(released) / "differs").write_bytes(b"x")
            self.assertEqual(build_script.compare_dirs(pathlib.Path(built), pathlib.Path(released)), ["differs", "missing"])

    def test_permissions_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "dir").mkdir(mode=0o700)
            (root / "dir/script.sh").write_text("")
            (root / "dir/script.sh").chmod(0o700)
            (root / "dir/data").write_text("")
            (root / "dir/data").chmod(0o600)
            (root / "link").symlink_to("dir/data")
            build_script.normalize_permissions(root)
            modes = [(root / p).stat().st_mode & 0o777 for p in ("dir", "dir/script.sh", "dir/data")]
            self.assertEqual(modes, [0o755, 0o755, 0o644])

    def test_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            with self.assertRaises(build_script.ReproduceError):
                build_script.single_file(d, "*.aab")
            (d / "a.aab").write_bytes(b"")
            self.assertEqual(build_script.single_file(d, "*.aab"), d / "a.aab")
            (d / "b.aab").write_bytes(b"")
            with self.assertRaises(build_script.ReproduceError):
                build_script.single_file(d, "*.aab")


class ParseArgsTest(unittest.TestCase):
    def test_reproduce_defaults_to_desktop_and_both_arches(self):
        self.assertEqual(
            build_script.parse_args(["reproduce", "v7.1.0"]),
            build_script.Reproduce("v7.1.0", (Target.DESKTOP,), (Arch.AARCH64, Arch.ARMV7A), False),
        )

    def test_reproduce_android_with_options(self):
        self.assertEqual(
            build_script.parse_args(["reproduce", "v7.1.0", "android", "--arches", "armv7a", "--nix-cache"]),
            build_script.Reproduce("v7.1.0", (Target.ANDROID,), (Arch.ARMV7A,), True),
        )

    def test_reproduce_rejects_unknown_arch_and_target(self):
        for argv in (["reproduce", "v7.1.0", "android", "--arches", "x86"], ["reproduce", "v7.1.0", "ios"], ["v7.1.0"], []):
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                build_script.parse_args(argv)

    def test_build_commands(self):
        out, here = pathlib.Path("o"), pathlib.Path(".")
        self.assertEqual(
            build_script.parse_args(["build", "apk", "armv7a", "--out", "o", "--nix-cache"]),
            build_script.BuildApk(Arch.ARMV7A, out, here, True),
        )
        self.assertEqual(
            build_script.parse_args(["build", "aab", "--libs", "l", "--out", "o", "--source", "s"]),
            build_script.BuildBundle(pathlib.Path("l"), out, pathlib.Path("s")),
        )
        self.assertEqual(
            build_script.parse_args(["build", "desktop", "linux", "24.04", "--version", "7.1.0", "--out", "o"]),
            build_script.BuildDesktopLinux(build_script.linux_image("24.04", platform.machine()), "7.1.0", out, here),
        )
        self.assertEqual(
            build_script.parse_args(["build", "libs", "linux", "--database", "postgres", "--host", "--out", "o"]),
            build_script.BuildLinuxLibs(
                build_script.linux_image("22.04", platform.machine()),
                build_script.Database.POSTGRES,
                "dist-newstyle",
                build_script.LibsBuilder(on_host=True),
                out,
                here,
            ),
        )
        self.assertEqual(
            build_script.parse_args(["build", "libs", "linux", "--container", "builder", "--dist-dir", "dist-lib", "--out", "o"]).builder,
            build_script.LibsBuilder(container="builder"),
        )
        self.assertEqual(
            build_script.parse_args(["build", "desktop", "macos", "--require-signing", "--out", "o"]),
            build_script.BuildDesktopMacos(out, here, True),
        )
        self.assertEqual(
            build_script.parse_args(["build", "desktop", "windows", "--out", "o"]), build_script.BuildDesktopWindows(out, here)
        )
        self.assertEqual(
            build_script.parse_args(["build", "libs", "macos", "--require-signing", "--out", "o"]),
            build_script.BuildMacosLibs(build_script.Database.SQLITE, out, here, True),
        )
        self.assertEqual(build_script.parse_args(["build", "libs", "windows", "--out", "o"]), build_script.BuildWindowsLibs(out, here))
        self.assertEqual(
            build_script.parse_args(["build", "openssl", "windows", "--out", "o"]),
            build_script.BuildOpenssl(build_script.Platform.WINDOWS, out, here),
        )
        self.assertEqual(
            build_script.parse_args(["build", "xcarchive", "--art", "a", "--out", "o", "--install-nix"]),
            build_script.BuildIosArchive(pathlib.Path("a"), out, here, True),
        )

    def test_build_commands_reject_missing_or_unknown_arguments(self):
        for argv in (
            ["build", "aab", "--out", "o"],
            ["build", "desktop", "linux", "20.04", "--version", "7.1.0", "--out", "o"],
            ["build", "desktop", "linux", "22.04", "--out", "o"],
            ["build", "desktop", "--out", "o"],
            ["build", "openssl", "linux", "--out", "o"],
            ["build", "libs", "macos", "--host", "--out", "o"],
            ["build", "apk", "x86", "--out", "o"],
        ):
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                build_script.parse_args(argv)


class PlatformTest(unittest.TestCase):
    def test_host_platform(self):
        for os_name, sys_platform, expected in (("nt", "win32", "windows"), ("posix", "darwin", "macos"), ("posix", "linux", "linux")):
            with mock.patch.object(build_script.os, "name", os_name), mock.patch.object(build_script.sys, "platform", sys_platform):
                self.assertEqual(build_script.host_platform(), expected)

    def test_builds_for_another_platform_are_refused(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(build_script, "host_platform", return_value=build_script.Platform.LINUX),
        ):
            out, here = pathlib.Path(tmp), pathlib.Path(".")
            for command in (
                build_script.BuildDesktopMacos(out, here, False),
                build_script.BuildOpenssl(build_script.Platform.WINDOWS, out, here),
                build_script.BuildIosArchive(pathlib.Path("a"), out, here, False),
            ):
                with self.assertRaises(build_script.ReproduceError) as raised:
                    build_script.build(command)
                self.assertIn("not on linux", str(raised.exception))


class ContainerTest(unittest.TestCase):
    def test_lifecycle_and_steps(self):
        recorder = Recorder(answers={"printenv": "/home/builder\n"})
        src = pathlib.Path("/src")
        with (
            mock.patch.object(build_script, "run", recorder),
            build_script.Container(src, {src: "/project"}, {"TAG": "22.04"}, ["--cap-add", "SYS_ADMIN"]) as c,
        ):
            home = c.run(Step(("printenv", "HOME")), capture=True)
            c.run(Step(("tee", "/f"), workdir="/w", env={"A": "1"}, user="root", input="x"))
        build, start, printenv, tee, rm, rmi = recorder.commands
        self.assertEqual(build[:4], ["docker", "build", "--no-cache", "-f"])
        self.assertIn("TAG=22.04", build)
        self.assertEqual(build[-1], "/src")
        self.assertEqual(start[:6], ["docker", "run", "-t", "-d", "--name", c.name])
        self.assertIn("/src:/project", start)
        self.assertEqual(printenv, ["docker", "exec", "-w", "/project", c.name, "printenv", "HOME"])
        self.assertEqual(home, "/home/builder\n")
        self.assertEqual(tee, ["docker", "exec", "-w", "/w", "-i", "-u", "root", "-e", "A=1", c.name, "tee", "/f"])
        self.assertEqual(recorder.inputs[3], "x")
        self.assertEqual((rm[:3], rmi[:3]), (["docker", "rm", "--force"], ["docker", "image", "rm"]))

    def test_image_is_removed_when_the_container_fails_to_start(self):
        recorder = Recorder(fail_once=["docker run"])
        with (
            mock.patch.object(build_script, "run", recorder),
            self.assertRaises(build_script.ReproduceError),
            build_script.Container(pathlib.Path("/src"), {}),
        ):
            pass
        self.assertEqual(recorder.commands[-1][:3], ["docker", "image", "rm"])

    def test_copy_out_follows_symlinks(self):
        recorder = Recorder()
        with mock.patch.object(build_script, "run", recorder):
            build_script.Container(pathlib.Path("/src"), {}).copy_out("/project/result/x.zip", pathlib.Path("/out"))
        self.assertEqual(recorder.commands[0][:3], ["docker", "cp", "-L"])


class AndroidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.source, self.out = self.root / "src", self.root / "out"
        props = self.source / "apps/multiplatform/gradle.properties"
        props.parent.mkdir(parents=True)
        props.write_text("android.version_code=387\n")
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def container(self):
        return build_script.Container(self.source, {})

    def test_armv7a_container_sets_pid_max_in_its_namespace(self):
        recorder = Recorder()
        with mock.patch.object(build_script, "run", recorder), build_script.android_container(self.source, self.out, set_pid_max=True):
            pass
        start = next(c for c in recorder.commands if c[1] == "run")
        self.assertIn("systempaths=unconfined", start)
        tee = next(c for c in recorder.commands if "tee" in c)
        self.assertIn("root", tee)
        self.assertIn("65535\n", recorder.inputs)

    def test_apk_build_retries_once_and_keeps_the_libraries(self):
        (self.source / "simplex-chat-armeabi-v7a.apk").write_bytes(b"apk")
        recorder = Recorder(
            fail_once=["build-android.sh"],
            answers={"HOME": "/home/builder\n", "PATH": "/usr/bin\n", "find": "/home/builder/.gradle/wrapper/dists/g/bin/gradle\n"},
        )
        with mock.patch.object(build_script, "run", recorder), mock.patch.object(build_script, "warn") as warn:
            build_script.build_apk(self.container(), self.source, Arch.ARMV7A, self.out, "simplex-armv7a.apk", True, True)
        self.assertIn("running it again", warn.call_args.args[0])
        builds = [c for c in recorder.commands if "./scripts/android/build-android.sh" in c]
        self.assertEqual(len(builds), 2)
        self.assertEqual(builds[0][-2:], ["-gs", "387"])
        self.assertIn("ARCHES=armv7a", builds[0])
        self.assertIn("PATH=/home/builder/.gradle/wrapper/dists/g/bin:/usr/bin", builds[0])
        self.assertTrue(any(a.startswith("NIX_CONFIG=extra-substituters") for a in builds[0]))
        self.assertEqual((self.out / "simplex-armv7a.apk").read_bytes(), b"apk")
        copies = [c[-2] for c in recorder.commands if c[:2] == ["docker", "cp"]]
        self.assertTrue(copies[0].endswith(":/project/result-armv7a-libsimplex/pkg-armv7a-android-libsimplex.zip"))
        self.assertEqual(len(copies), 2)

    def test_apk_build_without_cache_or_libraries(self):
        (self.source / "simplex-chat-arm64-v8a.apk").write_bytes(b"apk")
        recorder = Recorder(answers={"find": "/g/bin/gradle\n"})
        with mock.patch.object(build_script, "run", recorder):
            build_script.build_apk(self.container(), self.source, Arch.AARCH64, self.out, "x.apk", False, False)
        build = next(c for c in recorder.commands if "./scripts/android/build-android.sh" in c)
        self.assertFalse(any(a.startswith("NIX_CONFIG") for a in build))
        self.assertFalse(any(c[:2] == ["docker", "cp"] for c in recorder.commands))

    def test_missing_gradle_distribution(self):
        with mock.patch.object(build_script, "run", Recorder()), self.assertRaises(build_script.ReproduceError):
            build_script.build_apk(self.container(), self.source, Arch.AARCH64, self.out, "x.apk", False, False)

    def test_bundle_extracts_both_arches_with_release_properties(self):
        libs = self.root / "libs"
        for arch in Arch:
            directory = libs / f"android-{arch}-unsigned"
            directory.mkdir(parents=True)
            for lib in build_script.ANDROID_LIBS:
                with zipfile.ZipFile(directory / f"pkg-{arch}-android-{lib}.zip", "w") as z:
                    z.writestr(f"{lib}.so", f"{arch} {lib}")
        bundle_dir = self.source / build_script.ANDROID_BUNDLE_DIR
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "android-google-release.aab").write_bytes(b"aab")
        recorder = Recorder()
        with mock.patch.object(build_script, "run", recorder):
            build_script.build_bundle(self.container(), self.source, libs, self.out)
        extracted = self.source / build_script.ANDROID_LIBS_DIR / "armeabi-v7a/libsupport.so"
        self.assertEqual(extracted.read_text(), "armv7a libsupport")
        self.assertEqual(recorder.steps(), [["./gradlew", "--no-daemon", ":android:bundleGoogleRelease"]])
        self.assertEqual(
            (self.source / "apps/multiplatform/local.properties").read_text(),
            "compression.level=5\nsimplex.assets.dir=../../assets\n",
        )
        self.assertEqual((self.out / "simplex.aab").read_bytes(), b"aab")

    def test_bundle_fails_without_a_library(self):
        with self.assertRaises(build_script.ReproduceError):
            build_script.build_bundle(self.container(), self.source, self.root / "none", self.out)

    def test_release_signer(self):
        recorder = Recorder(answers={"apksigner": "Signer #1 certificate SHA-256 digest: 3c52c4fd\n"})
        with mock.patch.object(build_script, "run", recorder):
            self.assertEqual(build_script.release_signer(self.container(), "simplex-aarch64.apk"), "3C:52:C4:FD")


SIGNING_ENV = {
    "ANDROID_KEYSTORE_PASSWORD": "store-secret",
    "ANDROID_KEY_ALIAS": "simplex",
    "ANDROID_KEY_PASSWORD": "key-secret",
}


class AndroidSigningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keystore = pathlib.Path(self.tmp.name) / "release.keystore"
        self.keystore.write_bytes(b"keystore")
        self.environ = {**SIGNING_ENV, "ANDROID_KEYSTORE": str(self.keystore)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_unset_environment_builds_unsigned(self):
        self.assertIsNone(build_script.AndroidSigning.from_env({}))

    def test_partial_environment_names_what_is_missing(self):
        environ = dict(self.environ)
        del environ["ANDROID_KEY_PASSWORD"]
        with self.assertRaises(build_script.ReproduceError) as raised:
            build_script.AndroidSigning.from_env(environ)
        self.assertIn("missing: ANDROID_KEY_PASSWORD", str(raised.exception))

    def test_missing_keystore_file(self):
        with self.assertRaises(build_script.ReproduceError):
            build_script.AndroidSigning.from_env({**self.environ, "ANDROID_KEYSTORE": str(self.keystore) + ".gone"})

    def test_passwords_are_secrets_kept_out_of_env_and_repr(self):
        signing = build_script.AndroidSigning.from_env(self.environ)
        self.assertEqual(signing.env, {"ANDROID_KEYSTORE": "/keystore/release.keystore", "ANDROID_KEY_ALIAS": "simplex"})
        self.assertEqual(signing.secrets, {"ANDROID_KEYSTORE_PASSWORD": "store-secret", "ANDROID_KEY_PASSWORD": "key-secret"})
        self.assertNotIn("secret", repr(signing))

    def test_gradle_reads_the_same_variables(self):
        gradle = (pathlib.Path(build_script.__file__).parents[1] / "apps/multiplatform/android/build.gradle.kts").read_text()
        names = ", ".join(f'"{name}"' for name in build_script.ANDROID_SIGNING_ENV)
        self.assertIn(f"val releaseSigningEnv = listOf({names})", gradle)

    def test_container_mounts_the_keystore_read_only(self):
        recorder = Recorder()
        signing = build_script.AndroidSigning.from_env(self.environ)
        root = self.keystore.parent
        with mock.patch.object(build_script, "run", recorder), build_script.android_container(root, root, False, signing):
            pass
        start = next(c for c in recorder.commands if c[1] == "run")
        self.assertIn(f"{self.keystore.resolve()}:/keystore/release.keystore:ro", start)

    def test_apk_build_passes_passwords_by_name_only(self):
        source = pathlib.Path(self.tmp.name)
        (source / "apps/multiplatform").mkdir(parents=True)
        (source / "apps/multiplatform/gradle.properties").write_text("android.version_code=387\n")
        (source / "simplex-chat-arm64-v8a.apk").write_bytes(b"signed apk")
        recorder = Recorder(answers={"find": "/g/bin/gradle\n"})
        signing = build_script.AndroidSigning.from_env(self.environ)
        with mock.patch.object(build_script, "run", recorder):
            build_script.build_apk(build_script.Container(source, {}), source, Arch.AARCH64, source, "x.apk", False, False, signing)
        index = next(i for i, c in enumerate(recorder.commands) if "./scripts/android/build-android.sh" in c)
        build = recorder.commands[index]
        self.assertIn("ANDROID_KEYSTORE=/keystore/release.keystore", build)
        self.assertIn("ANDROID_KEY_ALIAS=simplex", build)
        self.assertEqual(build[build.index("ANDROID_KEY_PASSWORD") - 1], "-e")
        self.assertFalse(any("secret" in a for a in build))
        self.assertEqual(recorder.envs[index], signing.secrets)

    def test_unsigned_build_warns(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(build_script, "warn") as warn:
            self.assertIsNone(build_script.android_signing())
        self.assertIn("building unsigned", warn.call_args.args[0])


class VerifyApkTest(unittest.TestCase):
    def check(self, released_bytes, copier_outputs):
        """copier_outputs: what apksigcopier writes on each copy, in order."""
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            (out / "rel.apk").write_bytes(released_bytes)
            recorder = Recorder()
            outputs = list(copier_outputs)

            def fake_run(args, **kwargs):
                args = [str(a) for a in args]
                if "apksigcopier" in args:
                    (out / pathlib.Path(args[-1]).name).write_bytes(outputs.pop(0))
                return recorder(args, **kwargs)

            with mock.patch.object(build_script, "run", fake_run):
                result = build_script.verify_apk(build_script.Container(out, {}), out, "rel.apk", "built.apk")
            return result, recorder.steps()

    def test_agp_signed_release_verifies_without_realigning(self):
        result, steps = self.check(SIGNED, [SIGNED])
        self.assertTrue(result)
        self.assertEqual(steps, [["apksigcopier", "copy", "/out/rel.apk", "/out/built.apk", "/out/built.apk.signed"]])

    def test_flags_dropped_by_apksigcopier_are_restored(self):
        result, steps = self.check(SIGNED, [COPIED])
        self.assertTrue(result)
        self.assertEqual(len(steps), 1)

    def test_apksigner_padded_release_is_realigned_with_the_same_padding(self):
        padded = make_zip([("res/a.png", b"png", 0, APKSIGNER_PADDING)])
        other = make_zip([("res/a.png", b"png", 0, b"")])
        result, steps = self.check(padded, [other, padded])
        self.assertTrue(result)
        copy, zipalign, copy_aligned = steps
        self.assertIn("--pad-like-apksigner", zipalign)
        self.assertEqual(copy_aligned, ["apksigcopier", "copy", "/out/rel.apk", "/out/built.apk.aligned", "/out/built.apk.aligned.signed"])

    def test_zero_padded_release_is_realigned_without_apksigner_padding(self):
        other = make_zip([("classes.dex", b"other", 0, b"")])
        result, steps = self.check(SIGNED, [other, SIGNED])
        self.assertTrue(result)
        self.assertNotIn("--pad-like-apksigner", steps[1])

    def test_different_content_does_not_verify(self):
        other = make_zip([("classes.dex", b"other", 0, b"")])
        result, _ = self.check(SIGNED, [other, other])
        self.assertFalse(result)


class FakeRunner:
    """Stands in for a Container or HostRunner: paths are host paths, and actions[argv[0]] runs for each step."""

    def __init__(self, actions=None, answers=None):
        self.steps = []
        self.actions = actions or {}
        self.answers = answers or {}

    def run(self, step, capture=False, secrets=None):
        self.steps.append(step)
        action = self.actions.get(pathlib.Path(step.argv[0]).name)
        if action:
            action(step)
        return next((answer for pattern, answer in self.answers.items() if pattern in " ".join(step.argv)), "")

    def path(self, host):
        return str(host)

    def argvs(self):
        return [step.argv for step in self.steps]


class DesktopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.source = self.root / "src"
        self.multiplatform = self.source / "apps/multiplatform"
        self.multiplatform.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_files_of_each_image(self):
        self.assertEqual(
            build_script.desktop_files(JAMMY),
            [
                "simplex-chat-ubuntu-22_04-x86_64",
                "simplex-chat-ubuntu-22_04-x86_64.deb",
                "simplex-desktop-ubuntu-22_04-x86_64.deb",
                "simplex-desktop-x86_64.AppImage",
            ],
        )
        self.assertNotIn("simplex-desktop-x86_64.AppImage", build_script.desktop_files(NOBLE))
        postgres = build_script.linux_libs_zip("x86_64", build_script.Database.POSTGRES)
        self.assertEqual(postgres, "simplex-chat-libs-linux-x86_64-postgres.zip")

    def test_aarch64_files_and_image(self):
        self.assertEqual(build_script.linux_image("22.04", "aarch64"), JAMMY_ARM)
        self.assertEqual(
            build_script.desktop_files(JAMMY_ARM),
            [
                "simplex-chat-ubuntu-22_04-aarch64",
                "simplex-chat-ubuntu-22_04-aarch64.deb",
                "simplex-desktop-ubuntu-22_04-aarch64.deb",
                "simplex-desktop-aarch64.AppImage",
            ],
        )
        with self.assertRaises(build_script.ReproduceError):
            build_script.linux_image("24.04", "riscv64")

    def write_exports(self, definitions, flake):
        (self.source / "src/Simplex/Chat").mkdir(parents=True)
        (self.source / "src/Simplex/Chat/Mobile.hs").write_text(
            'foreign export ccall "chat_send_cmd" cChatSendCmd :: X\nforeign export ccall "chat_reopen_store" cReopen :: X\n'
        )
        (self.source / "libsimplex.dll.def").write_text(definitions)
        (self.source / "flake.nix").write_text(flake)

    def test_exports_are_checked_without_the_android_only_ones(self):
        self.write_exports("EXPORTS\n  chat_send_cmd\n", '"chat_send_cmd"\n')
        build_script.check_exports(self.source)

    def test_missing_or_duplicated_exports_fail(self):
        self.write_exports("EXPORTS\n", '"chat_send_cmd"\n')
        with self.assertRaises(build_script.ReproduceError) as raised:
            build_script.check_exports(self.source)
        self.assertIn("libsimplex.dll.def", str(raised.exception))
        (self.source / "libsimplex.dll.def").write_text("chat_send_cmd\n")
        (self.source / "flake.nix").write_text('"chat_send_cmd"\n"chat_send_cmd"\n')
        with self.assertRaises(build_script.ReproduceError):
            build_script.check_exports(self.source)

    def test_touch_tree_follows_symlinks_and_skips_dangling_ones(self):
        tree = self.root / "tree"
        (tree / "dir").mkdir(parents=True)
        (tree / "dir/file").write_text("x")
        (tree / "link").symlink_to("dir/file")
        (tree / "dangling").symlink_to("missing")
        build_script.touch_tree(tree, "1704067200")
        for path in (tree, tree / "dir", tree / "dir/file"):
            self.assertEqual(path.stat().st_mtime, 1704067200)
        self.assertFalse((tree / "missing").exists())

    def test_edit_lines_keeps_newlines_and_removes_none(self):
        path = self.root / "f"
        path.write_text("a\nb\nc")
        build_script.edit_lines(path, lambda line: None if line == "b" else line + "!")
        self.assertEqual(path.read_text(), "a!\nc!")

    def test_desktop_entry_registers_the_url_schemes(self):
        self.assertEqual(build_script.register_url_schemes("Exec=/opt/simplex/bin/simplex"), "Exec=/opt/simplex/bin/simplex %u")
        self.assertEqual(build_script.register_url_schemes("Exec=simplex %U"), "Exec=simplex %U")
        self.assertEqual(build_script.register_url_schemes("MimeType="), build_script.DESKTOP_MIME_TYPES)
        self.assertEqual(build_script.register_url_schemes("Name=SimpleX"), "Name=SimpleX")

    def test_cli_deb(self):
        binary = self.root / "simplex-chat"
        binary.write_bytes(b"x" * 2049)
        work = self.root / "work"
        work.mkdir()
        runner = FakeRunner()
        deb = build_script.build_cli_deb(runner, binary, "7.1.0", "aarch64", work)
        package = work / "deb-build/simplex-chat"
        control = (package / "DEBIAN/control").read_text()
        self.assertIn("Version: 7.1.0\nSection: Messenger\n", control)
        self.assertIn("Architecture: arm64\n", control)
        self.assertTrue(control.endswith("Installed-Size: 3\n"))
        self.assertEqual(stat.S_IMODE((package / "usr/bin/simplex-chat").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((package / "DEBIAN").stat().st_mode), 0o755)
        self.assertEqual((package / "usr/bin/simplex-chat").stat().st_mtime, 1764547200)
        self.assertEqual(runner.argvs()[0], ("dpkg-deb", "--build", "--root-owner-group", "--uniform-compression", "./simplex-chat"))
        self.assertEqual([s.env for s in runner.steps], [{"SOURCE_DATE_EPOCH": "1764547200"}] * 2)
        self.assertEqual(deb, work / "deb-build/simplex-chat.deb")

    def test_installed_size_rounds_as_printf(self):
        binary = self.root / "b"
        for size, expected in ((0, "1"), (512, "1"), (513, "2"), (1024, "2"), (1536, "2"), (2049, "3")):
            binary.write_bytes(b"x" * size)
            work = self.root / f"w{size}"
            work.mkdir()
            build_script.build_cli_deb(FakeRunner(), binary, "1", "x86_64", work)
            self.assertIn(f"Installed-Size: {expected}\n", (work / "deb-build/simplex-chat/DEBIAN/control").read_text())

    def test_linux_lib_copies_libsimplex_and_its_ghc_libraries(self):
        self.write_exports("chat_send_cmd\n", '"chat_send_cmd"\n')
        build = self.source / "dist-newstyle/build/x86_64-linux/ghc-9.6.3/simplex-chat-7.1.0/build"
        build.mkdir(parents=True)
        (build / "libHSsimplex-chat-7.1.0-inplace-ghc9.6.3.so").write_bytes(b"lib")
        (self.multiplatform / "desktop/build/cmake").mkdir(parents=True)

        def copy_ghc_libs(step):
            for name in step.argv[1:-1]:
                (pathlib.Path(step.argv[-1]) / pathlib.Path(name).name).write_bytes(b"ghc")

        ldd = "\tlibHSbase-4.18-ghc9.6.3.so => /ghc/lib/libHSbase-4.18-ghc9.6.3.so (0x1)\n\tlibc.so.6 => /lib/libc.so.6 (0x2)\n"
        runner = FakeRunner(actions={"cp": copy_ghc_libs}, answers={"ldd": ldd})
        libs = build_script.build_linux_lib(runner, self.source, "x86_64", build_script.Database.POSTGRES)
        cabal = runner.argvs()[0]
        self.assertIn("--builddir=dist-newstyle", cabal)
        self.assertIn("simplexmq +client_library +client_postgres", cabal)
        self.assertIn(("patchelf", "--replace-needed", "libffi.so.7", "libffi.so.8", "libsimplex.so"), runner.argvs())
        self.assertEqual(runner.argvs()[-1], ("cp", "/ghc/lib/libHSbase-4.18-ghc9.6.3.so", str(build / "deps")))
        self.assertEqual(libs, self.source / build_script.DESKTOP_LIBS_DIR / "linux-x86_64")
        self.assertEqual(sorted(p.name for p in libs.iterdir()), ["libHSbase-4.18-ghc9.6.3.so", "libsimplex.so"])
        self.assertFalse((self.multiplatform / "desktop/build/cmake").exists())

    def test_vlc_is_extracted_once(self):
        libs = self.root / "libs"
        libs.mkdir()
        work = self.root / "work"
        work.mkdir()

        def extract(step):
            (work / "squashfs-root/usr/lib/vlc").mkdir(parents=True)
            (work / "squashfs-root/usr/lib/libvlc.so").write_bytes(b"vlc")

        runner = FakeRunner(actions={"vlc-linux-x86_64.appimage": extract})
        with mock.patch.object(build_script, "fetch_pinned", lambda url, digest, target: target.write_bytes(b"") or target) as fetch:
            build_script.add_vlc_linux(runner, libs, "x86_64", work)
            build_script.add_vlc_linux(runner, libs, "x86_64", work)
        self.assertEqual(len(runner.steps), 1)
        self.assertEqual(runner.argvs()[0][1], "--appimage-extract")
        self.assertTrue((libs / "vlc/libvlc.so").exists())
        del fetch

    def test_libs_are_linked_where_gradle_finds_them(self):
        build_script.link_desktop_libs(self.source, "linux-aarch64", "linux-arm64")
        build_script.link_desktop_libs(self.source, "linux-aarch64", "linux-arm64")
        link = self.multiplatform / "build/links/linux-arm64"
        self.assertEqual(os.readlink(link), "../../common/src/commonMain/cpp/desktop/libs/linux-aarch64/")

    def test_desktop_deb_is_repacked_for_the_ubuntu_release(self):
        release = self.multiplatform / "release/main/deb"

        def gradle(step):
            release.mkdir(parents=True)
            (release / "simplex_7.1.0-1_amd64.deb").write_bytes(b"jpackage")

        def dpkg(step):
            if step.argv[1] == "-R":
                app = self.multiplatform / "extracted/opt/simplex"
                (app / "lib/app").mkdir(parents=True)
                (self.multiplatform / "extracted/DEBIAN").mkdir()
                (self.multiplatform / "extracted/DEBIAN/control").write_text("Package: simplex\nVersion: 7.1.0-1\n")
                (app / "lib/app/skiko-awt-runtime-linux-x64-0.8.jar").write_bytes(b"jar")
                cfg = "app.classpath=$APPDIR/a.jar\napp.classpath=$APPDIR/skiko-awt-runtime-linux-x64-0.8.jar\n"
                (app / "lib/app/simplex.cfg").write_text(cfg)
                (app / "lib/simplex-SimpleX.desktop").write_text("[Desktop Entry]\nExec=/opt/simplex/bin/simplex\nMimeType=\n")
            else:
                (release / "simplex_x86_64.deb").write_bytes(b"repacked")

        runner = FakeRunner(actions={"gradlew": gradle, "dpkg-deb": dpkg})
        captured = {}
        original_touch = build_script.touch_tree

        def touch(root, epoch):
            captured["control"] = (root / "DEBIAN/control").read_text()
            captured["cfg"] = (root / "opt/simplex/lib/app/simplex.cfg").read_text()
            captured["entry"] = (root / "opt/simplex/lib/simplex-SimpleX.desktop").read_text()
            captured["jars"] = sorted(p.name for p in (root / "opt/simplex/lib/app").iterdir())
            original_touch(root, epoch)

        with mock.patch.object(build_script, "touch_tree", touch):
            deb = build_script.package_desktop_deb(runner, self.source, "x86_64", "jammy")
        self.assertEqual(deb, release / "simplex_x86_64.deb")
        self.assertEqual(captured["control"], "Package: simplex\nVersion: 7.1.0-1~jammy\n")
        self.assertNotIn("skiko", captured["cfg"])
        self.assertEqual(captured["jars"], ["simplex.cfg"])
        self.assertIn("Exec=/opt/simplex/bin/simplex %u\n", captured["entry"])
        self.assertIn(build_script.DESKTOP_MIME_TYPES, captured["entry"])
        self.assertEqual(runner.argvs()[0], ("./gradlew", "-Psimplex.assets.dir=../../assets", "packageDeb"))
        self.assertEqual(runner.steps[2].env, {"SOURCE_DATE_EPOCH": "1704067200"})
        self.assertFalse((self.multiplatform / "extracted").exists())

    def test_ubuntu_codename(self):
        runner = FakeRunner(answers={"os-release": 'NAME="Ubuntu"\nVERSION_CODENAME=noble\n'})
        self.assertEqual(build_script.ubuntu_codename(runner), "noble")
        with self.assertRaises(build_script.ReproduceError):
            build_script.ubuntu_codename(FakeRunner())

    def test_appimage(self):
        libs = self.source / build_script.DESKTOP_LIBS_DIR / "linux-x86_64"
        libs.mkdir(parents=True)
        libcrypto = self.root / "libcrypto.so.3"
        libcrypto.write_bytes(b"pinned")
        distribute = self.multiplatform / "desktop/src/jvmMain/resources/distribute"
        distribute.mkdir(parents=True)
        (distribute / "chat.simplex.app.desktop").write_text("Exec=/opt/x/simplex %u\nIcon=chat.simplex.app\n")
        (distribute / "chat.simplex.app.appdata.xml").write_text("<x/>")
        app = self.multiplatform / "release/main/app"
        bundled_during_gradle = []

        def gradle(step):
            bundled_during_gradle.append((libs / "libcrypto.so.3").read_bytes())
            (app / "simplex/bin").mkdir(parents=True)
            (app / "simplex/lib/app").mkdir(parents=True)
            (app / "simplex/bin/simplex").write_bytes(b"launcher")
            (app / "simplex/lib/simplex.png").write_bytes(b"png")
            (app / "simplex/lib/app/skiko-awt-runtime-linux-x64.jar").write_bytes(b"jar")
            (app / "simplex/lib/app/simplex.cfg").write_text("skiko-awt-runtime-linux-x64.jar\nother\n")

        def appimagetool(step):
            (app / "AppDir/SimpleX_Chat-x86_64.AppImage").write_bytes(b"appimage")

        runner = FakeRunner(actions={"gradlew": gradle, "appimagetool-x86_64.AppImage": appimagetool})
        work = self.root / "work"
        work.mkdir()
        with mock.patch.object(build_script, "fetch_pinned", lambda url, digest, target: target.write_bytes(b"") or target):
            image = build_script.package_appimage(runner, self.source, "x86_64", libcrypto, work)
        self.assertEqual(bundled_during_gradle, [b"pinned"])
        self.assertFalse((libs / "libcrypto.so.3").exists())
        appdir = app / "AppDir"
        self.assertEqual(os.readlink(appdir / "AppRun"), "usr/bin/simplex")
        self.assertEqual((appdir / "usr/share/applications/chat.simplex.app.desktop").read_text(), "Exec=simplex %u\nIcon=simplex\n")
        self.assertTrue((appdir / "usr/share/metainfo/chat.simplex.app.appdata.xml").exists())
        self.assertFalse((appdir / "usr/lib/app/skiko-awt-runtime-linux-x64.jar").exists())
        self.assertEqual((appdir / "usr/lib/app/simplex.cfg").read_text(), "other\n")
        tool_step = next(s for s in runner.steps if s.argv[0].endswith("appimagetool-x86_64.AppImage"))
        self.assertEqual(tool_step.argv[1:4], ("--verbose", "--no-appstream", "--runtime-file"))
        self.assertEqual(tool_step.env, {"SOURCE_DATE_EPOCH": "1704067200"})
        self.assertEqual(image, self.multiplatform / "release/main/SimpleX_Chat-x86_64.AppImage")

    def test_desktop_build_order_and_released_names(self):
        out = self.root / "out"
        out.mkdir()

        def made(name):
            def make(*args, **kwargs):
                path = self.root / name
                path.write_bytes(name.encode())
                return path

            return make

        container = FakeRunner(answers={"list-bin": "/project/dist/simplex-chat\n", "os-release": "VERSION_CODENAME=jammy\n"})
        container.__enter__ = lambda: container
        calls = []

        def record(name, result=None):
            def call(*args, **kwargs):
                calls.append(name)
                return result(*args) if result else None

            return call

        def cp(step):
            if step.argv[0] == "cp":
                pathlib.Path(step.argv[2]).write_bytes(b"cli")

        container.actions["cp"] = cp
        with (
            mock.patch.object(build_script, "linux_container", return_value=contextlib.nullcontext(container)),
            mock.patch.object(build_script, "build_cli_deb", record("cli deb", made("cli.deb"))),
            mock.patch.object(build_script, "build_linux_lib", record("lib", lambda *a: self.root)),
            mock.patch.object(build_script, "add_vlc_linux", record("vlc")),
            mock.patch.object(build_script, "link_desktop_libs", record("link")),
            mock.patch.object(build_script, "package_desktop_deb", record("desktop deb", made("desktop.deb"))),
            mock.patch.object(build_script, "zip_desktop_libs", record("libs zip")),
            mock.patch.object(build_script, "fetch_openssl", record("openssl", lambda work: work)),
            mock.patch.object(build_script, "package_appimage", record("appimage", made("app.AppImage"))),
            mock.patch.object(build_script, "log"),
        ):
            build_script.build_desktop_linux(self.source, JAMMY, "7.1.0", out)
        self.assertEqual(calls, ["cli deb", "lib", "vlc", "link", "desktop deb", "libs zip", "openssl", "appimage"])
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(build_script.desktop_files(JAMMY)))
        configure = next(a for a in container.argvs() if a[:2] == ("perl", "Configure"))
        self.assertIn("--openssldir=/etc/ssl", configure)
        self.assertIn("flags: +openssl", (self.source / "cabal.project.local").read_text())

    def test_release_made_before_this_script_is_built_by_its_own_scripts(self):
        out, work = self.root / "out", self.root / "work"
        out.mkdir()
        work.mkdir()
        for script in (build_script.TAG_CLI_DEB_SCRIPT, build_script.TAG_DESKTOP_DEB_SCRIPT, build_script.TAG_APPIMAGE_SCRIPT):
            (self.source / script).parent.mkdir(parents=True, exist_ok=True)
            (self.source / script).write_text("#!/bin/sh\n")
        release = self.source / build_script.DESKTOP_RELEASE_DIR

        def run_script(step):
            name = pathlib.Path(step.argv[0]).name
            if name == "build-cli-deb.sh":
                pathlib.Path(step.argv[3]).mkdir()
                (pathlib.Path(step.argv[3]) / "simplex-chat.deb").write_bytes(b"cli deb")
            elif name == "make-deb-linux.sh":
                (release / "deb").mkdir(parents=True)
                (release / "deb/simplex_x86_64.deb").write_bytes(b"desktop deb")
            elif name == "make-appimage-linux.sh":
                (release / "SimpleX_Chat-x86_64.AppImage").write_bytes(b"appimage")

        container = FakeRunner(
            actions={name: run_script for name in ("build-cli-deb.sh", "make-deb-linux.sh", "make-appimage-linux.sh")},
            answers={"list-bin": "/project/dist/simplex-chat\n"},
        )
        container.actions["cp"] = lambda step: pathlib.Path(step.argv[2]).write_bytes(b"cli")
        with (
            mock.patch.object(build_script, "linux_container", return_value=contextlib.nullcontext(container)),
            mock.patch.object(build_script.tempfile, "TemporaryDirectory", return_value=contextlib.nullcontext(str(work))),
            mock.patch.object(build_script, "build_cli_deb") as python_deb,
            mock.patch.object(build_script, "log"),
        ):
            build_script.build_desktop_linux(self.source, JAMMY, "7.0.3", out)
        python_deb.assert_not_called()
        self.assertEqual(
            {p.name: p.read_bytes() for p in out.iterdir()},
            {
                "simplex-chat-ubuntu-22_04-x86_64": b"cli",
                "simplex-chat-ubuntu-22_04-x86_64.deb": b"cli deb",
                "simplex-desktop-ubuntu-22_04-x86_64.deb": b"desktop deb",
                "simplex-desktop-x86_64.AppImage": b"appimage",
            },
        )
        scripts = [s for s in container.steps if s.argv[0].startswith("scripts/")]
        self.assertEqual(scripts[0].argv[1], "7.0.3")
        self.assertEqual(scripts[1].env, {"ASSETS_DIR": "../../assets"})

    def test_reproduce_reports_mismatches_and_writes_sums_only_when_all_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = pathlib.Path(tmp) / "v7.1.0-simplex-chat"

            def fake_build(source, image, version, out):
                for name in build_script.desktop_files(image):
                    (out / name).write_bytes(name.encode())

            def fake_download(url, target):
                target.write_bytes(b"other" if target.name.endswith(".AppImage") else target.name.encode())

            with (
                mock.patch.object(build_script, "checkout"),
                mock.patch.object(build_script, "build_desktop_linux", fake_build),
                mock.patch.object(build_script, "download", fake_download),
                mock.patch.object(build_script, "log"),
            ):
                mismatched = build_script.reproduce_desktop("v7.1.0", pathlib.Path(tmp), results)
            self.assertEqual(mismatched, ["simplex-desktop-x86_64.AppImage"])
            self.assertFalse((results / "_sha256sums").exists())


class OpenSslTest(unittest.TestCase):
    def tarball(self, path):
        member = tarfile.TarInfo(f"openssl-{build_script.OPENSSL_VERSION}/Configure")
        member.size = 4
        with tarfile.open(path, "w:gz") as tar:
            tar.addfile(member, io.BytesIO(b"perl"))

    def test_fetch_extracts_the_checked_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            self.tarball(work / "fixture.tar.gz")
            digest = build_script.sha256(work / "fixture.tar.gz")

            def fixture_download(url, target):
                shutil.copy(work / "fixture.tar.gz", target)

            with mock.patch.object(build_script, "download", fixture_download), mock.patch.object(build_script, "OPENSSL_SHA256", digest):
                source = build_script.fetch_openssl(work)
            self.assertEqual((source / "Configure").read_text(), "perl")

    def test_fetch_rejects_a_changed_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            with (
                mock.patch.object(build_script, "download", lambda url, target: self.tarball(target)),
                self.assertRaises(build_script.ReproduceError) as raised,
            ):
                build_script.fetch_openssl(work)
            self.assertIn("pinned SHA-256", str(raised.exception))
            self.assertFalse((work / f"openssl-{build_script.OPENSSL_VERSION}").exists())

    def test_build_installs_only_when_asked(self):
        build = build_script.openssl_build("darwin64-arm64-cc", "/w/openssl", "/etc/ssl", install=True)
        self.assertEqual(
            build[0],
            ("perl", "Configure", "darwin64-arm64-cc", "--prefix=/w/openssl", "--openssldir=/etc/ssl")
            + ("--libdir=lib", "no-docs", "no-tests"),
        )
        self.assertEqual(build[-1], ("make", "install_sw"))
        self.assertNotIn(("make", "install_sw"), build_script.openssl_build("mingw64", "/p", "/d", install=False))

    def test_flake_pins_the_same_openssl(self):
        flake = (pathlib.Path(build_script.__file__).parents[1] / "flake.nix").read_text()
        pin = flake[flake.index("let pinOpenssl") : flake.index("}); in", flake.index("let pinOpenssl"))]
        self.assertIn(f'version = "{build_script.OPENSSL_VERSION}";', pin)
        self.assertIn(f'url = "{build_script.OPENSSL_URL}";', pin)
        self.assertIn(f'sha256 = "{build_script.OPENSSL_SHA256}";', pin)


class LinuxLibsTest(unittest.TestCase):
    def test_host_build_updates_cabal_and_zips_the_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            libs = root / "libs"
            libs.mkdir()
            (libs / "libsimplex.so").write_bytes(b"lib")
            (libs / "vlc").mkdir()
            recorder = Recorder()
            with (
                mock.patch.object(build_script, "run", recorder),
                mock.patch.object(build_script, "build_linux_lib", return_value=libs) as lib,
            ):
                build_script.build_linux_libs(
                    root, JAMMY, build_script.Database.POSTGRES, "dist-newstyle", build_script.LibsBuilder(on_host=True), root
                )
            self.assertEqual(recorder.commands, [["cabal", "update"]])
            self.assertIsInstance(lib.call_args.args[0], build_script.HostRunner)
            with zipfile.ZipFile(root / "simplex-chat-libs-linux-x86_64-postgres.zip") as z:
                self.assertEqual(z.namelist(), ["libs/libsimplex.so"])

    def test_running_container_is_used_and_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            libs = root / "libs"
            libs.mkdir()
            recorder = Recorder()
            with (
                mock.patch.object(build_script, "run", recorder),
                mock.patch.object(build_script, "build_linux_lib", return_value=libs) as lib,
            ):
                builder = build_script.LibsBuilder(container="builder")
                build_script.build_linux_libs(root, JAMMY, build_script.Database.SQLITE, "dist-lib", builder, root)
            self.assertEqual(recorder.commands, [["docker", "exec", "-w", "/project", "builder", "cabal", "update"]])
            self.assertEqual(lib.call_args.args[4], "dist-lib")


class HostDesktopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.source, self.out = self.root / "src", self.root / "out"
        self.source.mkdir()
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, work):
        source = work / f"openssl-{build_script.OPENSSL_VERSION}"
        source.mkdir(exist_ok=True)
        (source / "libcrypto.a").write_bytes(b"static")
        return source

    def zip_names(self, name):
        with zipfile.ZipFile(self.out / name) as z:
            return sorted(z.namelist())

    def openssl_on(self, os_name, sys_platform, machine="arm64"):
        recorder = Recorder()
        with (
            mock.patch.object(build_script, "run", recorder),
            mock.patch.object(build_script, "fetch_openssl", self.fetch),
            mock.patch.object(build_script.os, "name", os_name),
            mock.patch.object(build_script.sys, "platform", sys_platform),
            mock.patch.object(build_script.platform, "machine", return_value=machine),
        ):
            return build_script.host_openssl(self.root), recorder.commands

    def test_openssl_for_windows_stays_in_its_source_tree(self):
        openssl, commands = self.openssl_on("nt", "win32")
        tree = self.root / f"openssl-{build_script.OPENSSL_VERSION}"
        self.assertEqual(openssl, build_script.OpenSsl(tree / "include", tree))
        self.assertEqual(
            commands[0][2:5], ["mingw64", "--prefix=C:/Program Files/OpenSSL", "--openssldir=C:/Program Files/Common Files/SSL"]
        )
        self.assertNotIn(["make", "install_sw"], commands)

    def test_openssl_for_macos_is_installed(self):
        openssl, commands = self.openssl_on("posix", "darwin", "x86_64")
        self.assertEqual(openssl, build_script.OpenSsl(self.root / "openssl/include", self.root / "openssl/lib"))
        self.assertEqual(commands[0][2:5], ["darwin64-x86_64-cc", f"--prefix={self.root / 'openssl'}", "--openssldir=/etc/ssl"])
        self.assertEqual(commands[-1], ["make", "install_sw"])

    def test_linux_has_no_pinned_openssl_build(self):
        with self.assertRaises(build_script.ReproduceError):
            self.openssl_on("posix", "linux")

    def test_openssl_command_writes_cabal_local(self):
        with mock.patch.object(
            build_script, "host_openssl", return_value=build_script.OpenSsl(self.root / "o/include", self.root / "o/lib")
        ):
            build_script.build_host_openssl(self.source, self.root)
        self.assertIn(f"extra-lib-dirs: {self.root / 'o/lib'}", (self.source / "cabal.project.local").read_text())

    def test_cabal_local_links_both_packages_with_openssl(self):
        text = build_script.openssl_cabal_local("/o/include", "/o/lib", "-optl-lws2_32")
        self.assertEqual(text.count("extra-include-dirs: /o/include"), 2)
        self.assertIn("package simplexmq\n", text)
        self.assertIn("package direct-sqlcipher\n    extra-include-dirs: /o/include\n    extra-lib-dirs: /o/lib\n    flags: +openssl", text)
        self.assertTrue(text.endswith("package simplex-chat\n    ghc-options: -optl-lws2_32\n"))
        self.assertNotIn("simplex-chat", build_script.openssl_cabal_local("/i", "/l"))

    def test_cli_loading_libcrypto_is_rejected(self):
        with self.assertRaises(build_script.ReproduceError):
            build_script.check_static_openssl(pathlib.Path("cli"), "\t/opt/homebrew/opt/openssl@3.0/lib/libcrypto.3.dylib\n")
        build_script.check_static_openssl(pathlib.Path("cli"), "\t/usr/lib/libSystem.B.dylib\n")

    def test_apple_signing_needs_all_or_none(self):
        full = {name: "x" for name in build_script.APPLE_SIGNING_ENV}
        with mock.patch.dict(os.environ, full, clear=True):
            self.assertTrue(build_script.apple_signing(require=True))
        partial = {"APPLE_SIMPLEX_SIGNING_KEYCHAIN": "x"}
        with mock.patch.dict(os.environ, partial, clear=True), self.assertRaises(build_script.ReproduceError):
            build_script.apple_signing(require=False)
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(build_script.ReproduceError):
            build_script.apple_signing(require=True)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(build_script, "warn") as warn:
            self.assertFalse(build_script.apple_signing(require=False))
        self.assertIn("unsigned", warn.call_args.args[0])

    def test_macos_unsigned_build(self):
        libs = self.root / "libs-app"
        libs.mkdir()
        (libs / "libsimplex.dylib").write_bytes(b"lib")
        dmg = self.root / "SimpleX-7.1.0.dmg"
        dmg.write_bytes(b"dmg")
        work = self.root / "work"
        (work / "openssl/lib").mkdir(parents=True)
        (work / "openssl/lib/libcrypto.a").write_bytes(b"static")
        recorder = Recorder(answers={"list-bin": f"{self.root}/dist/simplex-chat\n", "otool -L": "\t/usr/lib/libSystem.B.dylib\n"})
        (self.root / "dist").mkdir()
        (self.root / "dist/simplex-chat").write_bytes(b"cli")
        calls = []
        recorder_cabal_locals = []
        original_cli = build_script.build_host_cli

        def build_cli(source, cabal_local):
            recorder_cabal_locals.append(cabal_local)
            return original_cli(source, cabal_local)

        with (
            mock.patch.object(build_script, "build_host_cli", build_cli),
            mock.patch.object(build_script, "run", recorder),
            mock.patch.object(
                build_script, "host_openssl", return_value=build_script.OpenSsl(work / "openssl/include", work / "openssl/lib")
            ),
            mock.patch.object(build_script.platform, "machine", return_value="arm64"),
            mock.patch.object(build_script, "apple_signing", return_value=False),
            mock.patch.object(build_script.tempfile, "TemporaryDirectory", return_value=contextlib.nullcontext(str(work))),
            mock.patch.object(build_script, "build_mac_lib", lambda source, arch, database, work: calls.append(("lib", arch)) or libs),
            mock.patch.object(build_script, "add_vlc_mac", lambda libs, arch, work: (libs / "vlc").mkdir()),
            mock.patch.object(build_script, "link_desktop_libs", lambda *args: calls.append(("link", *args[1:]))),
            mock.patch.object(build_script, "package_dmg", lambda source, keychain: calls.append(("dmg", keychain)) or dmg),
            mock.patch.object(build_script, "sign_mac_libs") as sign,
        ):
            build_script.build_desktop_macos(self.source, self.out, require_signing=False)
        self.assertIn(f"extra-lib-dirs: {work / 'openssl-static'}", recorder_cabal_locals[0])
        self.assertEqual(calls, [("lib", "aarch64"), ("link", "mac-aarch64", "macos-arm64"), ("dmg", None)])
        sign.assert_not_called()
        self.assertIn(f"extra-lib-dirs: {work / 'openssl/lib'}", (self.source / "cabal.project.local").read_text())
        self.assertEqual(
            sorted(p.name for p in self.out.iterdir()),
            ["simplex-chat-libs-macos-aarch64.zip", "simplex-chat-macos-aarch64", "simplex-desktop-macos-aarch64.dmg"],
        )
        self.assertEqual(self.zip_names("simplex-chat-libs-macos-aarch64.zip"), ["libs/libsimplex.dylib"])

    def test_macos_cli_linking_libcrypto_fails(self):
        recorder = Recorder(answers={"list-bin": f"{self.root}/cli\n", "otool -L": "\t@rpath/libcrypto.3.dylib\n"})
        with (
            mock.patch.object(build_script, "run", recorder),
            mock.patch.object(build_script, "host_openssl", return_value=build_script.OpenSsl(self.root, self.root)),
            mock.patch.object(build_script.platform, "machine", return_value="x86_64"),
            mock.patch.object(build_script, "apple_signing", return_value=False),
            mock.patch.object(build_script, "static_libcrypto", return_value=self.root),
            self.assertRaises(build_script.ReproduceError),
        ):
            build_script.build_desktop_macos(self.source, self.out, require_signing=False)

    def test_windows_build(self):
        libs = self.root / "libs-app"
        libs.mkdir()
        (libs / "libsimplex.dll").write_bytes(b"lib")
        (self.root / "dist").mkdir()
        (self.root / "dist/simplex-chat.exe").write_bytes(b"cli")
        msi = self.source / build_script.DESKTOP_RELEASE_DIR / "msi"
        recorder = Recorder(answers={"list-bin": f"{self.root}/dist/simplex-chat.exe\n", "objdump": "\tDLL Name: KERNEL32.dll\n"})

        def fake_run(args, **kwargs):
            args = [str(a) for a in args]
            if args[0] == "dlltool":
                (self.source / "libsimplex.lib").write_bytes(b"import lib")
            if "packageMsi" in args:
                msi.mkdir(parents=True)
                (msi / "SimpleX-7.1.0.msi").write_bytes(b"msi")
            return recorder(args, **kwargs)

        calls = []
        with (
            mock.patch.object(build_script, "run", fake_run),
            mock.patch.object(
                build_script, "host_openssl", lambda work: build_script.OpenSsl(self.fetch(work) / "include", self.fetch(work))
            ),
            mock.patch.object(build_script, "build_windows_lib", lambda source, openssl: calls.append(("lib", openssl.lib.name)) or libs),
            mock.patch.object(build_script, "add_vlc_windows", lambda libs, work: (libs / "vlc").mkdir()),
            mock.patch.object(build_script, "link_desktop_libs", lambda *args: calls.append(("link", *args[1:]))),
        ):
            build_script.build_desktop_windows(self.source, self.out)
        self.assertEqual(calls, [("lib", f"openssl-{build_script.OPENSSL_VERSION}"), ("link", "windows-x86_64", "windows-x64")])
        self.assertIn(["bash", "./gradlew", "-Psimplex.assets.dir=../../assets", "packageMsi"], recorder.commands)
        self.assertEqual(
            sorted(p.name for p in self.out.iterdir()),
            ["simplex-chat-libs-windows-x86_64.zip", "simplex-chat-windows-x86-64", "simplex-desktop-windows-x86_64.msi"],
        )
        self.assertEqual(self.zip_names("simplex-chat-libs-windows-x86_64.zip"), ["libs/libsimplex.dll", "libs/libsimplex.lib"])

    def test_cli_cabal_local_is_static_on_windows(self):
        cabal_locals = []

        def fake_cli(source, cabal_local):
            cabal_locals.append(cabal_local)
            raise build_script.ReproduceError("stop")

        with (
            mock.patch.object(build_script, "run"),
            mock.patch.object(
                build_script, "host_openssl", lambda work: build_script.OpenSsl(self.fetch(work) / "include", self.fetch(work))
            ),
            mock.patch.object(build_script, "build_host_cli", fake_cli),
            self.assertRaises(build_script.ReproduceError),
        ):
            build_script.build_desktop_windows(self.source, self.out)
        self.assertIn("/openssl-static\n", cabal_locals[0])
        self.assertIn("ghc-options: -optl-lws2_32 -optl-lgdi32 -optl-lcrypt32", cabal_locals[0])

    def test_read_only_cabal_checkouts_are_removed(self):
        pack = self.source / "dist-newstyle/src/simplexmq-abc/.git/objects/pack/p.pack"
        pack.parent.mkdir(parents=True)
        pack.write_bytes(b"pack")
        pack.chmod(0o444)
        pack.parent.chmod(0o555)
        keep = self.source / "dist-newstyle/src/aeson-1"
        keep.mkdir()
        tarball = self.source / "dist-newstyle/src/direct-sq_-fd06-direct-sqlcipher-2.3.28.tar.gz"
        tarball.write_bytes(b"tar")
        build_script.remove_cabal_sources(self.source)
        self.assertFalse(tarball.exists())
        self.assertFalse((self.source / "dist-newstyle/src/simplexmq-abc").exists())
        self.assertTrue(keep.exists())


def otool_l(rpaths=(), loads=()):
    lines = ["lib.dylib:", "Load command 0", "      cmd LC_ID_DYLIB"]
    for path in loads:
        lines += ["          cmd LC_LOAD_DYLIB", f"         name {path} (offset 24)"]
    for path in rpaths:
        lines += ["          cmd LC_RPATH", f"         path {path} (offset 12)"]
    return "\n".join(lines) + "\n"


def otool_big_l(own, deps):
    lines = ["lib.dylib:", f"\t{own} (compatibility version 0.0.0, current version 0.0.0)"]
    return "\n".join(lines + [f"\t{d} (compatibility version 0.0.0)" for d in deps]) + "\n"


class FakeOtool:
    """Stands in for run(): answers otool for each library from tables that install_name_tool edits, records commands."""

    def __init__(self, big_l=None, small_l=None, answers=None):
        self.big_l, self.small_l = big_l or {}, small_l or {}
        self.answers = answers or {}
        self.commands = []

    def __call__(self, args, cwd=None, env=None, input=None, check=True, capture=False, title=None):
        args = [str(a) for a in args]
        self.commands.append(args)
        if args[0] == "otool":
            table = self.big_l if args[1] == "-L" else self.small_l
            return mock.Mock(returncode=0, stdout=table.get(pathlib.Path(args[2]).name, ""))
        if args[:2] == ["install_name_tool", "-delete_rpath"]:
            name = pathlib.Path(args[3]).name
            self.small_l[name] = "".join(line for line in self.small_l[name].splitlines(keepends=True) if f"path {args[2]} " not in line)
        if args[:2] == ["install_name_tool", "-change"]:
            name = pathlib.Path(args[4]).name
            self.small_l[name] = self.small_l[name].replace(args[2], args[3])
        stdout = next((answer for pattern, answer in self.answers.items() if pattern in " ".join(args)), "")
        return mock.Mock(returncode=0, stdout=stdout)


class MacLibTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_otool_parsing(self):
        fake = FakeOtool(
            big_l={"lib.dylib": otool_big_l("@rpath/lib.dylib", ["@rpath/libHSbase.dylib", "/usr/lib/libSystem.B.dylib"])},
            small_l={"lib.dylib": otool_l(["@loader_path/../lib/x", "/Users/runner/ghc"], ["/w/openssl/lib/libcrypto.3.dylib"])},
        )
        lib = pathlib.Path("lib.dylib")
        with mock.patch.object(build_script, "run", fake):
            self.assertEqual(build_script.rpath_dependencies(lib), ["libHSbase.dylib"])
            self.assertEqual(build_script.rpaths(lib), ["@loader_path/../lib/x", "/Users/runner/ghc"])
            self.assertEqual(build_script.libcrypto_path(lib), "/w/openssl/lib/libcrypto.3.dylib")

    def test_dependencies_are_copied_once_with_loader_path_rpaths(self):
        ghc = self.root / "ghc"
        (ghc / "lib").mkdir(parents=True)
        build = self.root / "build"
        deps = build / "deps"
        deps.mkdir(parents=True)
        (build / "libsimplex.dylib").write_bytes(b"simplex")
        (ghc / "lib/libHSbase.dylib").write_bytes(b"base")
        (ghc / "lib/libHSrts.dylib").write_bytes(b"rts")
        fake = FakeOtool(
            big_l={
                "libsimplex.dylib": otool_big_l("@rpath/libsimplex.dylib", ["@rpath/libHSbase.dylib", "@rpath/libHSrts.dylib"]),
                "libHSbase.dylib": otool_big_l("@rpath/libHSbase.dylib", ["@rpath/libHSrts.dylib"]),
                "libHSrts.dylib": otool_big_l("@rpath/libHSrts.dylib", []),
            },
            small_l={
                "libsimplex.dylib": otool_l([str(ghc / "lib")]),
                "libHSbase.dylib": otool_l(["@loader_path/../lib"]),
                "libHSrts.dylib": otool_l([]),
            },
        )
        with mock.patch.object(build_script, "run", fake):
            build_script.copy_mac_dependencies(build / "libsimplex.dylib", deps, ghc, set())
        self.assertEqual(sorted(p.name for p in deps.iterdir()), ["libHSbase.dylib", "libHSrts.dylib", "libsimplex.dylib"])
        add_rpath = [c for c in fake.commands if c[:2] == ["install_name_tool", "-add_rpath"]]
        self.assertEqual(add_rpath, [["install_name_tool", "-add_rpath", "@loader_path", str(deps / "libHSbase.dylib")]])

    def test_libcrypto_is_bundled_and_local_rpaths_deleted(self):
        libs = self.root / "libs"
        libs.mkdir()
        openssl = self.root / "openssl/lib"
        openssl.mkdir(parents=True)
        (openssl / "libcrypto.3.dylib").write_bytes(b"crypto")
        for name in ("libHSsmplxmq-6.5.dylib", "libsimplex.dylib", "libHSdrct-2.3.dylib", "libffi.dylib"):
            (libs / name).write_bytes(b"x")
        linked = str(openssl / "libcrypto.3.dylib")
        fake = FakeOtool(
            small_l={
                "libHSsmplxmq-6.5.dylib": otool_l(["/Users/runner/ghc/lib"], [linked]),
                "libHSdrct-2.3.dylib": otool_l([], [linked]),
                "libsimplex.dylib": otool_l(["@loader_path"]),
            }
        )
        with mock.patch.object(build_script, "run", fake):
            build_script.bundle_mac_libcrypto(libs, build_script.Database.SQLITE)
        self.assertEqual((libs / "libcrypto.3.0.dylib").read_bytes(), b"crypto")
        changes = [c[-1] for c in fake.commands if c[:2] == ["install_name_tool", "-change"]]
        self.assertEqual(sorted(pathlib.Path(c).name for c in changes), ["libHSdrct-2.3.dylib", "libHSsmplxmq-6.5.dylib"])
        self.assertIn(["install_name_tool", "-id", "libcrypto.3.0.dylib", str(libs / "libcrypto.3.0.dylib")], fake.commands)
        self.assertIn(["install_name_tool", "-delete_rpath", "/Users/runner/ghc/lib", str(libs / "libHSsmplxmq-6.5.dylib")], fake.commands)

    def test_libraries_still_pointing_to_local_directories_fail(self):
        libs = self.root / "libs"
        libs.mkdir()
        (libs / "libHSsmplxmq-6.5.dylib").write_bytes(b"x")
        (libs / "libffi.dylib").write_bytes(b"x")
        crypto = self.root / "libcrypto.3.dylib"
        crypto.write_bytes(b"c")
        fake = FakeOtool(small_l={"libHSsmplxmq-6.5.dylib": otool_l([], [str(crypto), "/opt/homebrew/lib/libgmp.dylib"])})
        with mock.patch.object(build_script, "run", fake), self.assertRaises(build_script.ReproduceError) as raised:
            build_script.bundle_mac_libcrypto(libs, build_script.Database.SQLITE)
        self.assertIn("libHSsmplxmq-6.5.dylib", str(raised.exception))

    def test_vlc_gets_rpaths_and_its_plugin_cache(self):
        libs, work = self.root / "libs", self.root / "work"
        libs.mkdir()
        work.mkdir()
        macos = work / "vlc/VLC.app/Contents/MacOS"

        def fake_run(args, **kwargs):
            if args[0] == "unzip":
                (macos / "lib").mkdir(parents=True)
                (macos / "plugins").mkdir()
                (macos / "lib/libvlc.dylib").write_bytes(b"vlc")
                (macos / "plugins/libaudio_plugin.dylib").write_bytes(b"p")
            return recorder(args, **kwargs)

        recorder = Recorder()
        with mock.patch.object(build_script, "run", fake_run), mock.patch.object(build_script, "fetch_pinned", lambda u, d, t: t):
            build_script.add_vlc_mac(libs, "aarch64", work)
        plugin = str(macos / "plugins/libaudio_plugin.dylib")
        self.assertIn(["install_name_tool", "-add_rpath", "@loader_path/../../", plugin], recorder.commands)
        self.assertIn([str(work / "vlc/vlc-cache-gen"), "plugins"], recorder.commands)
        self.assertTrue((libs / "vlc/libvlc.dylib").exists())
        self.assertTrue((libs / "vlc/vlc/plugins/libaudio_plugin.dylib").exists())

    def test_keychain_is_searched_while_signing_and_removed_after(self):
        work = self.root
        recorder = Recorder(answers={"list-keychains": '    "/Users/r/login.keychain-db"\n'})
        environ = {"APPLE_SIMPLEX_SIGNING_KEYCHAIN": base64.b64encode(b"keychain").decode()}
        with mock.patch.object(build_script, "run", recorder), mock.patch.dict(os.environ, environ):
            with build_script.apple_keychain(work) as keychain:
                self.assertEqual(keychain.read_bytes(), b"keychain")
            self.assertFalse(keychain.exists())
        searches = [c for c in recorder.commands if c[:3] == ["security", "list-keychains", "-s"]]
        self.assertEqual(searches, [["security", "list-keychains", "-s", "/Users/r/login.keychain-db", str(keychain)], searches[1]])
        self.assertEqual(searches[1], ["security", "list-keychains", "-s", "/Users/r/login.keychain-db"])

    def notarize(self, status):
        """notarize with the service answering status; returns the log titles of its commands."""
        titles = []

        def fake_run(args, **kwargs):
            titles.append(kwargs.get("title"))
            stdout = json.dumps({"id": "abc", "status": status}) if "submit" in [str(a) for a in args] else ""
            return mock.Mock(returncode=0, stdout=stdout)

        environ = {"APPLE_SIMPLEX_NOTARIZATION_APPLE_ID": "id@x", "APPLE_SIMPLEX_NOTARIZATION_PASSWORD": "secret"}
        with mock.patch.object(build_script, "run", fake_run), mock.patch.dict(os.environ, environ):
            build_script.notarize(self.root / "libs", self.root)
        return titles

    def test_notarization_must_be_accepted_and_hides_the_password(self):
        titles = self.notarize("Accepted")
        self.assertFalse(any(t and "secret" in t for t in titles))
        with self.assertRaises(build_script.ReproduceError):
            self.notarize("Invalid")

    def test_dmg_signing_properties_are_written_and_removed(self):
        source = self.root / "src"
        multiplatform = source / "apps/multiplatform"
        multiplatform.mkdir(parents=True)
        dmg_dir = source / build_script.DESKTOP_RELEASE_DIR / "dmg"
        seen = []

        def fake_run(args, **kwargs):
            seen.append(([str(a) for a in args], (multiplatform / "local.properties").read_text()))
            dmg_dir.mkdir(parents=True, exist_ok=True)
            (dmg_dir / "SimpleX-7.1.0.dmg").write_bytes(b"dmg")
            return mock.Mock(returncode=0, stdout="")

        environ = {"APPLE_SIMPLEX_NOTARIZATION_APPLE_ID": "id@x", "APPLE_SIMPLEX_NOTARIZATION_PASSWORD": "secret"}
        with mock.patch.object(build_script, "run", fake_run), mock.patch.dict(os.environ, environ):
            dmg = build_script.package_dmg(source, self.root / "k.keychain")
        self.assertEqual([args[-1] for args, _ in seen], ["packageDmg", "notarizeDmg"])
        self.assertIn("desktop.mac.notarization.team_id=5NN7GUYB6T\n", seen[0][1])
        self.assertIn(f"desktop.mac.signing.keychain={self.root / 'k.keychain'}\n", seen[0][1])
        self.assertFalse((multiplatform / "local.properties").exists())
        self.assertEqual(dmg.name, "SimpleX-7.1.0.dmg")

    def test_unsigned_dmg_is_only_packaged(self):
        source = self.root / "src"
        (source / "apps/multiplatform").mkdir(parents=True)
        dmg_dir = source / build_script.DESKTOP_RELEASE_DIR / "dmg"
        dmg_dir.mkdir(parents=True)
        (dmg_dir / "SimpleX-7.1.0.dmg").write_bytes(b"dmg")
        recorder = Recorder()
        with mock.patch.object(build_script, "run", recorder):
            build_script.package_dmg(source, None)
        self.assertEqual([c[-1] for c in recorder.commands], ["packageDmg"])


class WindowsLibTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.source = self.root / "src"
        (self.source / "src/Simplex/Chat").mkdir(parents=True)
        (self.source / "src/Simplex/Chat/Mobile.hs").write_text('foreign export ccall "chat_send_cmd" c :: X\n')
        (self.source / "libsimplex.dll.def").write_text("chat_send_cmd\n")
        (self.source / "flake.nix").write_text('"chat_send_cmd"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_ghc_settings_stop_using_lld(self):
        libdir = self.root / "ghc-lib"
        libdir.mkdir()
        (libdir / "settings").write_text('[("ld command", "ld.lld.exe"), ("x", "ld.lld.exe")]\n')
        with mock.patch.object(build_script, "run", Recorder(answers={"print-libdir": f"{libdir}\n"})):
            build_script.disable_ghc_lld()
        self.assertEqual((libdir / "settings").read_text(), '[("ld command", "abracadabra.exe"), ("x", "ld.lld.exe")]\n')

    def test_library_links_the_pinned_openssl(self):
        openssl = self.root / "openssl-3.5.9"
        openssl.mkdir()
        (openssl / "libcrypto-3-x64.dll").write_bytes(b"crypto")
        (self.source / "libsimplex.dll").write_bytes(b"simplex")
        recorder = Recorder()
        with mock.patch.object(build_script, "run", recorder), mock.patch.object(build_script, "disable_ghc_lld") as disable:
            libs = build_script.build_windows_lib(self.source, build_script.OpenSsl(openssl / "include", openssl))
        disable.assert_called_once()
        cabal_local = (self.source / "cabal.project.local").read_text()
        self.assertIn(f"extra-include-dirs: {openssl.as_posix()}/include", cabal_local)
        options = f"-shared -threaded -optl-L{openssl.as_posix()} -optl-lcrypto-3-x64 -o libsimplex.dll libsimplex.dll.def"
        self.assertIn(f"ghc-options: {options}", cabal_local)
        self.assertIn("simplex-chat +client_library", recorder.commands[-1])
        self.assertEqual(sorted(p.name for p in libs.iterdir()), ["libcrypto-3-x64.dll", "libsimplex.dll"])

    def test_vlc_plugins_get_the_jar_time_before_the_cache(self):
        libs, work = self.root / "libs", self.root / "work"
        libs.mkdir()
        work.mkdir()
        archive = work / "vlc-win-x86_64.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("vlc-3.0.23/libvlc.dll", b"vlc")
            z.writestr("vlc-3.0.23/plugins/audio/libplugin.dll", b"p")
        times = []

        def cache_gen(args, **kwargs):
            times.append((work / "vlc/vlc-3.0.23/plugins/audio/libplugin.dll").stat().st_mtime)
            return mock.Mock(returncode=0, stdout="")

        with mock.patch.object(build_script, "run", cache_gen), mock.patch.object(build_script, "fetch_pinned", lambda u, d, t: t):
            build_script.add_vlc_windows(libs, work)
        self.assertEqual(times, [0])
        self.assertTrue((libs / "vlc/libvlc.dll").exists())
        self.assertEqual((libs / "vlc/vlc/plugins/audio/libplugin.dll").stat().st_mtime, 0)


OTOOL = """Archive : libffi.a(closures.o)
Load command 1
      cmd LC_VERSION_MIN_IPHONEOS
  cmdsize 16
  version 11.0
      sdk 11.0
Load command 2
      cmd LC_SYMTAB
Archive : libffi.a(ffi.o)
Load command 1
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 2
    minos 14.2
      sdk 14.2
"""

ENTITLEMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>keychain-access-groups</key>
	<array>
		<string>$(AppIdentifierPrefix)chat.simplex.app</string>
	</array>
</dict>
</plist>
"""


class IosDetailsTest(unittest.TestCase):
    def test_minimum_versions_reads_both_load_commands(self):
        self.assertEqual(build_script.minimum_versions(OTOOL), ["11.0", "14.2"])
        self.assertEqual(build_script.minimum_versions("Load command 1\n      cmd LC_SYMTAB\n"), [])

    def test_versions_compare_numerically(self):
        self.assertGreater(build_script.version_tuple("15.10"), build_script.version_tuple("15.9"))
        self.assertEqual(max(["9.0", "15.0", "11.0"], key=build_script.version_tuple), "15.0")

    def test_build_settings_take_the_first_value(self):
        output = (
            "Build settings for target X:\n"
            "    DEVELOPMENT_TEAM = 5NN7GUYB6T\n    IPHONEOS_DEPLOYMENT_TARGET = 15.0\n    DEVELOPMENT_TEAM = OTHER\n"
        )
        settings = build_script.build_settings(output, ["DEVELOPMENT_TEAM", "IPHONEOS_DEPLOYMENT_TARGET"])
        self.assertEqual(settings, {"DEVELOPMENT_TEAM": "5NN7GUYB6T", "IPHONEOS_DEPLOYMENT_TARGET": "15.0"})
        with self.assertRaises(build_script.ReproduceError):
            build_script.build_settings("    DEVELOPMENT_TEAM = 5NN7GUYB6T\n", ["DEVELOPMENT_TEAM", "IPHONEOS_DEPLOYMENT_TARGET"])

    def test_entitlements_get_the_team_prefix(self):
        self.assertIn("<string>5NN7GUYB6T.chat.simplex.app</string>", build_script.resolve_entitlements(ENTITLEMENTS, "5NN7GUYB6T"))

    def test_nix_env_finds_a_certificate_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = pathlib.Path(tmp) / "ca-bundle.crt"
            cert.write_text("")
            with (
                mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
                mock.patch.object(build_script, "NIX_CERT_FILES", (pathlib.Path(tmp) / "missing", cert)),
            ):
                env = build_script.nix_env()
            self.assertEqual(env["NIX_SSL_CERT_FILE"], str(cert))
            self.assertTrue(env["PATH"].endswith(":/usr/bin"))
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(build_script, "NIX_CERT_FILES", ()),
                self.assertRaises(build_script.ReproduceError),
            ):
                build_script.nix_env()


class FakeApple:
    """Stands in for run() with Apple's tools: codesign stores and returns entitlements, otool returns OTOOL."""

    def __init__(self, tamper=False):
        self.commands = []
        self.signed = {}
        self.tamper = tamper

    def __call__(self, args, cwd=None, env=None, input=None, check=True, capture=False, title=None):
        args = [str(a) for a in args]
        self.commands.append(args)
        stdout = ""
        if args[0] == "codesign" and "--entitlements" in args and "-d" not in args:
            text = pathlib.Path(args[args.index("--entitlements") + 1]).read_text()
            self.signed[args[-1]] = text.replace("5NN7GUYB6T", "XXXX") if self.tamper else text
        elif args[0] == "codesign" and "-d" in args:
            stdout = self.signed[args[-1]]
        elif args[0] == "otool":
            stdout = OTOOL
        return mock.Mock(returncode=0, stdout=stdout)


class IosSigningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.bundle = self.dir / "SimpleX.app"
        (self.bundle / "Frameworks/SimpleXChat.framework").mkdir(parents=True)
        (self.bundle / "Frameworks/Info.plist").write_text("not code")
        self.entitlements = self.dir / "SimpleX.entitlements"
        self.entitlements.write_text(ENTITLEMENTS)

    def tearDown(self):
        self.tmp.cleanup()

    def test_signs_nested_code_first_then_the_bundle_with_resolved_entitlements(self):
        apple = FakeApple()
        with mock.patch.object(build_script, "run", apple), mock.patch("builtins.print"):
            build_script.sign_bundle(self.bundle, self.entitlements, "5NN7GUYB6T", self.dir)
        signs = [c for c in apple.commands if "--sign" in c]
        self.assertEqual([pathlib.Path(c[-1]).name for c in signs], ["SimpleXChat.framework", "SimpleX.app"])
        self.assertIn("5NN7GUYB6T.chat.simplex.app", apple.signed[str(self.bundle)])

    def test_rejects_a_signature_with_other_entitlements(self):
        with mock.patch.object(build_script, "run", FakeApple(tamper=True)), self.assertRaises(build_script.ReproduceError):
            build_script.sign_bundle(self.bundle, self.entitlements, "5NN7GUYB6T", self.dir)

    def test_rejects_a_bundle_without_entitlements(self):
        with self.assertRaises(build_script.ReproduceError):
            build_script.sign_bundle(self.bundle, self.dir / "missing.entitlements", "5NN7GUYB6T", self.dir)

    def test_minimum_version_guard(self):
        libs = self.dir / "apps/ios/Libraries/ios"
        libs.mkdir(parents=True)
        (libs / "libffi.a").write_bytes(b"")
        with mock.patch.object(build_script, "run", FakeApple()), mock.patch("builtins.print"):
            build_script.check_minimum_versions(self.dir, "15.0")
            with self.assertRaises(build_script.ReproduceError):
                build_script.check_minimum_versions(self.dir, "14.0")


class IosArchiveTest(unittest.TestCase):
    def test_build_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source, art, out = root / "src", root / "art", root / "out"
            (art / "ios/Assets.xcassets/logo.imageset").mkdir(parents=True)
            for name in ("SimpleX NSE", "SimpleX SE"):
                (source / f"apps/ios/{name}").mkdir(parents=True)
                (source / f"apps/ios/{name}/{name}.entitlements").write_text(ENTITLEMENTS)
            (source / "apps/ios/SimpleX (iOS).entitlements").write_text(ENTITLEMENTS)
            (source / "apps/ios/Shared/SimpleXAssets.xcassets").mkdir(parents=True)
            (source / "apps/ios/Libraries/ios").mkdir(parents=True)
            (source / "apps/ios/Libraries/ios/libHSsimplex-chat.a").write_bytes(b"")
            out.mkdir()
            apple = FakeApple()

            def fake_run(args, cwd=None, env=None, input=None, check=True, capture=False, title=None):
                args = [str(a) for a in args]
                if args[:2] == ["nix", "build"]:
                    self.assertIn("NIX_SSL_CERT_FILE", env)
                    link = pathlib.Path(args[args.index("--out-link") + 1])
                    link.mkdir()
                    (link / build_script.IOS_CORE_ZIP).write_bytes(b"core")
                elif args[0] == "scripts/ios/copy-assets.sh":
                    (source / "apps/ios/Shared/SimpleXAssets.xcassets/logo.imageset").mkdir()
                elif args[:2] == ["xcodebuild", "-showBuildSettings"]:
                    apple.commands.append(args)
                    return mock.Mock(returncode=0, stdout="    DEVELOPMENT_TEAM = 5NN7GUYB6T\n    IPHONEOS_DEPLOYMENT_TARGET = 15.0\n")
                elif args[:2] == ["xcodebuild", "archive"]:
                    app = pathlib.Path(args[args.index("-archivePath") + 1]) / "Products/Applications/SimpleX.app"
                    for appex in ("SimpleX NSE", "SimpleX SE"):
                        (app / f"PlugIns/{appex}.appex").mkdir(parents=True)
                elif args[0] == "ditto":
                    pathlib.Path(args[-1]).write_bytes(b"archive")
                return apple(args, cwd, env, input, check, capture)

            with (
                mock.patch.object(build_script, "run", fake_run),
                mock.patch("builtins.print"),
                mock.patch.object(build_script, "nix_env", return_value={"NIX_SSL_CERT_FILE": "/c"}),
                mock.patch.object(build_script, "install_ios_libs") as install_libs,
            ):
                build_script.build_ios_archive(source, art, out, install=False)

            installed = install_libs.call_args.args[1]
            self.assertEqual(installed.name, build_script.IOS_CORE_ZIP)
            xcconfig = (source / "apps/ios/Local.xcconfig").read_text()
            self.assertIn("SWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) SIMPLEX_ASSETS", xcconfig)
            self.assertIn(f"SIMPLEX_ASSETS_DIR = {art}", xcconfig)
            self.assertEqual(
                [c[0] for c in apple.commands][:5],
                ["nix", "scripts/ios/copy-assets.sh", "xcodebuild", "otool", "xcodebuild"],
            )
            archive = next(c for c in apple.commands if c[:2] == ["xcodebuild", "archive"])
            self.assertIn("CODE_SIGNING_ALLOWED=NO", archive)
            signed = [c[-1] for c in apple.commands if c[0] == "codesign" and "--entitlements" in c and "-d" not in c]
            self.assertEqual([pathlib.Path(p).name for p in signed], ["SimpleX NSE.appex", "SimpleX SE.appex", "SimpleX.app"])
            self.assertIn(["plutil", "-replace", "ApplicationProperties.Team", "-string", "5NN7GUYB6T"], [c[:5] for c in apple.commands])
            self.assertEqual((out / "SimpleX.xcarchive.zip").read_bytes(), b"archive")

    def test_needs_the_art_assets(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(build_script.ReproduceError):
            build_script.build_ios_archive(pathlib.Path(tmp), pathlib.Path(tmp) / "art", pathlib.Path(tmp), install=False)


PBXPROJ = """
    A1 /* libHSsimplex-chat-6.5.0.0-Old.a */ = {isa = PBXBuildFile; fileRef = B1 /* libHSsimplex-chat-6.5.0.0-Old.a */; };
    A2 /* libHSsimplex-chat-6.5.0.0-Old-ghc9.6.3.a */ = {isa = PBXBuildFile; fileRef = B2 /* libHSsimplex-chat-6.5.0.0-Old-ghc9.6.3.a */; };
    B1 /* libHSsimplex-chat-6.5.0.0-Old.a */ = {path = "libHSsimplex-chat-6.5.0.0-Old.a"; };
    B2 /* libHSsimplex-chat-6.5.0.0-Old-ghc9.6.3.a */ = {path = "libHSsimplex-chat-6.5.0.0-Old-ghc9.6.3.a"; };
    C1 /* libHSsimplex-chat-6.5.0.0-Old.a in Frameworks */,
    C2 /* libHSsimplex-chat-6.5.0.0-Old-ghc9.6.3.a in Frameworks */,
    D1 /* libHSsimplex-chat-6.5.0.0-Old.a */,
    D2 /* libHSsimplex-chat-6.5.0.0-Old-ghc9.6.3.a */,
"""


class IosLibsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.source = self.root / "src"
        (self.source / "apps/ios/SimpleX.xcodeproj").mkdir(parents=True)
        (self.source / build_script.IOS_PBXPROJ).write_text(PBXPROJ)

    def tearDown(self):
        self.tmp.cleanup()

    def install_libraries(self, *names):
        ios = self.source / build_script.IOS_LIBRARIES / "ios"
        ios.mkdir(parents=True, exist_ok=True)
        for name in names:
            (ios / name).write_bytes(b"")

    def test_pbxproj_points_at_the_installed_libraries(self):
        self.install_libraries("libHSsimplex-chat-6.5.1.0-New.a", "libHSsimplex-chat-6.5.1.0-New-ghc9.6.3.a", "libffi.a")
        with mock.patch.object(build_script, "log"):
            build_script.update_pbxproj(self.source)
        text = (self.source / build_script.IOS_PBXPROJ).read_text()
        self.assertNotIn("Old", text)
        self.assertEqual(text.count("libHSsimplex-chat-6.5.1.0-New-ghc9.6.3.a"), 6)
        self.assertEqual(text.count("libHSsimplex-chat-6.5.1.0-New.a"), 6)

    def test_pbxproj_without_four_references_each_is_not_changed(self):
        (self.source / build_script.IOS_PBXPROJ).write_text(PBXPROJ.replace("    D1 /* libHSsimplex-chat-6.5.0.0-Old.a */,\n", ""))
        self.install_libraries("libHSsimplex-chat-6.5.1.0-New.a", "libHSsimplex-chat-6.5.1.0-New-ghc9.6.3.a")
        with self.assertRaises(build_script.ReproduceError):
            build_script.update_pbxproj(self.source)
        self.assertIn("Old", (self.source / build_script.IOS_PBXPROJ).read_text())

    def test_pbxproj_needs_both_libraries(self):
        self.install_libraries("libHSsimplex-chat-6.5.1.0-New.a")
        with self.assertRaises(build_script.ReproduceError):
            build_script.update_pbxproj(self.source)

    def test_simulator_libraries_must_be_marked_for_the_simulator(self):
        sim = self.root / "sim"
        sim.mkdir()
        (sim / "libffi.a").write_bytes(b"")
        for platform_line, rejected in (("platform 7", False), ("platform IOSSIMULATOR", False), ("platform 2", True)):
            with mock.patch.object(build_script, "run", Recorder(answers={"otool": f"Load command 1\n {platform_line}\n"})):
                if rejected:
                    with self.assertRaises(build_script.ReproduceError):
                        build_script.check_simulator_libs(sim)
                else:
                    build_script.check_simulator_libs(sim)

    def test_install_relabels_copies_for_ios_and_the_simulator(self):
        package = self.root / build_script.IOS_CORE_ZIP
        with zipfile.ZipFile(package, "w") as z:
            z.writestr("libHSsimplex-chat-6.5.1.0-New.a", b"a")
            z.writestr("libHSsimplex-chat-6.5.1.0-New-ghc9.6.3.a", b"g")
        recorder = Recorder(answers={"otool": " platform 7\n"})
        work = self.root / "work"
        work.mkdir()
        with (
            mock.patch.object(build_script, "run", recorder),
            mock.patch.object(build_script, "fetch_pinned", lambda url, digest, target: target),
            mock.patch.object(build_script, "log"),
        ):
            build_script.install_ios_libs(self.source, package, work)
        libraries = self.source / build_script.IOS_LIBRARIES
        for name in ("mac", "ios", "sim"):
            self.assertEqual(len(list((libraries / name).iterdir())), 2)
        relabels = [c for c in recorder.commands if c[0].endswith("mac2ios")]
        self.assertEqual(sum(c[1] == "-s" for c in relabels), 2)
        self.assertEqual(len(relabels), 4)
        self.assertIn("libHSsimplex-chat-6.5.1.0-New.a", (self.source / build_script.IOS_PBXPROJ).read_text())

    def test_simulator_package_has_the_libraries_of_the_nix_package(self):
        source = self.root / "src"
        (source / "simplex-chat.cabal").write_text("name: simplex-chat\nversion:        6.5.1.0\n")
        build = source / "dist-newstyle/build/aarch64-osx/ghc-9.6.3/simplex-chat-6.5.1.0/build"
        build.mkdir(parents=True)
        (build / "libHSsimplex-chat-6.5.1.0-New-ghc9.6.3.a").write_bytes(b"lib")
        brew = self.root / "brew"
        (brew / "lib").mkdir(parents=True)
        for name in ("libffi.a", "libgmp.a", "libgmpxx.a"):
            (brew / "lib" / name).write_bytes(b"x")
        recorder = Recorder(answers={"numeric-version": "9.6.3\n", "brew --prefix": f"{brew}\n"})
        openssl = build_script.OpenSsl(self.root / "o/include", self.root / "o/lib")
        with mock.patch.object(build_script, "run", recorder), mock.patch.object(build_script, "host_openssl", return_value=openssl):
            build_script.build_ios_sim_libs(source, self.root)
        self.assertIn(f"extra-lib-dirs: {self.root / 'o/lib'}", (source / "cabal.project.local").read_text())
        self.assertIn(["cabal", "build", "-j", "lib:simplex-chat"], recorder.commands)
        with zipfile.ZipFile(self.root / build_script.IOS_CORE_ZIP) as z:
            self.assertEqual(sorted(z.namelist()), ["libHSsimplex-chat-6.5.1.0-New-ghc9.6.3.a", "libffi.a", "libgmp.a", "libgmpxx.a"])


class PrebuiltLibsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_android_libs_are_built_with_nix_on_this_machine(self):
        def fake_run(args, **kwargs):
            args = [str(a) for a in args]
            link = pathlib.Path(args[args.index("--out-link") + 1])
            link.mkdir()
            lib = "libsimplex" if "lib:simplex-chat" in args[-1] else "libsupport"
            (link / f"pkg-aarch64-android-{lib}.zip").write_bytes(lib.encode())
            return recorder(args, **kwargs)

        recorder = Recorder()
        with mock.patch.object(build_script, "run", fake_run), mock.patch.object(build_script, "install_nix") as install:
            build_script.build_android_libs(self.root, Arch.AARCH64, self.root, install=False)
        install.assert_not_called()
        self.assertEqual(
            [c[-1] for c in recorder.commands],
            [".#hydraJobs.x86_64-linux.aarch64-android:lib:simplex-chat", ".#hydraJobs.x86_64-linux.aarch64-android:lib:support"],
        )
        self.assertEqual(
            sorted(p.name for p in self.root.glob("*.zip")), ["pkg-aarch64-android-libsimplex.zip", "pkg-aarch64-android-libsupport.zip"]
        )

    def test_android_libs_are_installed_for_their_abi(self):
        for lib in build_script.ANDROID_LIBS:
            with zipfile.ZipFile(self.root / f"pkg-aarch64-android-{lib}.zip", "w") as z:
                z.writestr(f"{lib}.so", lib)
        build_script.install_android_libs(self.root, Arch.AARCH64, self.root)
        libs = self.root / build_script.ANDROID_LIBS_DIR / "arm64-v8a"
        self.assertEqual(sorted(p.name for p in libs.iterdir()), ["libsimplex.so", "libsupport.so"])

    def test_linux_libs_are_installed_and_linked(self):
        package = self.root / "simplex-chat-libs-linux-x86_64.zip"
        with zipfile.ZipFile(package, "w") as z:
            z.writestr("libs/libsimplex.so", "lib")
            z.writestr("other/readme", "x")
        with mock.patch.object(build_script.platform, "machine", return_value="x86_64"):
            build_script.install_linux_libs(self.root, package)
        libs = self.root / build_script.DESKTOP_LIBS_DIR / "linux-x86_64"
        self.assertEqual([p.name for p in libs.iterdir()], ["libsimplex.so"])
        self.assertTrue((self.root / "apps/multiplatform/build/links/linux-x64").is_symlink())

    def test_install_commands(self):
        here = pathlib.Path(".")
        self.assertEqual(
            build_script.parse_args(["install", "libs", "android", "core", "--arch", "armv7a"]),
            build_script.InstallLibs("android", pathlib.Path("core"), Arch.ARMV7A, here),
        )
        self.assertEqual(
            build_script.parse_args(["install", "libs", "ios", "pkg.zip"]),
            build_script.InstallLibs("ios", pathlib.Path("pkg.zip"), Arch.AARCH64, here),
        )
        self.assertEqual(
            build_script.parse_args(["build", "libs", "android", "--install-nix", "--out", "o"]),
            build_script.BuildAndroidLibs(Arch.AARCH64, pathlib.Path("o"), here, True),
        )
        self.assertEqual(
            build_script.parse_args(["build", "libs", "ios-sim", "--out", "o"]), build_script.BuildIosSimLibs(pathlib.Path("o"), here)
        )


class GitHubLogTest(unittest.TestCase):
    def output(self, environ, action):
        with (
            mock.patch.dict(os.environ, environ, clear=True),
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            action()
        return out.getvalue(), err.getvalue()

    def test_warn_annotates_on_github_actions(self):
        out, err = self.output(GITHUB, lambda: build_script.warn("unsigned build"))
        self.assertEqual((out, err), ("::warning::unsigned build\n", ""))

    def test_warn_prints_to_stderr_elsewhere(self):
        out, err = self.output({}, lambda: build_script.warn("unsigned build"))
        self.assertEqual(out, "")
        self.assertIn("warning: unsigned build", err)

    def test_error_escapes_newlines_and_percent(self):
        out, _ = self.output(GITHUB, lambda: build_script.error("100% failed\nat step 2"))
        self.assertEqual(out, "::error::100%25 failed%0Aat step 2\n")

    def test_group_is_closed_when_its_command_fails(self):
        def failing():
            with build_script.log_group("cabal build"):
                raise build_script.ReproduceError("failed")

        out, _ = self.output(GITHUB, lambda: self.assertRaises(build_script.ReproduceError, failing))
        self.assertEqual(out, "::group::cabal build\n::endgroup::\n")

    def test_run_groups_shown_output_only(self):
        def commands():
            build_script.run([sys.executable, "-c", "print('built')"])
            build_script.run([sys.executable, "-c", "print('hidden')"], capture=True, title="never shown")

        with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0, stdout="")):
            out, _ = self.output(GITHUB, commands)
        self.assertEqual(out, f"::group::{sys.executable} -c print('built')\n::endgroup::\n")

    def test_container_steps_are_titled_with_their_command(self):
        recorder = Recorder()
        container = build_script.Container(pathlib.Path("."), {}, {}, [])
        with mock.patch.object(build_script, "run", wraps=recorder) as run:
            container.run(build_script.Step(("cabal", "update"), env={"PATH": "/x"}))
        self.assertEqual(run.call_args.kwargs["title"], "cabal update")

    def test_summary_lists_files_on_github_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "simplex-aarch64.apk").write_bytes(b"apk")
            summary = d / "summary.md"
            self.output({**GITHUB, "GITHUB_STEP_SUMMARY": str(summary)}, lambda: build_script.write_sha256sums(d))
            text = summary.read_text()
        self.assertIn("| `simplex-aarch64.apk` | 3 | `", text)
        self.assertNotIn("SHA256SUMS`", text)

    def test_summary_is_skipped_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = pathlib.Path(tmp) / "summary.md"
            self.output({"GITHUB_STEP_SUMMARY": str(summary)}, lambda: build_script.add_summary("text"))
            self.assertFalse(summary.exists())


class MainTest(unittest.TestCase):
    def test_errors_exit_with_1(self):
        with (
            mock.patch.object(build_script, "reproduce", side_effect=build_script.ReproduceError("boom")),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            self.assertEqual(build_script.main(["reproduce", "v7.1.0"]), 1)
        self.assertIn("error: boom", err.getvalue())

    def test_build_commands_go_to_build(self):
        with mock.patch.object(build_script, "build") as build:
            self.assertEqual(build_script.main(["build", "aab", "--libs", "l", "--out", "o"]), 0)
        self.assertIsInstance(build.call_args.args[0], build_script.BuildBundle)

    def test_missing_tools_are_named(self):
        with mock.patch.object(build_script.shutil, "which", return_value=None), self.assertRaises(build_script.ReproduceError) as e:
            build_script.require_tools(["git", "docker"])
        self.assertIn("git, docker", str(e.exception))

    def test_unreachable_docker_daemon(self):
        with (
            mock.patch.object(build_script.shutil, "which", return_value="/usr/bin/docker"),
            mock.patch.object(build_script.subprocess, "run", return_value=mock.Mock(returncode=1)),
            self.assertRaises(build_script.ReproduceError) as e,
        ):
            build_script.require_tools(["docker"])
        self.assertIn("daemon", str(e.exception))

    def test_ios_needs_nix_unless_installing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_script.BuildIosArchive(pathlib.Path("a"), pathlib.Path(tmp), pathlib.Path("."), False)
            with (
                mock.patch.object(build_script, "require_tools"),
                mock.patch.object(build_script, "host_platform", return_value=build_script.Platform.MACOS),
                mock.patch.object(build_script, "NIX_PROFILE", pathlib.Path("/nonexistent")),
                self.assertRaises(build_script.ReproduceError) as e,
            ):
                build_script.build(command)
        self.assertIn("--install-nix", str(e.exception))


if __name__ == "__main__":
    unittest.main()
