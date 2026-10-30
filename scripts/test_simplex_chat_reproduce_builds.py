#!/usr/bin/env python3

import importlib.util
import io
import pathlib
import struct
import tempfile
import unittest
import zipfile
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "reproduce", pathlib.Path(__file__).with_name("simplex-chat-reproduce-builds.py")
)
reproduce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reproduce)

UTF8_FLAG = 0x800
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


class ApkPaddingTest(unittest.TestCase):
    def test_apksigner_padding_is_detected_on_any_entry(self):
        apk = make_zip([("classes.dex", b"dex", 0, b""), ("res/a.png", b"png", 0, APKSIGNER_PADDING)])
        self.assertEqual(reproduce.apk_padding(apk), "apksigner")

    def test_zip_without_the_extra_field_has_zero_padding(self):
        apk = make_zip([("classes.dex", b"dex", 0, b""), ("res/a.png", b"png", 0, b"\x00\x00")])
        self.assertEqual(reproduce.apk_padding(apk), "zero")

    def test_non_zip_is_rejected(self):
        with self.assertRaises(reproduce.ReproduceError):
            reproduce.apk_padding(b"not a zip")


class CopyV1FlagsTest(unittest.TestCase):
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
        self.assertEqual(reproduce.android_build_tag("v6.4.0", tags, "armv7a"), "v6.4.0-armv7a")
        self.assertEqual(reproduce.android_build_tag("v6.4.0", tags, "aarch64"), "v6.4.0")
        self.assertEqual(reproduce.android_build_tag("v7.1.0", tags, "armv7a"), "v7.1.0")

    def test_android_vercode(self):
        with tempfile.TemporaryDirectory() as tmp:
            props = pathlib.Path(tmp) / "apps/multiplatform/gradle.properties"
            props.parent.mkdir(parents=True)
            props.write_text("android.version_name=7.1-beta.7\nandroid.version_code=387\n")
            self.assertEqual(reproduce.android_vercode(tmp), "387")
            props.write_text("android.version_name=7.1\n")
            with self.assertRaises(reproduce.ReproduceError):
                reproduce.android_vercode(tmp)


class ChecksumsTest(unittest.TestCase):
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
            self.assertEqual(reproduce.compare_dirs(built, released), ["differs", "missing"])


