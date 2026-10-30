#!/usr/bin/env python3

import importlib.util
import io
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "reproduce", pathlib.Path(__file__).with_name("simplex-chat-reproduce-builds.py")
)
reproduce = importlib.util.module_from_spec(spec)
# dataclasses look the module up by name
sys.modules[spec.name] = reproduce
spec.loader.exec_module(reproduce)

Arch, Step, Target = reproduce.Arch, reproduce.Step, reproduce.Target
JAMMY, NOBLE = reproduce.DESKTOP_IMAGES
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
    for entry in reproduce.central_directory(bytes(data)):
        packed = struct.pack("<H", flags[entry.name])
        data[entry.central_offset + 8 : entry.central_offset + 10] = packed
        data[entry.local_offset + 6 : entry.local_offset + 8] = packed
    return bytes(data)


def local_flags(data, name):
    entry = next(e for e in reproduce.central_directory(data) if e.name == name)
    return struct.unpack("<H", data[entry.local_offset + 6 : entry.local_offset + 8])[0]


def central_flags(data, name):
    return next(e.flags for e in reproduce.central_directory(data) if e.name == name)


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
                    raise reproduce.ReproduceError(f"failed: {joined}")
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
        self.assertEqual(reproduce.apk_padding(apk), "apksigner")

    def test_zip_without_the_extra_field_has_zero_padding(self):
        apk = make_zip([("classes.dex", b"dex", 0, b""), ("res/a.png", b"png", 0, b"\x00\x00")])
        self.assertEqual(reproduce.apk_padding(apk), "zero")

    def test_non_zip_is_rejected(self):
        with self.assertRaises(reproduce.ReproduceError):
            reproduce.apk_padding(b"not a zip")

    def test_v1_signature_files_get_the_flags_in_both_headers(self):
        patched = reproduce.copy_v1_flags(SIGNED, COPIED)
        for name in ("META-INF/KEY0.SF", "META-INF/KEY0.RSA", "META-INF/MANIFEST.MF"):
            self.assertEqual(local_flags(patched, name), UTF8_FLAG, name)
            self.assertEqual(central_flags(patched, name), UTF8_FLAG, name)

    def test_other_entries_keep_their_flags(self):
        patched = reproduce.copy_v1_flags(SIGNED, COPIED)
        for name in ("classes.dex", "META-INF/versions/9/OSGI-INF/MANIFEST.MF"):
            self.assertEqual(local_flags(patched, name), 0, name)
            self.assertEqual(central_flags(patched, name), 0, name)

    def test_patched_zip_matches_the_signed_one_byte_for_byte(self):
        self.assertNotEqual(COPIED, SIGNED)
        self.assertEqual(reproduce.copy_v1_flags(SIGNED, COPIED), SIGNED)

    def test_patched_zip_still_reads(self):
        with zipfile.ZipFile(io.BytesIO(reproduce.copy_v1_flags(SIGNED, COPIED))) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.read("META-INF/KEY0.SF"), b"sf")

    def test_v1_signature_file_names(self):
        self.assertTrue(reproduce.is_v1_signature_file("META-INF/MANIFEST.MF"))
        self.assertTrue(reproduce.is_v1_signature_file("META-INF/KEY0.SF"))
        self.assertTrue(reproduce.is_v1_signature_file("META-INF/CERT.EC"))
        self.assertFalse(reproduce.is_v1_signature_file("META-INF/versions/9/OSGI-INF/MANIFEST.MF"))
        self.assertFalse(reproduce.is_v1_signature_file("META-INF/androidx.core_core.version"))
        self.assertFalse(reproduce.is_v1_signature_file("assets/KEY0.SF"))


class ReleaseDetailsTest(unittest.TestCase):
    def test_tag_version(self):
        self.assertEqual(reproduce.tag_version("v7.1.0-beta.7"), "7.1.0")
        self.assertEqual(reproduce.tag_version("v6.4.8"), "6.4.8")

    def test_android_build_tag(self):
        tags = ["v6.4.0", "v6.4.0-armv7a", "v7.1.0"]
        self.assertEqual(reproduce.android_build_tag("v6.4.0", tags, Arch.ARMV7A), "v6.4.0-armv7a")
        self.assertEqual(reproduce.android_build_tag("v6.4.0", tags, Arch.AARCH64), "v6.4.0")
        self.assertEqual(reproduce.android_build_tag("v7.1.0", tags, Arch.ARMV7A), "v7.1.0")

    def test_arches(self):
        self.assertEqual([a.abi for a in Arch], ["arm64-v8a", "armeabi-v7a"])
        self.assertEqual(f"simplex-{Arch.ARMV7A}.apk", "simplex-armv7a.apk")

    def test_android_vercode(self):
        with tempfile.TemporaryDirectory() as tmp:
            props = pathlib.Path(tmp) / "apps/multiplatform/gradle.properties"
            props.parent.mkdir(parents=True)
            props.write_text("android.version_name=7.1-beta.7\nandroid.version_code=387\n")
            self.assertEqual(reproduce.android_vercode(pathlib.Path(tmp)), "387")
            props.write_text("android.version_name=7.1\n")
            with self.assertRaises(reproduce.ReproduceError):
                reproduce.android_vercode(pathlib.Path(tmp))


