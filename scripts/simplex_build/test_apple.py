import os
import pathlib
import tempfile
import unittest
import zipfile
from unittest import mock

from .testing import XCODE_DEVELOPER, Recorder, build_script, patch_all

PINNED = f"Xcode {build_script.XCODE_VERSION}\nBuild version {build_script.XCODE_BUILD}\n"


class XcodeTest(unittest.TestCase):
    def test_version_and_build_are_read_from_xcodebuild(self):
        self.assertEqual(build_script.xcode_version("Xcode 26.6\nBuild version 17F113\n"), ("26.6", "17F113"))
        with self.assertRaises(build_script.BuildError):
            build_script.xcode_version("xcode-select: error: tool 'xcodebuild' requires Xcode\n")

    def test_the_pinned_xcode_is_accepted_and_its_developer_directory_returned(self):
        recorder = Recorder(answers={"xcodebuild -version": PINNED, "xcrun --find": f"{XCODE_DEVELOPER}/usr/bin/xcodebuild\n"})
        with patch_all("run", recorder), patch_all("log"), patch_all("add_summary") as summary:
            self.assertEqual(build_script.require_xcode(), XCODE_DEVELOPER)
        self.assertIn(f"Xcode {build_script.XCODE_VERSION} ({build_script.XCODE_BUILD})", summary.call_args.args[0])

    def test_another_xcode_fails_and_names_both_versions(self):
        recorder = Recorder(answers={"xcodebuild -version": "Xcode 26.6\nBuild version 17F113\n"})
        with patch_all("run", recorder), self.assertRaises(build_script.BuildError) as raised:
            build_script.require_xcode()
        self.assertIn("Xcode 26.6 (17F113)", str(raised.exception))
        self.assertIn(f"Xcode {build_script.XCODE_VERSION} ({build_script.XCODE_BUILD})", str(raised.exception))
        self.assertIn("DEVELOPER_DIR", str(raised.exception))

    def test_another_build_of_the_pinned_version_fails(self):
        recorder = Recorder(answers={"xcodebuild -version": f"Xcode {build_script.XCODE_VERSION}\nBuild version 17C1\n"})
        with patch_all("run", recorder), self.assertRaises(build_script.BuildError):
            build_script.require_xcode()


def current_umask():
    umask = os.umask(0o077)
    os.umask(umask)
    return umask


class BuildRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.source = self.root / "checkout"
        (self.source / "apps").mkdir(parents=True)
        (self.source / "apps/file").write_text("source")
        (self.source / "link").symlink_to("apps/file")
        (self.source / "apps/private").write_text("")
        (self.source / "apps/private").chmod(0o600)
        (self.source / "apps/script.sh").write_text("")
        (self.source / "apps/script.sh").chmod(0o700)
        self.fixed = self.root / "fixed"
        self.environment = {"TZ": "Europe/London", "LC_ALL": "de_DE.UTF-8"}

    def tearDown(self):
        self.tmp.cleanup()

    def patched(self):
        return patch_all("BUILD_ROOT", self.fixed)

    def test_the_checkout_is_copied_and_built_with_the_release_settings_then_removed(self):
        umask = current_umask()
        with self.patched(), mock.patch.dict(os.environ, self.environment):
            os.environ.pop("SDKROOT", None)
            with build_script.build_root(self.source, {"SDKROOT": "/sdk"}) as root:
                self.assertEqual(root, build_script.BuildRoot(self.fixed / "simplex-chat", self.fixed / "work"))
                self.assertEqual((root.source / "apps/file").read_text(), "source")
                self.assertEqual(os.readlink(root.source / "link"), "apps/file")
                modes = {name: (root.source / name).stat().st_mode & 0o777 for name in ("apps", "apps/private", "apps/script.sh")}
                self.assertEqual(modes, {"apps": 0o755, "apps/private": 0o644, "apps/script.sh": 0o755})
                self.assertEqual(list(root.work.iterdir()), [])
                self.assertEqual(os.environ["CABAL_DIR"], str(self.fixed / "cabal"))
                self.assertEqual((os.environ["LC_ALL"], os.environ["TZ"], os.environ["SDKROOT"]), ("en_US.UTF-8", "UTC", "/sdk"))
                self.assertEqual(current_umask(), 0o022)
            self.assertFalse(self.fixed.exists())
            self.assertEqual({name: os.environ[name] for name in self.environment}, self.environment)
            self.assertNotIn("SDKROOT", os.environ)
        self.assertEqual(current_umask(), umask)
        self.assertEqual((self.source / "apps/file").read_text(), "source")

    def test_a_failed_build_restores_the_settings_and_removes_the_copy(self):
        umask = current_umask()
        with (
            self.patched(),
            mock.patch.dict(os.environ, self.environment),
            self.assertRaises(build_script.BuildError),
            build_script.build_root(self.source, {}),
        ):
            raise build_script.BuildError("failed")
        self.assertFalse(self.fixed.exists())
        self.assertEqual(current_umask(), umask)

    def test_an_earlier_build_is_replaced(self):
        (self.fixed / "simplex-chat/stale").mkdir(parents=True)
        (self.fixed / "work").mkdir()
        (self.fixed / "work/old").write_text("")
        with self.patched(), build_script.build_root(self.source, {}) as root:
            self.assertFalse((root.source / "stale").exists())
            self.assertEqual(list(root.work.iterdir()), [])

    def test_a_checkout_in_the_build_root_is_rejected(self):
        inside = self.fixed / "simplex-chat"
        inside.mkdir(parents=True)
        with self.patched(), self.assertRaises(build_script.BuildError), build_script.build_root(inside, {}):
            pass
        self.assertTrue(inside.is_dir())


