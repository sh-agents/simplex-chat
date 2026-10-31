import io
import os
import pathlib
import platform
import tempfile
import unittest
import zipfile
from unittest import mock

from .testing import Arch, Recorder, Target, build_script, patch_all


class ParseArgsTest(unittest.TestCase):
    def test_reproduce_defaults_to_desktop_and_both_arches(self):
        self.assertEqual(
            build_script.parse_args(["reproduce", "v7.1.0"]),
            build_script.Reproduce("v7.1.0", "simplex-chat/simplex-chat", (Target.DESKTOP,), (Arch.AARCH64, Arch.ARMV7A), False),
        )

    def test_reproduce_android_with_options(self):
        self.assertEqual(
            build_script.parse_args(["reproduce", "v7.1.0", "android", "--arches", "armv7a", "--nix-cache"]),
            build_script.Reproduce("v7.1.0", "simplex-chat/simplex-chat", (Target.ANDROID,), (Arch.ARMV7A,), True),
        )

    def test_reproduce_a_release_of_a_fork(self):
        command = build_script.parse_args(["reproduce", "v7.1.99-test", "--repo", "sh-agents/simplex-chat"])
        self.assertEqual(command.repo, "sh-agents/simplex-chat")
        for repo in ("simplex-chat", "https://github.com/a/b", "a/b/c", "a/b c"):
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                build_script.parse_args(["reproduce", "v7.1.0", "--repo", repo])

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
        self.assertIsNone(build_script.parse_args(["build", "xcarchive", "--out", "o"]).art)
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
        with patch_all("run", fake_run), patch_all("install_nix") as install:
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

    def test_test_commands(self):
        self.assertEqual(
            build_script.parse_args(["test", "desktop", "simplex-desktop-x86_64.AppImage", "--out", "screens"]),
            build_script.TestRelease("desktop", pathlib.Path("simplex-desktop-x86_64.AppImage"), pathlib.Path("screens")),
        )
        self.assertEqual(
            build_script.parse_args(["test", "desktop", "app.deb", "--out", "o", "--database", "old", "--cli", "simplex-chat"]),
            build_script.TestRelease(
                "desktop", pathlib.Path("app.deb"), pathlib.Path("o"), pathlib.Path("old"), pathlib.Path("simplex-chat")
            ),
        )
        self.assertEqual(
            build_script.parse_args(["test", "apk", "simplex-armv7a.apk", "--out", "o", "--database", "old"]),
            build_script.TestRelease("apk", pathlib.Path("simplex-armv7a.apk"), pathlib.Path("o"), pathlib.Path("old")),
        )
        self.assertEqual(
            build_script.parse_args(["test", "old-database", "--out", "old"]), build_script.MakeOldDatabase(pathlib.Path("old"))
        )
        for argv in (["test", "desktop", "app.deb"], ["test", "ios", "x", "--out", "o"], ["test", "cli", "x", "--out", "o", "--cli", "y"]):
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                build_script.parse_args(argv)

    def test_desktop_upgrade_command_needs_the_cli_and_no_database(self):
        self.assertEqual(
            build_script.parse_args(["test", "desktop", "app.msi", "--out", "o", "--upgrade-from-previous", "--cli", "simplex-chat"]),
            build_script.TestRelease("desktop", pathlib.Path("app.msi"), pathlib.Path("o"), None, pathlib.Path("simplex-chat"), True),
        )
        upgrade = ["test", "desktop", "app.msi", "--out", "o", "--upgrade-from-previous"]
        for argv in (upgrade, [*upgrade, "--cli", "c", "--database", "old"], ["test", "cli", "x", "--out", "o", "--upgrade-from-previous"]):
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                build_script.parse_args(argv)

    def test_desktop_test_commands_go_to_their_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp).resolve()
            with (
                patch_all("require_tools"),
                patch_all("test_desktop") as fresh,
                patch_all("test_desktop_upgrade") as upgrade,
            ):
                build_script.test(build_script.TestRelease("desktop", pathlib.Path("app.dmg"), out, cli=pathlib.Path("cli"), upgrade=True))
                build_script.test(build_script.TestRelease("desktop", pathlib.Path("app.dmg"), out))
        upgrade.assert_called_once_with(pathlib.Path("app.dmg").resolve(), out, pathlib.Path("cli").resolve())
        fresh.assert_called_once_with(pathlib.Path("app.dmg").resolve(), out, None, None)

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


class MainTest(unittest.TestCase):
    def test_errors_exit_with_1(self):
        with (
            patch_all("reproduce", side_effect=build_script.BuildError("boom")),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            self.assertEqual(build_script.main(["reproduce", "v7.1.0"]), 1)
        self.assertIn("error: boom", err.getvalue())

    def test_build_commands_go_to_build(self):
        with patch_all("build") as build:
            self.assertEqual(build_script.main(["build", "aab", "--libs", "l", "--out", "o"]), 0)
        self.assertIsInstance(build.call_args.args[0], build_script.BuildBundle)

    def test_missing_tools_are_named(self):
        with mock.patch.object(build_script.shutil, "which", return_value=None), self.assertRaises(build_script.BuildError) as e:
            build_script.require_tools(["git", "docker"])
        self.assertIn("git, docker", str(e.exception))

    def test_unreachable_docker_daemon(self):
        with (
            mock.patch.object(build_script.shutil, "which", return_value="/usr/bin/docker"),
            mock.patch.object(build_script.subprocess, "run", return_value=mock.Mock(returncode=1)),
            self.assertRaises(build_script.BuildError) as e,
        ):
            build_script.require_tools(["docker"])
        self.assertIn("daemon", str(e.exception))

    def test_windows_desktop_needs_powershell_for_the_msi(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_script.BuildDesktopWindows(pathlib.Path(tmp), pathlib.Path("."))
            with (
                patch_all("require_tools") as require,
                patch_all("host_platform", return_value=build_script.Platform.WINDOWS),
                patch_all("build_desktop_windows") as build,
                patch_all("write_sha256sums"),
            ):
                build_script.build(command)
        self.assertIn("powershell", require.call_args.args[0])
        build.assert_called_once()

    def test_ios_needs_nix_unless_installing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_script.BuildIosArchive(pathlib.Path("a"), pathlib.Path(tmp), pathlib.Path("."), False)
            with (
                patch_all("require_tools"),
                patch_all("host_platform", return_value=build_script.Platform.MACOS),
                patch_all("NIX_PROFILE", pathlib.Path("/nonexistent")),
                self.assertRaises(build_script.BuildError) as e,
            ):
                build_script.build(command)
        self.assertIn("--install-nix", str(e.exception))