class FilesTest(unittest.TestCase):
    def test_sha256sums_list_every_file_but_the_sums(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "b.apk").write_bytes(b"b")
            (d / "a.zip").write_bytes(b"a")
            (d / "SHA256SUMS").write_text("old")
            with mock.patch("builtins.print"):
                reproduce.write_sha256sums(d)
            self.assertEqual(
                (d / "SHA256SUMS").read_text(),
                f"{reproduce.sha256(d / 'a.zip')}  a.zip\n{reproduce.sha256(d / 'b.apk')}  b.apk\n",
            )

    def test_compare_dirs_reports_differing_and_missing_files(self):
        with tempfile.TemporaryDirectory() as built, tempfile.TemporaryDirectory() as released:
            for name, data in (("same", b"1"), ("differs", b"2"), ("missing", b"3")):
                (pathlib.Path(built) / name).write_bytes(data)
            (pathlib.Path(released) / "same").write_bytes(b"1")
            (pathlib.Path(released) / "differs").write_bytes(b"x")
            self.assertEqual(reproduce.compare_dirs(pathlib.Path(built), pathlib.Path(released)), ["differs", "missing"])

    def test_permissions_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "dir").mkdir(mode=0o700)
            (root / "dir/script.sh").write_text("")
            (root / "dir/script.sh").chmod(0o700)
            (root / "dir/data").write_text("")
            (root / "dir/data").chmod(0o600)
            (root / "link").symlink_to("dir/data")
            reproduce.normalize_permissions(root)
            modes = [(root / p).stat().st_mode & 0o777 for p in ("dir", "dir/script.sh", "dir/data")]
            self.assertEqual(modes, [0o755, 0o755, 0o644])

    def test_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            with self.assertRaises(reproduce.ReproduceError):
                reproduce.single_file(d, "*.aab")
            (d / "a.aab").write_bytes(b"")
            self.assertEqual(reproduce.single_file(d, "*.aab"), d / "a.aab")
            (d / "b.aab").write_bytes(b"")
            with self.assertRaises(reproduce.ReproduceError):
                reproduce.single_file(d, "*.aab")


class ParseArgsTest(unittest.TestCase):
    def test_reproduce_defaults_to_desktop_and_both_arches(self):
        self.assertEqual(
            reproduce.parse_args(["v7.1.0"]),
            reproduce.Reproduce("v7.1.0", (Target.DESKTOP,), (Arch.AARCH64, Arch.ARMV7A), False),
        )

    def test_reproduce_android_with_options(self):
        self.assertEqual(
            reproduce.parse_args(["v7.1.0", "android", "--arches", "armv7a", "--nix-cache"]),
            reproduce.Reproduce("v7.1.0", (Target.ANDROID,), (Arch.ARMV7A,), True),
        )

    def test_reproduce_rejects_unknown_arch_and_target(self):
        for argv in (["v7.1.0", "android", "--arches", "x86"], ["v7.1.0", "ios"]):
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                reproduce.parse_args(argv)

    def test_build_commands(self):
        out, here = pathlib.Path("o"), pathlib.Path(".")
        self.assertEqual(
            reproduce.parse_args(["build", "android-apk", "armv7a", "--out", "o", "--nix-cache"]),
            reproduce.BuildApk(Arch.ARMV7A, out, here, True),
        )
        self.assertEqual(
            reproduce.parse_args(["build", "android-bundle", "--libs", "l", "--out", "o", "--source", "s"]),
            reproduce.BuildBundle(pathlib.Path("l"), out, pathlib.Path("s")),
        )
        self.assertEqual(
            reproduce.parse_args(["build", "desktop-linux", "24.04", "--version", "7.1.0", "--out", "o"]),
            reproduce.BuildDesktopLinux(NOBLE, "7.1.0", out, here),
        )
        self.assertEqual(
            reproduce.parse_args(["build", "ios-archive", "--art", "a", "--out", "o", "--install-nix"]),
            reproduce.BuildIosArchive(pathlib.Path("a"), out, here, True),
        )

    def test_build_commands_reject_missing_or_unknown_arguments(self):
        for argv in (
            ["build", "android-bundle", "--out", "o"],
            ["build", "desktop-linux", "20.04", "--version", "7.1.0", "--out", "o"],
            ["build", "desktop-linux", "22.04", "--out", "o"],
            ["build", "android-apk", "x86", "--out", "o"],
        ):
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                reproduce.parse_args(argv)


