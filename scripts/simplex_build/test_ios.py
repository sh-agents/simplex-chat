import os
import pathlib
import tempfile
import unittest
import zipfile
from unittest import mock

from .testing import (
    ENTITLEMENTS,
    OTOOL,
    PBXPROJ,
    FakeApple,
    Recorder,
    build_script,
    patch_all,
)


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
        with self.assertRaises(build_script.BuildError):
            build_script.build_settings("    DEVELOPMENT_TEAM = 5NN7GUYB6T\n", ["DEVELOPMENT_TEAM", "IPHONEOS_DEPLOYMENT_TARGET"])

    def test_entitlements_get_the_team_prefix(self):
        self.assertIn("<string>5NN7GUYB6T.chat.simplex.app</string>", build_script.resolve_entitlements(ENTITLEMENTS, "5NN7GUYB6T"))

    def test_nix_env_finds_a_certificate_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = pathlib.Path(tmp) / "ca-bundle.crt"
            cert.write_text("")
            with (
                mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
                patch_all("NIX_CERT_FILES", (pathlib.Path(tmp) / "missing", cert)),
            ):
                env = build_script.nix_env()
            self.assertEqual(env["NIX_SSL_CERT_FILE"], str(cert))
            self.assertTrue(env["PATH"].endswith(":/usr/bin"))
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                patch_all("NIX_CERT_FILES", ()),
                self.assertRaises(build_script.BuildError),
            ):
                build_script.nix_env()


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
        with patch_all("run", apple), mock.patch("builtins.print"):
            build_script.sign_bundle(self.bundle, self.entitlements, "5NN7GUYB6T", self.dir)
        signs = [c for c in apple.commands if "--sign" in c]
        self.assertEqual([pathlib.Path(c[-1]).name for c in signs], ["SimpleXChat.framework", "SimpleX.app"])
        self.assertIn("5NN7GUYB6T.chat.simplex.app", apple.signed[str(self.bundle)])

    def test_rejects_a_signature_with_other_entitlements(self):
        with patch_all("run", FakeApple(tamper=True)), self.assertRaises(build_script.BuildError):
            build_script.sign_bundle(self.bundle, self.entitlements, "5NN7GUYB6T", self.dir)

    def test_rejects_a_bundle_without_entitlements(self):
        with self.assertRaises(build_script.BuildError):
            build_script.sign_bundle(self.bundle, self.dir / "missing.entitlements", "5NN7GUYB6T", self.dir)

    def test_minimum_version_guard(self):
        libs = self.dir / "apps/ios/Libraries/ios"
        libs.mkdir(parents=True)
        (libs / "libffi.a").write_bytes(b"")
        with patch_all("run", FakeApple()), mock.patch("builtins.print"):
            build_script.check_minimum_versions(self.dir, "15.0")
            with self.assertRaises(build_script.BuildError):
                build_script.check_minimum_versions(self.dir, "14.0")


class IosArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.source, self.art, self.out = root / "src", root / "art", root / "out"
        (self.art / "ios/Assets.xcassets/logo.imageset").mkdir(parents=True)
        for name in ("SimpleX NSE", "SimpleX SE"):
            (self.source / f"apps/ios/{name}").mkdir(parents=True)
            (self.source / f"apps/ios/{name}/{name}.entitlements").write_text(ENTITLEMENTS)
        (self.source / "apps/ios/SimpleX (iOS).entitlements").write_text(ENTITLEMENTS)
        (self.source / "apps/ios/Shared/SimpleXAssets.xcassets").mkdir(parents=True)
        (self.source / "apps/ios/Libraries/ios").mkdir(parents=True)
        (self.source / "apps/ios/Libraries/ios/libHSsimplex-chat.a").write_bytes(b"")
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, art):
        """build_ios_archive with art, against stand-ins of nix, Xcode and codesign; returns their commands and warn."""
        apple = FakeApple()
        source = self.source

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
            patch_all("run", fake_run),
            mock.patch("builtins.print"),
            patch_all("nix_env", return_value={"NIX_SSL_CERT_FILE": "/c"}),
            patch_all("install_ios_libs") as install_libs,
            patch_all("warn") as warn,
        ):
            build_script.build_ios_archive(source, art, self.out, install=False)
        self.assertEqual(install_libs.call_args.args[1].name, build_script.IOS_CORE_ZIP)
        return apple.commands, warn

    def test_build_flow(self):
        commands, warn = self.build(self.art)
        warn.assert_not_called()
        xcconfig = (self.source / "apps/ios/Local.xcconfig").read_text()
        self.assertIn("SWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) SIMPLEX_ASSETS", xcconfig)
        self.assertIn(f"SIMPLEX_ASSETS_DIR = {self.art}", xcconfig)
        self.assertEqual([c[0] for c in commands][:5], ["nix", "scripts/ios/copy-assets.sh", "xcodebuild", "otool", "xcodebuild"])
        archive = next(c for c in commands if c[:2] == ["xcodebuild", "archive"])
        self.assertIn("CODE_SIGNING_ALLOWED=NO", archive)
        signed = [c[-1] for c in commands if c[0] == "codesign" and "--entitlements" in c and "-d" not in c]
        self.assertEqual([pathlib.Path(p).name for p in signed], ["SimpleX NSE.appex", "SimpleX SE.appex", "SimpleX.app"])
        self.assertIn(["plutil", "-replace", "ApplicationProperties.Team", "-string", "5NN7GUYB6T"], [c[:5] for c in commands])
        self.assertEqual((self.out / "SimpleX.xcarchive.zip").read_bytes(), b"archive")

    def test_without_the_art_checkout_the_archive_has_the_default_icons(self):
        commands, warn = self.build(None)
        self.assertIn("default icons", warn.call_args.args[0])
        self.assertFalse((self.source / "apps/ios/Local.xcconfig").exists())
        self.assertNotIn("scripts/ios/copy-assets.sh", [c[0] for c in commands])
        self.assertEqual((self.out / "SimpleX.xcarchive.zip").read_bytes(), b"archive")

    def test_art_checkout_without_the_assets_fails(self):
        with self.assertRaises(build_script.BuildError):
            build_script.build_ios_archive(self.source, self.art / "missing", self.out, install=False)


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
        with patch_all("log"):
            build_script.update_pbxproj(self.source)
        text = (self.source / build_script.IOS_PBXPROJ).read_text()
        self.assertNotIn("Old", text)
        self.assertEqual(text.count("libHSsimplex-chat-6.5.1.0-New-ghc9.6.3.a"), 6)
        self.assertEqual(text.count("libHSsimplex-chat-6.5.1.0-New.a"), 6)

    def test_pbxproj_without_four_references_each_is_not_changed(self):
        (self.source / build_script.IOS_PBXPROJ).write_text(PBXPROJ.replace("    D1 /* libHSsimplex-chat-6.5.0.0-Old.a */,\n", ""))
        self.install_libraries("libHSsimplex-chat-6.5.1.0-New.a", "libHSsimplex-chat-6.5.1.0-New-ghc9.6.3.a")
        with self.assertRaises(build_script.BuildError):
            build_script.update_pbxproj(self.source)
        self.assertIn("Old", (self.source / build_script.IOS_PBXPROJ).read_text())

    def test_pbxproj_needs_both_libraries(self):
        self.install_libraries("libHSsimplex-chat-6.5.1.0-New.a")
        with self.assertRaises(build_script.BuildError):
            build_script.update_pbxproj(self.source)

    def test_simulator_libraries_must_be_marked_for_the_simulator(self):
        sim = self.root / "sim"
        sim.mkdir()
        (sim / "libffi.a").write_bytes(b"")
        for platform_line, rejected in (("platform 7", False), ("platform IOSSIMULATOR", False), ("platform 2", True)):
            with patch_all("run", Recorder(answers={"otool": f"Load command 1\n {platform_line}\n"})):
                if rejected:
                    with self.assertRaises(build_script.BuildError):
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
            patch_all("run", recorder),
            patch_all("fetch_pinned", lambda url, digest, target: target),
            patch_all("log"),
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
        with patch_all("run", recorder), patch_all("host_openssl", return_value=openssl):
            build_script.build_ios_sim_libs(source, self.root)
        self.assertIn(f"extra-lib-dirs: {self.root / 'o/lib'}", (source / "cabal.project.local").read_text())
        self.assertIn(["cabal", "build", "-j", "lib:simplex-chat"], recorder.commands)
        with zipfile.ZipFile(self.root / build_script.IOS_CORE_ZIP) as z:
            self.assertEqual(sorted(z.namelist()), ["libHSsimplex-chat-6.5.1.0-New-ghc9.6.3.a", "libffi.a", "libgmp.a", "libgmpxx.a"])
