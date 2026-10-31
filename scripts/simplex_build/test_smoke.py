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


def desktop_env(root):
    """The app's settings and data directories under root."""
    return mock.patch.dict(build_script.os.environ, {"XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data")})


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

    def test_appimage_is_copied_to_work_and_runs_without_fuse_extracted_there(self):
        appimage = self.root / "simplex-desktop-x86_64.AppImage"
        appimage.write_bytes(b"appimage")
        work = self.root / "work"
        work.mkdir()
        app, env = build_script.install_desktop(appimage, work)
        self.assertEqual(app, work / "simplex-desktop-x86_64.AppImage")
        self.assertEqual(app.read_bytes(), b"appimage")
        self.assertEqual(app.stat().st_mode & 0o777, 0o755)
        self.assertEqual(env["APPIMAGE_EXTRACT_AND_RUN"], "1")
        self.assertEqual(env["TMPDIR"], str(work))

    def test_new_appimage_replaces_the_installed_one(self):
        work = self.root / "work"
        (self.root / "old").mkdir()
        work.mkdir()
        old, new = self.root / "old/simplex-desktop-x86_64.AppImage", self.root / "simplex-desktop-x86_64.AppImage"
        old.write_bytes(b"v7.0.3")
        new.write_bytes(b"new")
        old_app, _ = build_script.install_desktop(old, work)
        app, _ = build_script.install_desktop(new, work)
        self.assertEqual(old_app, app)
        self.assertEqual(app.read_bytes(), b"new")

    def dmg_install(self, version):
        def fake_run(args, **kwargs):
            args = [str(a) for a in args]
            if args[:2] == ["hdiutil", "attach"]:
                macos = pathlib.Path(args[-2]) / "SimpleX.app/Contents/MacOS"
                macos.mkdir(parents=True)
                (macos / f"SimpleX-{version}").write_bytes(b"app")
            if args[:2] == ["hdiutil", "detach"]:
                build_script.remove_tree(pathlib.Path(args[-1]))
            return recorder(args, **kwargs)

        recorder = Recorder()
        with patch_all("run", fake_run):
            app, _ = build_script.install_desktop(pathlib.Path("simplex-desktop-macos-aarch64.dmg"), self.root)
        return app, recorder.commands

    def test_dmg_app_is_copied_out_and_the_image_detached(self):
        app, commands = self.dmg_install("new")
        self.assertEqual(app, self.root / "SimpleX.app/Contents/MacOS/SimpleX")
        self.assertEqual(commands[-1][:2], ["hdiutil", "detach"])

    def test_new_dmg_app_replaces_the_installed_bundle(self):
        self.dmg_install("old")
        self.dmg_install("new")
        self.assertEqual([p.name for p in (self.root / "SimpleX.app/Contents/MacOS").iterdir()], ["SimpleX-new"])

    def test_system_wide_packages_are_uninstalled_and_others_left_to_work(self):
        recorder = Recorder()
        with patch_all("run", recorder):
            build_script.uninstall_desktop(pathlib.Path("simplex-desktop-ubuntu-22_04-x86_64.deb"), self.root)
            build_script.uninstall_desktop(self.root / "simplex-desktop-windows-x86_64.msi", self.root)
            build_script.uninstall_desktop(pathlib.Path("simplex-desktop-x86_64.AppImage"), self.root)
            build_script.uninstall_desktop(pathlib.Path("simplex-desktop-macos-aarch64.dmg"), self.root)
        deb, msi = recorder.commands
        self.assertEqual(deb, ["sudo", "apt-get", "remove", "-y", "simplex"])
        self.assertEqual(msi[:2], ["msiexec", "/x"])
        self.assertTrue(msi[2].endswith("\\simplex-desktop-windows-x86_64.msi"))
        self.assertNotIn("/", msi[2])
        self.assertIn("/qn", msi)

    def test_stop_app_kills_its_process_group_on_posix(self):
        app = FakeApp()
        with patch_all("host_platform", return_value=build_script.Platform.LINUX), mock.patch.object(build_script.os, "killpg") as killpg:
            build_script.stop_app(app)
        killpg.assert_called_once_with(4242, build_script.signal.SIGKILL)
        self.assertFalse(app.killed)

    def test_stop_app_whose_processes_already_exited(self):
        with (
            patch_all("host_platform", return_value=build_script.Platform.MACOS),
            mock.patch.object(build_script.os, "killpg", side_effect=ProcessLookupError),
        ):
            build_script.stop_app(FakeApp(returncode=1))

    def test_stop_app_kills_the_process_on_windows(self):
        app = FakeApp()
        with patch_all("host_platform", return_value=build_script.Platform.WINDOWS), mock.patch.object(build_script.os, "killpg") as killpg:
            build_script.stop_app(app)
        self.assertTrue(app.killed)
        killpg.assert_not_called()

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

    def desktop_run(self, app, popen=None):
        with (
            desktop_env(self.root),
            patch_all("install_desktop", return_value=(pathlib.Path("/opt/simplex/bin/simplex"), {})),
            patch_all("host_platform", return_value=build_script.Platform.MACOS),
            patch_all("run", Recorder()),
            mock.patch.object(build_script.subprocess, "Popen", side_effect=popen, return_value=app) as popen,
            mock.patch.object(build_script.os, "killpg") as killpg,
            mock.patch.object(build_script.time, "sleep"),
        ):
            build_script.test_desktop(pathlib.Path("simplex-desktop-macos-aarch64.dmg"), self.out)
        return popen, killpg

    def test_desktop_app_that_keeps_running_passes_and_is_stopped_with_its_children(self):
        popen, killpg = self.desktop_run(FakeApp())
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(4242, build_script.signal.SIGKILL)

    def test_fresh_desktop_app_runs_with_its_own_data_removed_after(self):
        def fake_popen(args, **kwargs):
            (self.root / "data/simplex").mkdir(parents=True)
            return FakeApp()

        self.desktop_run(FakeApp(), fake_popen)
        self.assertFalse((self.root / "data/simplex").exists())

    def test_fresh_desktop_app_refuses_to_run_with_existing_app_data(self):
        (self.root / "config/simplex").mkdir(parents=True)
        with self.assertRaises(build_script.BuildError) as raised:
            self.desktop_run(FakeApp())
        self.assertIn("config/simplex exists", str(raised.exception))
        self.assertTrue((self.root / "config/simplex").exists())

    def test_desktop_app_that_exits_after_its_window_fails(self):
        app = FakeApp()
        calls = iter([None, 1])
        app.poll = lambda: next(calls, 1)
        with self.assertRaises(build_script.BuildError) as raised:
            self.desktop_run(app)
        self.assertIn("after opening its window", str(raised.exception))

    def test_desktop_app_waits_for_its_window_and_the_ready_condition_before_the_screenshot(self):
        events = []
        ready = iter([False, False, True])

        def condition():
            events.append("ready?")
            return next(ready)

        with (
            patch_all("host_platform", return_value=build_script.Platform.MACOS),
            patch_all("screenshot", lambda target, env: events.append(f"screenshot {target.name}")),
            mock.patch.object(build_script.subprocess, "Popen", return_value=FakeApp()),
            mock.patch.object(build_script.os, "killpg"),
            mock.patch.object(build_script.time, "sleep"),
        ):
            screen = build_script.run_desktop(pathlib.Path("app"), {}, {}, self.out, "app.v7.0.3", (condition, "database"))
        self.assertEqual(events, ["ready?", "ready?", "ready?", "screenshot app.v7.0.3.png"])
        self.assertEqual(screen, self.out / "app.v7.0.3.png")
        self.assertTrue((self.out / "app.v7.0.3.log").exists())

    def test_desktop_app_without_the_ready_condition_fails_naming_it(self):
        with (
            patch_all("host_platform", return_value=build_script.Platform.MACOS),
            patch_all("screenshot") as screenshot,
            patch_all("WINDOW_TIMEOUT_SECONDS", 0),
            mock.patch.object(build_script.subprocess, "Popen", return_value=FakeApp()),
            mock.patch.object(build_script.os, "killpg") as killpg,
            mock.patch.object(build_script.time, "sleep"),
            self.assertRaises(build_script.BuildError) as raised,
        ):
            build_script.run_desktop(pathlib.Path("app"), {}, {}, self.out, "app", (lambda: False, "database with its key"))
        self.assertIn("no database with its key", str(raised.exception))
        screenshot.assert_not_called()
        killpg.assert_called_once()

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
            patch_all("open_android_old_database", lambda out, work: opened.append(("open",))),
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
        self.assertEqual(opened[opened.index("-k") + 1 :], ["old-release-key", "--user-display-name", "alice", "-e", "/groups", "-y"])
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

    def test_desktop_old_database_is_where_the_app_opens_it_and_removed_after(self):
        with desktop_env(self.root), patch_all("host_platform", return_value=build_script.Platform.LINUX):
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
            desktop_env(self.root),
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
            desktop_env(self.root),
            patch_all("run", fake_run),
            patch_all("host_platform", return_value=build_script.Platform.MACOS),
            patch_all("install_desktop", return_value=(self.root / "app", {})),
            patch_all("window_open", return_value=True),
            patch_all("screenshot"),
            mock.patch.object(build_script.subprocess, "Popen", return_value=FakeApp()),
            mock.patch.object(build_script.os, "killpg"),
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
        '<node text="Enter passphrase…" bounds="[100,500][980,600]" />'
        '<node text="Save passphrase and open chat" bounds="[300,700][780,800]" />'
        '<node text="Upgrade and open chat" bounds="[300,900][780,1000]" /></hierarchy>'
        "UI hierchary dumped to: /dev/tty"
    )

    def test_android_node_centre_from_uiautomator_dump(self):
        self.assertEqual(build_script.android_node_centre(self.ANDROID_SCREEN, "Upgrade and"), (540, 950))
        self.assertIsNone(build_script.android_node_centre(self.ANDROID_SCREEN, "Open chat"))

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
        screens = iter([self.ANDROID_SCREEN] * 5 + [chat_list])
        recorder = Recorder()
        with (
            patch_all("run", recorder),
            patch_all("android_screen", lambda: next(screens)),
            mock.patch.object(build_script.time, "sleep"),
        ):
            build_script.open_android_old_database(self.out, self.root)
        self.assertEqual(
            recorder.commands,
            [
                ["adb", "shell", "input", "tap", "540", "550"],
                ["adb", "shell", "input", "text", "old-release-key"],
                ["adb", "shell", "input", "tap", "540", "750"],
                ["adb", "shell", "input", "tap", "540", "950"],
            ],
        )

    def test_android_app_stuck_on_the_old_database_leaves_its_screen(self):
        recorder = Recorder()
        with (
            patch_all("run", recorder),
            patch_all("android_screen", return_value=self.ANDROID_SCREEN.replace("Upgrade and open chat", "Opening")),
            patch_all("emulator_screenshot") as screenshot,
            patch_all("ANDROID_FIRST_SCREEN_SECONDS", 0),
            mock.patch.object(build_script.time, "sleep"),
            self.assertRaises(build_script.BuildError),
        ):
            build_script.open_android_old_database(self.out, self.root)
        self.assertIn("Save passphrase", (self.out / "android-old-database-screen.txt").read_text())
        screenshot.assert_called_once_with(self.out / "android-old-database.png", self.root)