class ContainerTest(unittest.TestCase):
    def test_lifecycle_and_steps(self):
        recorder = Recorder(answers={"printenv": "/home/builder\n"})
        src = pathlib.Path("/src")
        with (
            mock.patch.object(reproduce, "run", recorder),
            reproduce.Container(src, {src: "/project"}, {"TAG": "22.04"}, ["--cap-add", "SYS_ADMIN"]) as c,
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
            mock.patch.object(reproduce, "run", recorder),
            self.assertRaises(reproduce.ReproduceError),
            reproduce.Container(pathlib.Path("/src"), {}),
        ):
            pass
        self.assertEqual(recorder.commands[-1][:3], ["docker", "image", "rm"])

    def test_copy_out_follows_symlinks(self):
        recorder = Recorder()
        with mock.patch.object(reproduce, "run", recorder):
            reproduce.Container(pathlib.Path("/src"), {}).copy_out("/project/result/x.zip", pathlib.Path("/out"))
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
        return reproduce.Container(self.source, {})

    def test_armv7a_container_sets_pid_max_in_its_namespace(self):
        recorder = Recorder()
        with mock.patch.object(reproduce, "run", recorder), reproduce.android_container(self.source, self.out, set_pid_max=True):
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
        with mock.patch.object(reproduce, "run", recorder), mock.patch.object(reproduce, "warn") as warn:
            reproduce.build_apk(self.container(), self.source, Arch.ARMV7A, self.out, "simplex-armv7a.apk", True, True)
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
        with mock.patch.object(reproduce, "run", recorder):
            reproduce.build_apk(self.container(), self.source, Arch.AARCH64, self.out, "x.apk", False, False)
        build = next(c for c in recorder.commands if "./scripts/android/build-android.sh" in c)
        self.assertFalse(any(a.startswith("NIX_CONFIG") for a in build))
        self.assertFalse(any(c[:2] == ["docker", "cp"] for c in recorder.commands))

    def test_missing_gradle_distribution(self):
        with mock.patch.object(reproduce, "run", Recorder()), self.assertRaises(reproduce.ReproduceError):
            reproduce.build_apk(self.container(), self.source, Arch.AARCH64, self.out, "x.apk", False, False)

    def test_bundle_extracts_both_arches_with_release_properties(self):
        libs = self.root / "libs"
        for arch in Arch:
            directory = libs / f"android-{arch}-unsigned"
            directory.mkdir(parents=True)
            for lib in reproduce.ANDROID_LIBS:
                with zipfile.ZipFile(directory / f"pkg-{arch}-android-{lib}.zip", "w") as z:
                    z.writestr(f"{lib}.so", f"{arch} {lib}")
        bundle_dir = self.source / reproduce.ANDROID_BUNDLE_DIR
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "android-google-release.aab").write_bytes(b"aab")
        recorder = Recorder()
        with mock.patch.object(reproduce, "run", recorder):
            reproduce.build_bundle(self.container(), self.source, libs, self.out)
        extracted = self.source / reproduce.ANDROID_LIBS_DIR / "armeabi-v7a/libsupport.so"
        self.assertEqual(extracted.read_text(), "armv7a libsupport")
        self.assertEqual(recorder.steps(), [["./gradlew", "--no-daemon", ":android:bundleGoogleRelease"]])
        self.assertEqual(
            (self.source / "apps/multiplatform/local.properties").read_text(),
            "compression.level=5\nsimplex.assets.dir=../../assets\n",
        )
        self.assertEqual((self.out / "simplex.aab").read_bytes(), b"aab")

    def test_bundle_fails_without_a_library(self):
        with self.assertRaises(reproduce.ReproduceError):
            reproduce.build_bundle(self.container(), self.source, self.root / "none", self.out)

    def test_release_signer(self):
        recorder = Recorder(answers={"apksigner": "Signer #1 certificate SHA-256 digest: 3c52c4fd\n"})
        with mock.patch.object(reproduce, "run", recorder):
            self.assertEqual(reproduce.release_signer(self.container(), "simplex-aarch64.apk"), "3C:52:C4:FD")


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
        self.assertIsNone(reproduce.AndroidSigning.from_env({}))

    def test_partial_environment_names_what_is_missing(self):
        environ = dict(self.environ)
        del environ["ANDROID_KEY_PASSWORD"]
        with self.assertRaises(reproduce.ReproduceError) as raised:
            reproduce.AndroidSigning.from_env(environ)
        self.assertIn("missing: ANDROID_KEY_PASSWORD", str(raised.exception))

    def test_missing_keystore_file(self):
        with self.assertRaises(reproduce.ReproduceError):
            reproduce.AndroidSigning.from_env({**self.environ, "ANDROID_KEYSTORE": str(self.keystore) + ".gone"})

    def test_passwords_are_secrets_kept_out_of_env_and_repr(self):
        signing = reproduce.AndroidSigning.from_env(self.environ)
        self.assertEqual(signing.env, {"ANDROID_KEYSTORE": "/keystore/release.keystore", "ANDROID_KEY_ALIAS": "simplex"})
        self.assertEqual(signing.secrets, {"ANDROID_KEYSTORE_PASSWORD": "store-secret", "ANDROID_KEY_PASSWORD": "key-secret"})
        self.assertNotIn("secret", repr(signing))

    def test_gradle_reads_the_same_variables(self):
        gradle = (pathlib.Path(reproduce.__file__).parents[1] / "apps/multiplatform/android/build.gradle.kts").read_text()
        names = ", ".join(f'"{name}"' for name in reproduce.ANDROID_SIGNING_ENV)
        self.assertIn(f"val releaseSigningEnv = listOf({names})", gradle)

    def test_container_mounts_the_keystore_read_only(self):
        recorder = Recorder()
        signing = reproduce.AndroidSigning.from_env(self.environ)
        root = self.keystore.parent
        with mock.patch.object(reproduce, "run", recorder), reproduce.android_container(root, root, False, signing):
            pass
        start = next(c for c in recorder.commands if c[1] == "run")
        self.assertIn(f"{self.keystore.resolve()}:/keystore/release.keystore:ro", start)

    def test_apk_build_passes_passwords_by_name_only(self):
        source = pathlib.Path(self.tmp.name)
        (source / "apps/multiplatform").mkdir(parents=True)
        (source / "apps/multiplatform/gradle.properties").write_text("android.version_code=387\n")
        (source / "simplex-chat-arm64-v8a.apk").write_bytes(b"signed apk")
        recorder = Recorder(answers={"find": "/g/bin/gradle\n"})
        signing = reproduce.AndroidSigning.from_env(self.environ)
        with mock.patch.object(reproduce, "run", recorder):
            reproduce.build_apk(reproduce.Container(source, {}), source, Arch.AARCH64, source, "x.apk", False, False, signing)
        index = next(i for i, c in enumerate(recorder.commands) if "./scripts/android/build-android.sh" in c)
        build = recorder.commands[index]
        self.assertIn("ANDROID_KEYSTORE=/keystore/release.keystore", build)
        self.assertIn("ANDROID_KEY_ALIAS=simplex", build)
        self.assertEqual(build[build.index("ANDROID_KEY_PASSWORD") - 1], "-e")
        self.assertFalse(any("secret" in a for a in build))
        self.assertEqual(recorder.envs[index], signing.secrets)

    def test_unsigned_build_warns(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(reproduce, "warn") as warn:
            self.assertIsNone(reproduce.android_signing())
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

            with mock.patch.object(reproduce, "run", fake_run):
                result = reproduce.verify_apk(reproduce.Container(out, {}), out, "rel.apk", "built.apk")
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


class DesktopTest(unittest.TestCase):
    def test_files_of_each_image(self):
        self.assertEqual(
            reproduce.desktop_files(JAMMY),
            [
                "simplex-chat-ubuntu-22_04-x86_64",
                "simplex-chat-ubuntu-22_04-x86_64.deb",
                "simplex-desktop-ubuntu-22_04-x86_64.deb",
                "simplex-desktop-x86_64.AppImage",
            ],
        )
        self.assertNotIn("simplex-desktop-x86_64.AppImage", reproduce.desktop_files(NOBLE))

    def test_steps_of_each_image(self):
        steps = reproduce.desktop_steps(JAMMY, "7.1.0")
        self.assertEqual(
            [s.argv[:2] for s in steps],
            [
                ("./scripts/desktop/build-cli-deb.sh", "7.1.0"),
                ("./scripts/desktop/make-deb-linux.sh",),
                ("perl", "Configure"),
                ("make", f"-j{os.cpu_count()}"),
                ("./scripts/desktop/make-appimage-linux.sh",),
            ],
        )
        self.assertEqual(len(reproduce.desktop_steps(NOBLE, "7.1.0")), 2)
        self.assertEqual(steps[1].env, {"ASSETS_DIR": "../../assets"})

    def test_appimage_bundles_the_pinned_openssl(self):
        configure, build, appimage = reproduce.desktop_steps(JAMMY, "7.1.0")[2:]
        self.assertEqual(configure.workdir, f"/out/openssl-{reproduce.OPENSSL_VERSION}")
        self.assertIn("--openssldir=/etc/ssl", configure.argv)
        self.assertIn("--prefix=/usr/local", configure.argv)
        self.assertEqual(build.env, {"SOURCE_DATE_EPOCH": "1704067200"})
        self.assertEqual(appimage.env["LIBCRYPTO"], f"/out/openssl-{reproduce.OPENSSL_VERSION}/libcrypto.so.3")

    def test_build_runs_the_steps_and_copies_the_released_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, out = pathlib.Path(tmp) / "src", pathlib.Path(tmp) / "out"
            release = source / reproduce.DESKTOP_RELEASE_DIR
            (release / "deb").mkdir(parents=True)
            (release / "deb/simplex_x86_64.deb").write_bytes(b"desktop deb")
            (release / "SimpleX_Chat-x86_64.AppImage").write_bytes(b"appimage")
            out.mkdir()
            recorder = Recorder(answers={"list-bin": "/project/dist-newstyle/build/simplex-chat\n"})

            def fake_run(args, **kwargs):
                args = [str(a) for a in args]
                if args[:2] == ["docker", "run"]:
                    work = next(pathlib.Path(a.split(":")[0]) for a in args if a.endswith(":/out"))
                    (work / "deb-build").mkdir()
                    (work / "simplex-chat").write_bytes(b"cli")
                    (work / "deb-build/simplex-chat.deb").write_bytes(b"cli deb")
                return recorder(args, **kwargs)

            with mock.patch.object(reproduce, "run", fake_run), mock.patch.object(reproduce, "log"), mock.patch.object(
                reproduce, "fetch_openssl"
            ) as fetch_openssl:
                reproduce.build_desktop_linux(source, JAMMY, "7.1.0", out)
            work = next(pathlib.Path(a.split(":")[0]) for c in recorder.commands for a in c if a.endswith(":/out"))
            fetch_openssl.assert_called_once_with(work)
            self.assertIn("flags: +openssl", (source / "cabal.project.local").read_text())
            build = next(c for c in recorder.commands if c[:2] == ["docker", "build"])
            self.assertIn("TAG=22.04", build)
            self.assertIn(f"HASH={JAMMY.digest}", build)
            self.assertEqual(
                [s[0] for s in recorder.steps()],
                ["cabal", "cabal", "cabal", "cabal", "cp", "strip"] + [s.argv[0] for s in reproduce.desktop_steps(JAMMY, "7.1.0")],
            )
            self.assertIn(["cp", "/project/dist-newstyle/build/simplex-chat", "/out/simplex-chat"], recorder.steps())
            self.assertEqual(
                {p.name: p.read_bytes() for p in out.iterdir()},
                {
                    "simplex-chat-ubuntu-22_04-x86_64": b"cli",
                    "simplex-chat-ubuntu-22_04-x86_64.deb": b"cli deb",
                    "simplex-desktop-ubuntu-22_04-x86_64.deb": b"desktop deb",
                    "simplex-desktop-x86_64.AppImage": b"appimage",
                },
            )

    def test_reproduce_reports_mismatches_and_writes_sums_only_when_all_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = pathlib.Path(tmp) / "v7.1.0-simplex-chat"

            def fake_build(source, image, version, out):
                for name in reproduce.desktop_files(image):
                    (out / name).write_bytes(name.encode())

            def fake_download(url, target):
                target.write_bytes(b"other" if target.name.endswith(".AppImage") else target.name.encode())

            with mock.patch.object(reproduce, "checkout"), mock.patch.object(
                reproduce, "build_desktop_linux", fake_build
            ), mock.patch.object(reproduce, "download", fake_download), mock.patch.object(reproduce, "log"):
                mismatched = reproduce.reproduce_desktop("v7.1.0", pathlib.Path(tmp), results)
            self.assertEqual(mismatched, ["simplex-desktop-x86_64.AppImage"])
            self.assertFalse((results / "_sha256sums").exists())


class OpenSslTest(unittest.TestCase):
    def tarball(self, path):
        member = tarfile.TarInfo(f"openssl-{reproduce.OPENSSL_VERSION}/Configure")
        member.size = 4
        with tarfile.open(path, "w:gz") as tar:
            tar.addfile(member, io.BytesIO(b"perl"))

    def test_fetch_extracts_the_checked_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            self.tarball(work / "fixture.tar.gz")
            digest = reproduce.sha256(work / "fixture.tar.gz")

            def fixture_download(url, target):
                shutil.copy(work / "fixture.tar.gz", target)

            with mock.patch.object(reproduce, "download", fixture_download), mock.patch.object(reproduce, "OPENSSL_SHA256", digest):
                source = reproduce.fetch_openssl(work)
            self.assertEqual((source / "Configure").read_text(), "perl")

    def test_fetch_rejects_a_changed_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            with mock.patch.object(reproduce, "download", lambda url, target: self.tarball(target)), self.assertRaises(
                reproduce.ReproduceError
            ) as raised:
                reproduce.fetch_openssl(work)
            self.assertIn("pinned SHA-256", str(raised.exception))
            self.assertFalse((work / f"openssl-{reproduce.OPENSSL_VERSION}").exists())

    def test_build_installs_only_when_asked(self):
        build = reproduce.openssl_build("darwin64-arm64-cc", "/w/openssl", "/etc/ssl", install=True)
        self.assertEqual(
            build[0],
            ("perl", "Configure", "darwin64-arm64-cc", "--prefix=/w/openssl", "--openssldir=/etc/ssl")
            + ("--libdir=lib", "no-docs", "no-tests"),
        )
        self.assertEqual(build[-1], ("make", "install_sw"))
        self.assertNotIn(("make", "install_sw"), reproduce.openssl_build("mingw64", "/p", "/d", install=False))

    def test_flake_pins_the_same_openssl(self):
        flake = (pathlib.Path(reproduce.__file__).parents[1] / "flake.nix").read_text()
        pin = flake[flake.index("let pinOpenssl") : flake.index("}); in", flake.index("let pinOpenssl"))]
        self.assertIn(f'version = "{reproduce.OPENSSL_VERSION}";', pin)
        self.assertIn(f'url = "{reproduce.OPENSSL_URL}";', pin)
        self.assertIn(f'sha256 = "{reproduce.OPENSSL_SHA256}";', pin)


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
        self.assertEqual(reproduce.minimum_versions(OTOOL), ["11.0", "14.2"])
        self.assertEqual(reproduce.minimum_versions("Load command 1\n      cmd LC_SYMTAB\n"), [])

    def test_versions_compare_numerically(self):
        self.assertGreater(reproduce.version_tuple("15.10"), reproduce.version_tuple("15.9"))
        self.assertEqual(max(["9.0", "15.0", "11.0"], key=reproduce.version_tuple), "15.0")

    def test_build_settings_take_the_first_value(self):
        output = (
            "Build settings for target X:\n"
            "    DEVELOPMENT_TEAM = 5NN7GUYB6T\n    IPHONEOS_DEPLOYMENT_TARGET = 15.0\n    DEVELOPMENT_TEAM = OTHER\n"
        )
        settings = reproduce.build_settings(output, ["DEVELOPMENT_TEAM", "IPHONEOS_DEPLOYMENT_TARGET"])
        self.assertEqual(settings, {"DEVELOPMENT_TEAM": "5NN7GUYB6T", "IPHONEOS_DEPLOYMENT_TARGET": "15.0"})
        with self.assertRaises(reproduce.ReproduceError):
            reproduce.build_settings("    DEVELOPMENT_TEAM = 5NN7GUYB6T\n", ["DEVELOPMENT_TEAM", "IPHONEOS_DEPLOYMENT_TARGET"])

    def test_entitlements_get_the_team_prefix(self):
        self.assertIn("<string>5NN7GUYB6T.chat.simplex.app</string>", reproduce.resolve_entitlements(ENTITLEMENTS, "5NN7GUYB6T"))

    def test_nix_env_finds_a_certificate_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = pathlib.Path(tmp) / "ca-bundle.crt"
            cert.write_text("")
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True), mock.patch.object(
                reproduce, "NIX_CERT_FILES", (pathlib.Path(tmp) / "missing", cert)
            ):
                env = reproduce.nix_env()
            self.assertEqual(env["NIX_SSL_CERT_FILE"], str(cert))
            self.assertTrue(env["PATH"].endswith(":/usr/bin"))
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(reproduce, "NIX_CERT_FILES", ()),
                self.assertRaises(reproduce.ReproduceError),
            ):
                reproduce.nix_env()


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
        with mock.patch.object(reproduce, "run", apple), mock.patch("builtins.print"):
            reproduce.sign_bundle(self.bundle, self.entitlements, "5NN7GUYB6T", self.dir)
        signs = [c for c in apple.commands if "--sign" in c]
        self.assertEqual([pathlib.Path(c[-1]).name for c in signs], ["SimpleXChat.framework", "SimpleX.app"])
        self.assertIn("5NN7GUYB6T.chat.simplex.app", apple.signed[str(self.bundle)])

    def test_rejects_a_signature_with_other_entitlements(self):
        with mock.patch.object(reproduce, "run", FakeApple(tamper=True)), self.assertRaises(reproduce.ReproduceError):
            reproduce.sign_bundle(self.bundle, self.entitlements, "5NN7GUYB6T", self.dir)

    def test_rejects_a_bundle_without_entitlements(self):
        with self.assertRaises(reproduce.ReproduceError):
            reproduce.sign_bundle(self.bundle, self.dir / "missing.entitlements", "5NN7GUYB6T", self.dir)

    def test_minimum_version_guard(self):
        libs = self.dir / "apps/ios/Libraries/ios"
        libs.mkdir(parents=True)
        (libs / "libffi.a").write_bytes(b"")
        with mock.patch.object(reproduce, "run", FakeApple()), mock.patch("builtins.print"):
            reproduce.check_minimum_versions(self.dir, "15.0")
            with self.assertRaises(reproduce.ReproduceError):
                reproduce.check_minimum_versions(self.dir, "14.0")