class ParseArgsTest(unittest.TestCase):
    def test_reproduce_defaults_to_desktop_and_both_arches(self):
        args = reproduce.parse_args(["v7.1.0"])
        self.assertIs(args.command, reproduce.reproduce)
        self.assertEqual((args.tag, args.targets, args.arches, args.nix_cache), ("v7.1.0", [], ["aarch64", "armv7a"], False))

    def test_reproduce_android_with_options(self):
        args = reproduce.parse_args(["v7.1.0", "android", "--arches", "armv7a", "--nix-cache"])
        self.assertEqual((args.targets, args.arches, args.nix_cache), (["android"], ["armv7a"], True))

    def test_reproduce_rejects_unknown_arch(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            reproduce.parse_args(["v7.1.0", "android", "--arches", "x86"])

    def test_build_apk(self):
        args = reproduce.parse_args(["build", "android-apk", "armv7a", "--out", "o", "--nix-cache"])
        self.assertIs(args.command, reproduce.build)
        self.assertEqual((args.kind, args.arch, args.out, args.source, args.nix_cache), ("android-apk", "armv7a", "o", ".", True))

    def test_build_bundle_needs_libs(self):
        args = reproduce.parse_args(["build", "android-bundle", "--libs", "l", "--out", "o", "--source", "s"])
        self.assertEqual((args.kind, args.libs, args.out, args.source), ("android-bundle", "l", "o", "s"))
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            reproduce.parse_args(["build", "android-bundle", "--out", "o"])


class Recorder:
    """Stands in for run(): records commands, fails the ones matching fail_once the first time."""

    def __init__(self, fail_once=()):
        self.commands = []
        self.fail_once = list(fail_once)

    def __call__(self, args, cwd=None, check=True, capture=False):
        self.commands.append([str(a) for a in args])
        joined = " ".join(map(str, args))
        for pattern in self.fail_once:
            if pattern in joined:
                self.fail_once.remove(pattern)
                if check:
                    raise reproduce.ReproduceError(f"failed: {joined}")
        return mock.Mock(returncode=0, stdout="")


class ContainerTest(unittest.TestCase):
    def test_lifecycle_and_exec(self):
        recorder = Recorder()
        with mock.patch.object(reproduce, "run", recorder):
            with reproduce.Container("/src", {"/src": "/project"}, {"TAG": "22.04"}, ["--cap-add", "SYS_ADMIN"]) as c:
                c.exec("echo hi", env={"A": "1"}, user="root")
        build, start, exec_, rm, rmi = recorder.commands
        self.assertEqual(build[:4], ["docker", "build", "--no-cache", "-f"])
        self.assertIn("TAG=22.04", build)
        self.assertEqual(build[-1], "/src")
        self.assertEqual(start[:6], ["docker", "run", "-t", "-d", "--name", c.name])
        self.assertIn("/src:/project", start)
        self.assertEqual(exec_, ["docker", "exec", "-u", "root", "-e", "A=1", c.name, "sh", "-eu", "-c", "echo hi"])
        self.assertEqual((rm[:3], rmi[:3]), (["docker", "rm", "--force"], ["docker", "image", "rm"]))

    def test_image_is_removed_when_the_container_fails_to_start(self):
        recorder = Recorder(fail_once=["docker run"])
        with mock.patch.object(reproduce, "run", recorder), self.assertRaises(reproduce.ReproduceError):
            with reproduce.Container("/src", {}):
                pass
        self.assertEqual(recorder.commands[-1][:3], ["docker", "image", "rm"])


class AndroidBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = pathlib.Path(self.tmp.name) / "src"
        props = self.source / "apps/multiplatform/gradle.properties"
        props.parent.mkdir(parents=True)
        props.write_text("android.version_code=387\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_armv7a_container_sets_pid_max_in_its_namespace(self):
        recorder = Recorder()
        with mock.patch.object(reproduce, "run", recorder):
            with reproduce.android_container(self.source, self.tmp.name, set_pid_max=True):
                pass
        start = next(c for c in recorder.commands if c[1] == "run")
        self.assertIn("systempaths=unconfined", start)
        pid_max = next(c for c in recorder.commands if "pid_max" in c[-1])
        self.assertEqual(pid_max[2:4], ["-u", "root"])

    def test_apk_build_retries_once_and_keeps_the_libraries(self):
        recorder = Recorder(fail_once=["build-android.sh"])
        with mock.patch.object(reproduce, "run", recorder), mock.patch.object(reproduce, "log"):
            container = reproduce.Container(self.source, {})
            reproduce.build_apk(container, self.source, "armv7a", "simplex-armv7a.apk", nix_cache=True, keep_libs=True)
        builds = [c for c in recorder.commands if "build-android.sh" in c[-1]]
        self.assertEqual(len(builds), 2)
        self.assertIn("VERCODE=387", builds[0])
        self.assertIn("ABI=armeabi-v7a", builds[0])
        self.assertTrue(any(a.startswith("NIX_CONFIG=extra-substituters") for a in builds[0]))
        self.assertIn("pkg-$ARCH-android-libsimplex.zip", recorder.commands[-1][-1])

    def test_apk_build_without_cache_or_libraries(self):
        recorder = Recorder()
        with mock.patch.object(reproduce, "run", recorder):
            reproduce.build_apk(reproduce.Container(self.source, {}), self.source, "aarch64", "x.apk", False, False)
        self.assertEqual(len(recorder.commands), 1)
        self.assertFalse(any(a.startswith("NIX_CONFIG") for a in recorder.commands[0]))

    def test_bundle_unzips_both_arches_from_any_subdirectory(self):
        libs = pathlib.Path(self.tmp.name) / "libs"
        for arch in ("aarch64", "armv7a"):
            (libs / f"android-{arch}-unsigned").mkdir(parents=True)
            for lib in ("libsimplex", "libsupport"):
                (libs / f"android-{arch}-unsigned" / f"pkg-{arch}-android-{lib}.zip").write_bytes(b"zip")
        recorder = Recorder()
        with mock.patch.object(reproduce, "run", recorder):
            reproduce.build_bundle(reproduce.Container(self.source, {}), self.source, libs)
        unzips = [c for c in recorder.commands if c[0] == "unzip"]
        self.assertEqual(len(unzips), 4)
        self.assertTrue(unzips[2][-1].endswith("libs/armeabi-v7a"))
        self.assertIn(":android:bundleGoogleRelease", recorder.commands[-1][-1])
        local_properties = (self.source / "apps/multiplatform/local.properties").read_text()
        self.assertEqual(local_properties, "compression.level=5\nsimplex.assets.dir=../../assets\n")

    def test_bundle_fails_without_a_library(self):
        with self.assertRaises(reproduce.ReproduceError):
            reproduce.build_bundle(reproduce.Container(self.source, {}), self.source, pathlib.Path(self.tmp.name))


class VerifyApkTest(unittest.TestCase):
    def check(self, released_bytes, signed_by_copier, expect_pad):
        with tempfile.TemporaryDirectory() as out:
            (pathlib.Path(out) / "rel.apk").write_bytes(released_bytes)
            recorder = Recorder()

            def fake_run(args, **kwargs):
                (pathlib.Path(out) / "built.apk.signed").write_bytes(signed_by_copier)
                return recorder(args, **kwargs)

            with mock.patch.object(reproduce, "run", fake_run):
                result = reproduce.verify_apk(reproduce.Container(out, {}), out, "rel.apk", "built.apk")
            self.assertEqual("--pad-like-apksigner" in recorder.commands[0][-1], expect_pad)
            return result

    def test_flags_dropped_by_apksigcopier_are_restored(self):
        self.assertTrue(self.check(SIGNED, COPIED, expect_pad=False))

    def test_apksigner_padded_release_is_rebuilt_with_the_same_padding(self):
        padded = make_zip([("res/a.png", b"png", 0, APKSIGNER_PADDING)])
        self.assertTrue(self.check(padded, padded, expect_pad=True))

    def test_different_content_does_not_verify(self):
        other = make_zip([("classes.dex", b"other", 0, b"")])
        self.assertFalse(self.check(SIGNED, other, expect_pad=False))


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


class FakeApple:
    """Stands in for run() with Apple's tools: codesign stores and returns entitlements, otool returns OTOOL."""

    def __init__(self, tamper=False, otool=OTOOL):
        self.commands = []
        self.signed = {}
        self.tamper = tamper
        self.otool = otool

    def __call__(self, args, cwd=None, check=True, capture=False):
        args = [str(a) for a in args]
        self.commands.append(args)
        stdout = ""
        if args[0] == "codesign" and "--entitlements" in args and "-d" not in args:
            text = pathlib.Path(args[args.index("--entitlements") + 1]).read_text()
            self.signed[args[-1]] = text.replace("5NN7GUYB6T", "XXXX") if self.tamper else text
        elif args[0] == "codesign" and "-d" in args:
            stdout = self.signed[args[-1]]
        elif args[0] == "otool":
            stdout = self.otool
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
        self.assertTrue(signs[0][-1].endswith("SimpleXChat.framework"))
        self.assertEqual(signs[1][-1], str(self.bundle))
        self.assertEqual(len(signs), 2)
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

            def fake_run(args, cwd=None, check=True, capture=False):
                args = [str(a) for a in args]
                joined = " ".join(args)
                if "nix build" in joined:
                    link = pathlib.Path(joined.split('--out-link "')[1].split('"')[0])
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
                return apple(args, cwd, check, capture)

            with mock.patch.object(reproduce, "run", fake_run), mock.patch("builtins.print"), mock.patch.object(
                reproduce.Path, "home", return_value=home
            ):
                reproduce.build_ios_archive(source, art, out, install=False)

            self.assertEqual((home / "Downloads" / reproduce.IOS_CORE_ZIP).read_bytes(), b"core")
            xcconfig = (source / "apps/ios/Local.xcconfig").read_text()
            self.assertIn("SWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) SIMPLEX_ASSETS", xcconfig)
            programs = [c[0] if c[0] != "sh" else "nix" for c in apple.commands]
            order = [
                "nix",
                "scripts/ci/ios-sim/prepare-libs.sh",
                "scripts/ios/update-pbxproj.sh",
                "scripts/ios/copy-assets.sh",
                "xcodebuild",
                "otool",
                "xcodebuild",
            ]
            self.assertEqual(programs[: len(order)], order)
            archive = next(c for c in apple.commands if c[:2] == ["xcodebuild", "archive"])
            self.assertIn("CODE_SIGNING_ALLOWED=NO", archive)
            signed = [c[-1] for c in apple.commands if c[0] == "codesign" and "--entitlements" in c and "-d" not in c]
            self.assertEqual([pathlib.Path(p).name for p in signed], ["SimpleX NSE.appex", "SimpleX SE.appex", "SimpleX.app"])
            self.assertIn(["plutil", "-replace", "ApplicationProperties.Team", "-string", "5NN7GUYB6T"], [c[:5] for c in apple.commands])
            self.assertEqual((out / "SimpleX.xcarchive.zip").read_bytes(), b"archive")

    def test_needs_the_art_assets(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(reproduce.ReproduceError):
            reproduce.build_ios_archive(pathlib.Path(tmp), pathlib.Path(tmp) / "art", pathlib.Path(tmp), install=False)


class MainTest(unittest.TestCase):
    def test_errors_exit_with_1(self):
        with mock.patch.object(reproduce, "reproduce", side_effect=reproduce.ReproduceError("boom")), mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ) as err:
            args = reproduce.parse_args(["v7.1.0"])
            with mock.patch.object(reproduce, "parse_args", return_value=args):
                args.command = reproduce.reproduce
                self.assertEqual(reproduce.main(["v7.1.0"]), 1)
        self.assertIn("error: boom", err.getvalue())


if __name__ == "__main__":
    unittest.main()