class DesktopUpgradeTest(unittest.TestCase):
    """The previous release's app makes its database, and the new package, installed over it, must migrate it."""

    PACKAGE = "simplex-desktop-ubuntu-22_04-x86_64.deb"
    # 32 bytes in base64, as DatabaseUtils.randomDatabasePassword makes the key
    KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    # as java.util.Properties.store writes them, with = escaped
    SETTINGS = (
        "#\n#Sat Oct 31 17:14:59 UTC 2026\nCurrentThemeIds={}\n"
        "EncryptedDBPassphrase=QUFFQ0F3UUZCZ2NJQ1FvTERBME9EeEFSRWhNVUZSWVhHQmthR3h3ZEhoOD0\\=\n"
        "LastMigratedVersionCode=381\nStoreDBPassphrase=true\n"
    )
    NO_KEY_SETTINGS = "#\nLastMigratedVersionCode=381\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.config, self.data = self.root / "config/simplex", self.root / "data/simplex"
        self.package = self.root / "new" / self.PACKAGE
        self.package.parent.mkdir()
        self.package.write_bytes(b"new deb")
        self.cli = self.root / "simplex-chat-ubuntu-22_04-x86_64"
        self.cli.write_bytes(b"cli")

    def tearDown(self):
        self.tmp.cleanup()

    def make_desktop_database(self, settings=SETTINGS):
        self.data.mkdir(parents=True, exist_ok=True)
        for suffix in build_script.DATABASE_SUFFIXES:
            (self.data / f"simplex_v1{suffix}").write_bytes(b"encrypted")
        self.config.mkdir(parents=True, exist_ok=True)
        (self.config / "settings.properties").write_text(settings)

    def test_java_properties_unescape_keys_and_values_and_skip_comments(self):
        text = "#\n#Sat Oct 31 17:14:59 UTC 2026\nkey\\=1=value\\=\\=\nother:x\\:y\n\n!comment=no\n"
        self.assertEqual(build_script.java_properties(text), {"key=1": "value==", "other": "x:y"})

    def test_stored_key_is_decoded_from_the_settings_the_app_wrote(self):
        self.make_desktop_database()
        self.assertEqual(build_script.desktop_stored_key(self.config), self.KEY)

    def test_no_stored_key_without_settings_or_without_the_key(self):
        self.assertIsNone(build_script.desktop_stored_key(self.config))
        self.make_desktop_database(self.NO_KEY_SETTINGS)
        self.assertIsNone(build_script.desktop_stored_key(self.config))

    def test_database_is_stored_once_both_files_and_the_key_exist(self):
        self.assertFalse(build_script.desktop_database_stored(self.config, self.data))
        self.make_desktop_database(self.NO_KEY_SETTINGS)
        self.assertFalse(build_script.desktop_database_stored(self.config, self.data))
        (self.config / "settings.properties").write_text(self.SETTINGS)
        (self.data / "simplex_v1_agent.db").unlink()
        self.assertFalse(build_script.desktop_database_stored(self.config, self.data))
        (self.data / "simplex_v1_agent.db").write_bytes(b"encrypted")
        self.assertTrue(build_script.desktop_database_stored(self.config, self.data))

    def upgrade(self, cli_output="Current user: alice\nyou have no groups\n", cli_returncode=0, package=None):
        """Runs test_desktop_upgrade with stand-ins: the old app makes its database while it runs, the new app keeps it."""
        self.events = events = []
        recorder = Recorder()

        def fake_fetch(url, digest, target):
            events.append(("fetch", url, digest, target.parent.name))
            target.write_bytes(b"old deb")
            return target

        def fake_install(package, work):
            events.append(("install", package.read_bytes()))
            return work / "simplex", {}

        def fake_run_desktop(app, app_env, env, out, name, ready=None):
            events.append(("run", name, ready[1] if ready else None))
            if ready:
                self.assertFalse(ready[0]())
                self.make_desktop_database()
                self.assertTrue(ready[0]())
            return out / f"{name}.png"

        def fake_run(args, input=None, check=True, capture=False, **kwargs):
            recorder(args, input=input, check=check, capture=capture)
            return mock.Mock(returncode=cli_returncode, stdout=cli_output)

        with (
            desktop_env(self.root),
            patch_all("host_platform", return_value=build_script.Platform.MACOS),
            patch_all("fetch_pinned", fake_fetch),
            patch_all("install_desktop", fake_install),
            patch_all("run_desktop", fake_run_desktop),
            patch_all("uninstall_desktop", lambda package, work: events.append(("uninstall", package))),
            patch_all("run", fake_run),
        ):
            build_script.test_desktop_upgrade(package or self.package, self.out, self.cli)
        return events, recorder.commands

    def test_new_package_installs_over_the_old_app_and_its_database_opens_without_migrations(self):
        events, commands = self.upgrade()
        self.assertEqual(
            events,
            [
                (
                    "fetch",
                    f"https://github.com/simplex-chat/simplex-chat/releases/download/v7.0.3/{self.PACKAGE}",
                    "21be1e78c1089c01567ccfec58cdf3e72a2733aa232c5288b8f155e6c7caba0b",
                    "v7.0.3",
                ),
                ("install", b"old deb"),
                ("run", f"{self.PACKAGE}.v7.0.3", "database with its key in the app's settings"),
                ("install", b"new deb"),
                ("run", f"{self.PACKAGE}.upgrade", None),
                ("uninstall", self.package),
            ],
        )
        (check,) = commands
        self.assertEqual(check[0], str(self.cli))
        self.assertEqual(check[1:], ["-d", str(self.data / "simplex_v1"), "-k", self.KEY, "--user-display-name", "alice", "-e", "/groups"])
        self.assertEqual((self.out / f"{self.PACKAGE}.upgrade.database.txt").read_text(), "Current user: alice\nyou have no groups\n")
        self.assertFalse(self.data.exists())
        self.assertFalse(self.config.exists())

    def test_database_that_the_new_cli_cannot_open_fails_and_the_app_is_uninstalled(self):
        with self.assertRaises(build_script.BuildError) as raised:
            self.upgrade(cli_output="Error opening database: ErrorMigration\n", cli_returncode=1)
        self.assertIn("did not open the v7.0.3 database", str(raised.exception))
        self.assertEqual(self.events[-1], ("uninstall", self.package))
        self.assertFalse(self.data.exists())

    def test_upgrade_needs_the_cli_that_checks_the_database(self):
        with self.assertRaises(build_script.BuildError) as raised:
            build_script.test_desktop_upgrade(self.package, self.out, None)
        self.assertIn("needs the CLI", str(raised.exception))

    def test_package_without_a_pinned_old_release_is_rejected(self):
        with self.assertRaises(build_script.BuildError) as raised:
            self.upgrade(package=self.root / "simplex-desktop-ubuntu-20_04-x86_64.deb")
        self.assertIn("no v7.0.3 package is pinned", str(raised.exception))

    def test_upgrade_refuses_to_run_with_existing_app_data(self):
        self.make_desktop_database()
        with self.assertRaises(build_script.BuildError) as raised:
            self.upgrade()
        self.assertIn("exists", str(raised.exception))
        self.assertTrue((self.data / "simplex_v1_chat.db").exists())

    def test_every_pinned_old_package_has_a_release_file_name_and_a_sha256(self):
        packages = r"ubuntu-2[24]_04-(x86_64|aarch64)\.deb|(x86_64|aarch64)\.AppImage|macos-(x86_64|aarch64)\.dmg|windows-x86_64\.msi"
        self.assertEqual(len(build_script.OLD_RELEASE_DESKTOP_SHA256), 9)
        for name, digest in build_script.OLD_RELEASE_DESKTOP_SHA256.items():
            self.assertRegex(name, f"^simplex-desktop-({packages})$")
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