class IosArchiveTest(unittest.TestCase):
    def test_build_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source, art, out, home = root / "src", root / "art", root / "out", root / "home"
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

            def fake_run(args, cwd=None, env=None, input=None, check=True, capture=False):
                args = [str(a) for a in args]
                if args[:2] == ["nix", "build"]:
                    self.assertIn("NIX_SSL_CERT_FILE", env)
                    link = pathlib.Path(args[args.index("--out-link") + 1])
                    link.mkdir()
                    (link / reproduce.IOS_CORE_ZIP).write_bytes(b"core")
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

            with mock.patch.object(reproduce, "run", fake_run), mock.patch("builtins.print"), mock.patch.object(
                reproduce.Path, "home", return_value=home
            ), mock.patch.object(reproduce, "nix_env", return_value={"NIX_SSL_CERT_FILE": "/c"}):
                reproduce.build_ios_archive(source, art, out, install=False)

            self.assertEqual((home / "Downloads" / reproduce.IOS_CORE_ZIP).read_bytes(), b"core")
            xcconfig = (source / "apps/ios/Local.xcconfig").read_text()
            self.assertIn("SWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) SIMPLEX_ASSETS", xcconfig)
            self.assertIn(f"SIMPLEX_ASSETS_DIR = {art}", xcconfig)
            self.assertEqual(
                [c[0] for c in apple.commands][:7],
                [
                    "nix",
                    "scripts/ci/ios-sim/prepare-libs.sh",
                    "scripts/ios/update-pbxproj.sh",
                    "scripts/ios/copy-assets.sh",
                    "xcodebuild",
                    "otool",
                    "xcodebuild",
                ],
            )
            archive = next(c for c in apple.commands if c[:2] == ["xcodebuild", "archive"])
            self.assertIn("CODE_SIGNING_ALLOWED=NO", archive)
            signed = [c[-1] for c in apple.commands if c[0] == "codesign" and "--entitlements" in c and "-d" not in c]
            self.assertEqual([pathlib.Path(p).name for p in signed], ["SimpleX NSE.appex", "SimpleX SE.appex", "SimpleX.app"])
            self.assertIn(["plutil", "-replace", "ApplicationProperties.Team", "-string", "5NN7GUYB6T"], [c[:5] for c in apple.commands])
            self.assertEqual((out / "SimpleX.xcarchive.zip").read_bytes(), b"archive")

    def test_needs_the_art_assets(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(reproduce.ReproduceError):
            reproduce.build_ios_archive(pathlib.Path(tmp), pathlib.Path(tmp) / "art", pathlib.Path(tmp), install=False)


class GitHubLogTest(unittest.TestCase):
    def output(self, environ, action):
        with mock.patch.dict(os.environ, environ, clear=True), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as out, mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            action()
        return out.getvalue(), err.getvalue()

    def test_warn_annotates_on_github_actions(self):
        out, err = self.output(GITHUB, lambda: reproduce.warn("unsigned build"))
        self.assertEqual((out, err), ("::warning::unsigned build\n", ""))

    def test_warn_prints_to_stderr_elsewhere(self):
        out, err = self.output({}, lambda: reproduce.warn("unsigned build"))
        self.assertEqual(out, "")
        self.assertIn("warning: unsigned build", err)

    def test_error_escapes_newlines_and_percent(self):
        out, _ = self.output(GITHUB, lambda: reproduce.error("100% failed\nat step 2"))
        self.assertEqual(out, "::error::100%25 failed%0Aat step 2\n")

    def test_group_is_closed_when_its_command_fails(self):
        def failing():
            with reproduce.log_group("cabal build"):
                raise reproduce.ReproduceError("failed")

        out, _ = self.output(GITHUB, lambda: self.assertRaises(reproduce.ReproduceError, failing))
        self.assertEqual(out, "::group::cabal build\n::endgroup::\n")

    def test_run_groups_shown_output_only(self):
        def commands():
            reproduce.run([sys.executable, "-c", "print('built')"])
            reproduce.run([sys.executable, "-c", "print('hidden')"], capture=True, title="never shown")

        with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0, stdout="")):
            out, _ = self.output(GITHUB, commands)
        self.assertEqual(out, f"::group::{sys.executable} -c print('built')\n::endgroup::\n")

    def test_container_steps_are_titled_with_their_command(self):
        recorder = Recorder()
        container = reproduce.Container(pathlib.Path("."), {}, {}, [])
        with mock.patch.object(reproduce, "run", wraps=recorder) as run:
            container.run(reproduce.Step(("cabal", "update"), env={"PATH": "/x"}))
        self.assertEqual(run.call_args.kwargs["title"], "cabal update")

    def test_summary_lists_files_on_github_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "simplex-aarch64.apk").write_bytes(b"apk")
            summary = d / "summary.md"
            self.output({**GITHUB, "GITHUB_STEP_SUMMARY": str(summary)}, lambda: reproduce.write_sha256sums(d))
            text = summary.read_text()
        self.assertIn("| `simplex-aarch64.apk` | 3 | `", text)
        self.assertNotIn("SHA256SUMS`", text)

    def test_summary_is_skipped_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = pathlib.Path(tmp) / "summary.md"
            self.output({"GITHUB_STEP_SUMMARY": str(summary)}, lambda: reproduce.add_summary("text"))
            self.assertFalse(summary.exists())


