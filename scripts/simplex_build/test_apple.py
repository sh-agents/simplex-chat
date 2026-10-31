import unittest

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
