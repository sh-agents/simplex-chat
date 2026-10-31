import pathlib
import tempfile
import unittest
import zipfile
from unittest import mock

from .testing import Recorder, build_script, patch_all


class FakeApp:
    """Stands in for the app's process: alive until killed, or exited with returncode."""

    def __init__(self, returncode=None):
        self.pid = 4242
        self.returncode = returncode
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self):
        return self.returncode


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_prints_its_version(self):
        binary = self.root / "simplex-chat-ubuntu-22_04-x86_64"
        binary.write_bytes(b"cli")
        recorder = Recorder(answers={"--version": "SimpleX Chat v7.1.0.15\n"})
        with patch_all("run", recorder), patch_all("host_platform", return_value=build_script.Platform.LINUX):
            build_script.test_cli(binary, self.out)
        self.assertEqual(recorder.commands, [[str(binary), "--version"]])
        self.assertEqual((self.out / f"{binary.name}.txt").read_text(), "SimpleX Chat v7.1.0.15\n")

    def test_cli_without_a_version_fails(self):
        binary = self.root / "cli"
        binary.write_bytes(b"cli")
        with (
            patch_all("run", Recorder(answers={"--version": "usage: ...\n"})),
            patch_all("host_platform", return_value=build_script.Platform.LINUX),
            self.assertRaises(build_script.BuildError),
        ):
            build_script.test_cli(binary, self.out)

    def test_windows_cli_runs_as_an_exe(self):
        binary = self.root / "simplex-chat-windows-x86-64"
        binary.write_bytes(b"cli")
        recorder = Recorder(answers={"--version": "SimpleX Chat v7.1.0.15\n"})
        with patch_all("run", recorder), patch_all("host_platform", return_value=build_script.Platform.WINDOWS):
            build_script.test_cli(binary, self.out)
        self.assertTrue(recorder.commands[0][0].endswith("simplex-chat-windows-x86-64.exe"))

    def test_packages_install_as_users_install_them(self):
        recorder = Recorder()
        with patch_all("run", recorder), mock.patch.dict(build_script.os.environ, {"PROGRAMFILES": "C:/Program Files"}):
            deb_app, deb_env = build_script.install_desktop(pathlib.Path("simplex-desktop-ubuntu-22_04-x86_64.deb"), self.root)
            msi_app, _ = build_script.install_desktop(pathlib.Path("simplex-desktop-windows-x86_64.msi"), self.root)
        self.assertEqual(deb_app, pathlib.Path("/opt/simplex/bin/simplex"))
        self.assertEqual(deb_env["JAVA_TOOL_OPTIONS"], "-Dskiko.renderApi=SOFTWARE")
        self.assertEqual(recorder.commands[0][:4], ["sudo", "apt-get", "install", "-y"])
        self.assertEqual(msi_app, pathlib.Path("C:/Program Files/SimpleX/SimpleX.exe"))
        self.assertEqual(recorder.commands[1][:2], ["msiexec", "/i"])
        self.assertNotIn("/", recorder.commands[1][2])
        self.assertIn("/qn", recorder.commands[1])

    def test_appimage_runs_without_fuse(self):
        appimage = self.root / "simplex-desktop-x86_64.AppImage"
        appimage.write_bytes(b"appimage")
        app, env = build_script.install_desktop(appimage, self.root)
        self.assertEqual(app, appimage)
        self.assertEqual(env["APPIMAGE_EXTRACT_AND_RUN"], "1")

    def test_dmg_app_is_copied_out_and_the_image_detached(self):
        def fake_run(args, **kwargs):
            args = [str(a) for a in args]
            if args[:2] == ["hdiutil", "attach"]:
                (pathlib.Path(args[-2]) / "SimpleX.app/Contents/MacOS").mkdir(parents=True)
            return recorder(args, **kwargs)

        recorder = Recorder()
        with patch_all("run", fake_run):
            app, _ = build_script.install_desktop(pathlib.Path("simplex-desktop-macos-aarch64.dmg"), self.root)
        self.assertEqual(app, self.root / "SimpleX.app/Contents/MacOS/SimpleX")
        self.assertEqual(recorder.commands[-1][:2], ["hdiutil", "detach"])

    def test_unknown_package_is_rejected(self):
        with self.assertRaises(build_script.BuildError):
            build_script.install_desktop(pathlib.Path("app.zip"), self.root)

    def test_blank_linux_screenshot_fails(self):
        with (
            patch_all("run", Recorder(answers={"identify": "3"})),
            patch_all("host_platform", return_value=build_script.Platform.LINUX),
            self.assertRaises(build_script.BuildError),
        ):
            build_script.screenshot(self.out / "app.png", {"DISPLAY": ":99"})

    def test_app_exiting_before_its_window_fails(self):
        with patch_all("host_platform", return_value=build_script.Platform.LINUX), self.assertRaises(build_script.BuildError):
            build_script.window_open(FakeApp(returncode=1), {})

    def desktop_run(self, app):
        with (
            patch_all("install_desktop", return_value=(pathlib.Path("/opt/simplex/bin/simplex"), {})),
            patch_all("host_platform", return_value=build_script.Platform.MACOS),
            patch_all("run", Recorder()),
            mock.patch.object(build_script.subprocess, "Popen", return_value=app),
            mock.patch.object(build_script.time, "sleep"),
        ):
            build_script.test_desktop(pathlib.Path("simplex-desktop-macos-aarch64.dmg"), self.out)

    def test_desktop_app_that_keeps_running_passes_and_is_stopped(self):
        app = FakeApp()
        self.desktop_run(app)
        self.assertTrue(app.killed)

    def test_desktop_app_that_exits_after_its_window_fails(self):
        app = FakeApp()
        calls = iter([None, 1])
        app.poll = lambda: next(calls, 1)
        with self.assertRaises(build_script.BuildError) as raised:
            self.desktop_run(app)
        self.assertIn("after opening its window", str(raised.exception))

    def apk_run(self, verify_returncode, pidof, screen='<hierarchy><node text="Get started"/></hierarchy>'):
        recorder = Recorder(answers={"pidof": pidof, "abilist": "x86_64,arm64-v8a\n", "uiautomator": screen})

        def fake_run(args, **kwargs):
            if [str(a) for a in args[:4]] == ["adb", "emu", "screenrecord", "screenshot"]:
                (pathlib.Path(args[4]) / "Screenshot_1.png").write_bytes(b"png")
            if args[1:2] == ["verify"]:
                recorder.commands.append([str(a) for a in args])
                return mock.Mock(returncode=verify_returncode, stdout="")
            return recorder(args, **kwargs)

        apk = self.root / "simplex-aarch64.apk"
        with zipfile.ZipFile(apk, "w") as z:
            z.writestr("lib/arm64-v8a/libsimplex.so", b"lib")
        with (
            patch_all("run", fake_run),
            patch_all("require_tools"),
            patch_all("find_apksigner", return_value="apksigner"),
            mock.patch.object(build_script.time, "sleep"),
        ):
            build_script.test_apk(apk, self.out)
        return recorder.commands

    def test_unsigned_apk_is_signed_with_a_throwaway_key(self):
        commands = self.apk_run(verify_returncode=1, pidof="1234\n")
        self.assertTrue(any(c[0] == "keytool" for c in commands))
        install = next(c for c in commands if c[:2] == ["adb", "install"])
        self.assertNotEqual(install[-1], str(self.root / "simplex-aarch64.apk"))
        self.assertIn(["adb", "shell", "am", "start", "-W", "-n", "chat.simplex.app/.MainActivity"], commands)

    def test_screenshot_comes_from_the_emulator(self):
        commands = self.apk_run(verify_returncode=0, pidof="1234\n")
        self.assertIn("screenrecord", [c[2] for c in commands if c[:2] == ["adb", "emu"]])
        self.assertEqual((self.out / "simplex-aarch64.apk.png").read_bytes(), b"png")

    def test_apk_for_an_abi_the_emulator_lacks_is_skipped(self):
        apk = self.root / "simplex-armv7a.apk"
        with zipfile.ZipFile(apk, "w") as z:
            z.writestr("lib/armeabi-v7a/libsimplex.so", b"lib")
        recorder = Recorder(answers={"abilist": "x86_64,arm64-v8a\n"})
        with patch_all("run", recorder), patch_all("require_tools"), patch_all("warn") as warn:
            build_script.test_apk(apk, self.out)
        self.assertIn("armeabi-v7a", warn.call_args.args[0])
        self.assertFalse(any(c[:2] == ["adb", "install"] for c in recorder.commands))

    def test_apk_still_opening_its_database_fails_without_a_screenshot(self):
        with patch_all("ANDROID_FIRST_SCREEN_SECONDS", 0), self.assertRaises(build_script.BuildError) as raised:
            self.apk_run(verify_returncode=0, pidof="1234\n", screen='<hierarchy><node text="Opening database..."/></hierarchy>')
        self.assertIn("database loading", str(raised.exception))
        self.assertFalse((self.out / "simplex-aarch64.apk.png").exists())

    def test_screenshot_waits_for_the_screen_after_the_database_loading(self):
        commands = self.apk_run(verify_returncode=0, pidof="1234\n")
        dump = commands.index(["adb", "exec-out", "uiautomator", "dump", "/dev/tty"])
        screenshot = next(i for i, c in enumerate(commands) if c[:3] == ["adb", "emu", "screenrecord"])
        self.assertLess(dump, screenshot)

    def test_signed_apk_is_installed_as_it_is(self):
        commands = self.apk_run(verify_returncode=0, pidof="1234\n")
        self.assertFalse(any(c[0] == "keytool" for c in commands))

    def test_apk_that_is_not_running_after_start_fails(self):
        with self.assertRaises(build_script.BuildError):
            self.apk_run(verify_returncode=0, pidof="")
