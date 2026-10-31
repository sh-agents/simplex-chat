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

    def apk_run(self, verify_returncode, pidof, screen='<hierarchy><node text="Get started"/></hierarchy>', database=None):
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
            build_script.test_apk(apk, self.out, database)
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

    def test_apk_gets_the_old_database_before_it_starts(self):
        opened = []
        with (
            patch_all("push_android_old_database", lambda database: opened.append(("push", database))),
            patch_all("open_android_old_database", lambda: opened.append(("open",))),
        ):
            commands = self.apk_run(verify_returncode=0, pidof="1234\n", database=self.root / "old")
        self.assertEqual(opened, [("push", self.root / "old"), ("open",)])
        install = next(c for c in commands if c[:2] == ["adb", "install"])
        self.assertEqual(install[2], "-g")

    def test_signed_apk_is_installed_as_it_is(self):
        commands = self.apk_run(verify_returncode=0, pidof="1234\n")
        self.assertFalse(any(c[0] == "keytool" for c in commands))

    def test_apk_that_is_not_running_after_start_fails(self):
        with self.assertRaises(build_script.BuildError):
            self.apk_run(verify_returncode=0, pidof="")


class OldDatabaseTest(unittest.TestCase):
    """The previous release's encrypted database, opened by every new build."""

    GROUPS = "Current user: alice\n#team (1 member)\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.database = self.root / "old"
        self.database.mkdir()
        for suffix in build_script.DATABASE_SUFFIXES:
            (self.database / f"simplex{suffix}").write_bytes(f"encrypted{suffix}".encode())

    def tearDown(self):
        self.tmp.cleanup()

    def cli_run(self, opens=True, wrong_key_opens=False):
        """Stands in for run(): the CLI opens the database with the old release's key, and with a wrong one only when told."""
        commands = []

        def fake_run(args, input=None, check=True, capture=False, **kwargs):
            args = [str(a) for a in args]
            commands.append(args)
            if "--version" in args:
                return mock.Mock(returncode=0, stdout="SimpleX Chat v7.1.0.15\n")
            right_key = args[args.index("-k") + 1] == "old-release-key"
            if right_key and opens or not right_key and wrong_key_opens:
                return mock.Mock(returncode=0, stdout=self.GROUPS)
            return mock.Mock(returncode=1, stdout="")

        return fake_run, commands

    def test_old_release_cli_makes_the_database_with_a_user_and_a_group(self):
        recorder = Recorder()
        fetched = []

        def fake_fetch(url, digest, target):
            fetched.append((url, digest))
            target.write_bytes(b"old cli")
            return target

        with (
            patch_all("run", recorder),
            patch_all("fetch_pinned", fake_fetch),
            mock.patch.object(build_script.platform, "machine", return_value="aarch64"),
        ):
            build_script.make_old_database(self.root / "db")
        self.assertEqual(
            fetched,
            [
                (
                    "https://github.com/simplex-chat/simplex-chat/releases/download/v7.0.3/simplex-chat-ubuntu-22_04-aarch64",
                    "91f332edc813cab78a848da7ba75ed91bcb8d09abf1fc92eca74330dc29ec970",
                )
            ],
        )
        (command,) = recorder.commands
        self.assertEqual(
            command[1:], ["-d", str(self.root / "db/simplex"), "-k", "old-release-key", "--user-display-name", "alice", "-e", "/g team"]
        )
        self.assertEqual(recorder.inputs, [""])

    def test_old_database_needs_a_pinned_cli_for_the_machine(self):
        with mock.patch.object(build_script.platform, "machine", return_value="riscv64"), self.assertRaises(build_script.BuildError):
            build_script.make_old_database(self.root / "db")

    def test_cli_opens_and_migrates_the_old_database_and_refuses_a_wrong_key(self):
        binary = self.root / "simplex-chat-ubuntu-24_04-x86_64"
        binary.write_bytes(b"cli")
        fake_run, commands = self.cli_run()
        with patch_all("run", fake_run), patch_all("host_platform", return_value=build_script.Platform.LINUX):
            build_script.test_cli(binary, self.out, self.database)
        opened, wrong = commands[1], commands[2]
        self.assertEqual(opened[opened.index("-k") + 1 :], ["old-release-key", "-e", "/groups", "-y"])
        self.assertEqual(wrong[wrong.index("-k") + 1], "not-old-release-key")
        self.assertEqual((self.out / f"{binary.name}.database.txt").read_text(), self.GROUPS)

    def test_cli_that_cannot_open_the_old_database_fails(self):
        binary = self.root / "cli"
        binary.write_bytes(b"cli")
        fake_run, _ = self.cli_run(opens=False)
        with (
            patch_all("run", fake_run),
            patch_all("host_platform", return_value=build_script.Platform.LINUX),
            self.assertRaises(build_script.BuildError) as raised,
        ):
            build_script.test_cli(binary, self.out, self.database)
        self.assertIn("did not open the v7.0.3 database", str(raised.exception))

    def test_cli_that_opens_the_database_with_a_wrong_key_fails(self):
        binary = self.root / "cli"
        binary.write_bytes(b"cli")
        fake_run, _ = self.cli_run(wrong_key_opens=True)
        with (
            patch_all("run", fake_run),
            patch_all("host_platform", return_value=build_script.Platform.LINUX),
            self.assertRaises(build_script.BuildError) as raised,
        ):
            build_script.test_cli(binary, self.out, self.database)
        self.assertIn("wrong key", str(raised.exception))

    def test_desktop_directories_follow_xdg_and_appdata(self):
        env = {"XDG_CONFIG_HOME": "/cfg", "XDG_DATA_HOME": "/data", "APPDATA": "C:/Users/u/AppData/Roaming"}
        with mock.patch.dict(build_script.os.environ, env), patch_all("host_platform", return_value=build_script.Platform.MACOS):
            self.assertEqual(build_script.desktop_dirs(), (pathlib.Path("/cfg/simplex"), pathlib.Path("/data/simplex")))
        with mock.patch.dict(build_script.os.environ, env), patch_all("host_platform", return_value=build_script.Platform.WINDOWS):
            appdata = pathlib.Path("C:/Users/u/AppData/Roaming/SimpleX")
            self.assertEqual(build_script.desktop_dirs(), (appdata, appdata))

    def test_desktop_settings_store_the_key_as_the_desktop_cryptor_does(self):
        settings = build_script.desktop_settings_with_key("old-release-key")
        self.assertEqual(
            settings.splitlines(),
            [
                "StoreDBPassphrase=true",
                "EncryptedDBPassphrase=b2xkLXJlbGVhc2Uta2V5",
                "InitializationVectorDBPassphrase=b2xkLXJlbGVhc2Uta2V5",
            ],
        )

    def desktop_env(self):
        return mock.patch.dict(
            build_script.os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"), "XDG_DATA_HOME": str(self.root / "data")}
        )

    def test_desktop_old_database_is_where_the_app_opens_it_and_removed_after(self):
        with self.desktop_env(), patch_all("host_platform", return_value=build_script.Platform.LINUX):
            with build_script.desktop_old_database(self.database) as prefix:
                self.assertEqual(prefix, self.root / "data/simplex/simplex_v1")
                self.assertEqual((self.root / "data/simplex/simplex_v1_chat.db").read_bytes(), b"encrypted_chat.db")
                self.assertEqual((self.root / "data/simplex/simplex_v1_agent.db").read_bytes(), b"encrypted_agent.db")
                self.assertIn("StoreDBPassphrase=true", (self.root / "config/simplex/settings.properties").read_text())
            self.assertFalse((self.root / "data/simplex").exists())
            self.assertFalse((self.root / "config/simplex").exists())

    def test_desktop_old_database_refuses_to_replace_existing_app_data(self):
        (self.root / "data/simplex").mkdir(parents=True)
        (self.root / "data/simplex/simplex_v1_chat.db").write_bytes(b"someone's chats")
        with (
            self.desktop_env(),
            patch_all("host_platform", return_value=build_script.Platform.LINUX),
            self.assertRaises(build_script.BuildError),
            build_script.desktop_old_database(self.database),
        ):
            pass
        self.assertEqual((self.root / "data/simplex/simplex_v1_chat.db").read_bytes(), b"someone's chats")

    def test_desktop_old_database_test_needs_the_cli(self):
        with self.assertRaises(build_script.BuildError):
            build_script.test_desktop(self.root / "SimpleX.deb", self.out, self.database, None)

    def test_desktop_app_must_have_migrated_the_old_database(self):
        package = self.root / "simplex-desktop-ubuntu-24_04-x86_64.deb"
        cli = self.root / "simplex-chat"
        cli.write_bytes(b"cli")
        fake_run, commands = self.cli_run()
        with (
            self.desktop_env(),
            patch_all("run", fake_run),
            patch_all("host_platform", return_value=build_script.Platform.MACOS),
            patch_all("install_desktop", return_value=(self.root / "app", {})),
            patch_all("window_open", return_value=True),
            patch_all("screenshot"),
            mock.patch.object(build_script.subprocess, "Popen", return_value=FakeApp()),
            mock.patch.object(build_script.time, "sleep"),
        ):
            build_script.test_desktop(package, self.out, self.database, cli)
        (check,) = commands
        self.assertEqual(check[0], str(cli))
        self.assertEqual(check[1:3], ["-d", str(self.root / "data/simplex/simplex_v1")])
        self.assertNotIn("-y", check)

    ANDROID_SCREEN = (
        '<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\' ?><hierarchy rotation="0">'
        '<node text="Database passphrase is required to open chat." bounds="[0,100][1080,200]" />'
        '<node text="Upgrade and open chat" bounds="[300,900][780,1000]" /></hierarchy>'
        "UI hierchary dumped to: /dev/tty"
    )

    def test_android_node_centre_from_uiautomator_dump(self):
        self.assertEqual(build_script.android_node_centre(self.ANDROID_SCREEN, "Upgrade and"), (540, 950))
        self.assertIsNone(build_script.android_node_centre(self.ANDROID_SCREEN, "Save passphrase"))

    def test_android_old_database_is_pushed_as_the_app_owns_it(self):
        recorder = Recorder(answers={"stat": "10123:10123\n"})
        with patch_all("run", recorder):
            build_script.push_android_old_database(self.database)
        self.assertEqual(recorder.commands[:2], [["adb", "root"], ["adb", "wait-for-device"]])
        target = "/data/user/0/chat.simplex.app/files_chat.db"
        self.assertIn(["adb", "push", str(self.database / "simplex_chat.db"), target], recorder.commands)
        self.assertIn(["adb", "shell", "chown", "10123:10123", target], recorder.commands)
        self.assertIn(["adb", "shell", "restorecon", "/data/user/0/chat.simplex.app/files_agent.db"], recorder.commands)

    def test_android_app_opens_the_old_database_with_its_key_and_confirms_its_upgrade(self):
        chat_list = '<?xml version="1.0"?><hierarchy><node text="team" bounds="[0,300][1080,400]" /></hierarchy>'
        screens = iter([self.ANDROID_SCREEN, self.ANDROID_SCREEN, self.ANDROID_SCREEN, chat_list])
        recorder = Recorder()
        with (
            patch_all("run", recorder),
            patch_all("android_screen", lambda: next(screens)),
            mock.patch.object(build_script.time, "sleep"),
        ):
            build_script.open_android_old_database()
        self.assertEqual(
            recorder.commands,
            [
                ["adb", "shell", "input", "text", "old-release-key"],
                ["adb", "shell", "input", "keyevent", "KEYCODE_ENTER"],
                ["adb", "shell", "input", "tap", "540", "950"],
            ],
        )