USER_TEMP = "/var/folders/n6/hn37q6pj7sj_9dmqqc2113800000gn/T/"


USER_CACHE = "/var/folders/n6/hn37q6pj7sj_9dmqqc2113800000gn/C/"


class MachinePathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def paths(self, developer, home="/Users/runner"):
        recorder = Recorder(answers={"DARWIN_USER_TEMP_DIR": USER_TEMP, "DARWIN_USER_CACHE_DIR": USER_CACHE})
        with patch_all("run", recorder), mock.patch.dict(os.environ, {"HOME": home, "TMPDIR": USER_TEMP}):
            return build_script.machine_paths(pathlib.Path("/Users/runner/work/simplex-chat/simplex-chat"), developer)

    def test_the_checkout_home_temporary_directories_and_xcode_are_paths_of_the_machine(self):
        self.assertEqual(
            self.paths(XCODE_DEVELOPER),
            [
                "/Applications/Xcode_26.3.app",
                "/Users/runner",
                "/Users/runner/work/simplex-chat/simplex-chat",
                USER_CACHE.rstrip("/"),
                USER_TEMP.rstrip("/"),
            ],
        )

    def test_xcode_in_its_usual_place_and_a_home_of_the_root_directory_are_not(self):
        paths = self.paths(build_script.CANONICAL_DEVELOPER_DIR, home="/")
        self.assertEqual(paths, ["/Users/runner/work/simplex-chat/simplex-chat", USER_CACHE.rstrip("/"), USER_TEMP.rstrip("/")])

    def test_paths_are_found_in_files_and_in_the_entries_of_jars(self):
        app = self.root / "SimpleX.app/Contents"
        (app / "MacOS").mkdir(parents=True)
        (app / "MacOS/SimpleX").write_bytes(b"\0/Users/runner/.cabal/bin\0")
        (app / "MacOS/clean").write_bytes(b"/Users/other\0")
        (app / "link").symlink_to("MacOS/SimpleX")
        with zipfile.ZipFile(app / "app.jar", "w", zipfile.ZIP_DEFLATED) as jar:
            jar.writestr("chat/simplex/Paths.class", b"\x01/var/folders/n6/x/T/kotlin\x01")
        cli = self.root / "simplex-chat"
        cli.write_bytes(b"/Users/runner/work/simplex-chat/simplex-chat/dist-newstyle")
        resources = app / "app/resources/vlc"
        resources.mkdir(parents=True)
        (resources / "libvlccore.dylib").write_bytes(b"/Users/runner/work/vlc/vlc/src")
        paths = ["/Users/runner", "/var/folders/n6"]
        found = build_script.find_machine_paths([cli, self.root / "SimpleX.app"], paths, skip=[app / "app/resources"])
        self.assertEqual(
            found,
            [
                f"{cli}: /Users/runner",
                f"{app / 'MacOS/SimpleX'}: /Users/runner",
                f"{app / 'app.jar'}!chat/simplex/Paths.class: /var/folders/n6",
            ],
        )

    def test_found_paths_fail_and_are_listed(self):
        build_script.check_machine_paths([])
        with self.assertRaises(build_script.BuildError) as raised:
            build_script.check_machine_paths(["a: /Users/runner", "b: /Users/runner"])
        self.assertIn("a: /Users/runner\nb: /Users/runner", str(raised.exception))