class MainTest(unittest.TestCase):
    def test_errors_exit_with_1(self):
        with mock.patch.object(reproduce, "reproduce", side_effect=reproduce.ReproduceError("boom")), mock.patch.dict(
            os.environ, {}, clear=True
        ), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(reproduce.main(["v7.1.0"]), 1)
        self.assertIn("error: boom", err.getvalue())

    def test_build_commands_go_to_build(self):
        with mock.patch.object(reproduce, "build") as build:
            self.assertEqual(reproduce.main(["build", "android-bundle", "--libs", "l", "--out", "o"]), 0)
        self.assertIsInstance(build.call_args.args[0], reproduce.BuildBundle)

    def test_missing_tools_are_named(self):
        with mock.patch.object(reproduce.shutil, "which", return_value=None), self.assertRaises(reproduce.ReproduceError) as e:
            reproduce.require_tools(["git", "docker"])
        self.assertIn("git, docker", str(e.exception))

    def test_unreachable_docker_daemon(self):
        with mock.patch.object(reproduce.shutil, "which", return_value="/usr/bin/docker"), mock.patch.object(
            reproduce.subprocess, "run", return_value=mock.Mock(returncode=1)
        ), self.assertRaises(reproduce.ReproduceError) as e:
            reproduce.require_tools(["docker"])
        self.assertIn("daemon", str(e.exception))

    def test_ios_needs_nix_unless_installing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = reproduce.BuildIosArchive(pathlib.Path("a"), pathlib.Path(tmp), pathlib.Path("."), False)
            with mock.patch.object(reproduce, "require_tools"), mock.patch.object(
                reproduce, "NIX_PROFILE", pathlib.Path("/nonexistent")
            ), self.assertRaises(reproduce.ReproduceError) as e:
                reproduce.build(command)
        self.assertIn("--install-nix", str(e.exception))


if __name__ == "__main__":
    unittest.main()
