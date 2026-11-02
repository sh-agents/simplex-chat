import os
import pathlib
import tempfile
import unittest
from unittest import mock

from .testing import build_script


class FixedBuildRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.source = self.root / "checkout"
        self.source.mkdir()
        (self.source / "file").write_text("source")

    def tearDown(self):
        self.tmp.cleanup()

    def test_any_directory_holds_the_copy_with_only_the_given_variables(self):
        root = build_script.BuildRoot.at(self.root / "C" / "simplex-chat-build")
        self.assertEqual(root.directory, self.root / "C" / "simplex-chat-build")
        self.assertEqual((root.source.name, root.work.name, root.tmp.name), ("simplex-chat", "work", "tmp"))
        with mock.patch.dict(os.environ, {"TMP": "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp"}):
            os.environ.pop("TEMP", None)
            with build_script.fixed_build_root(self.source, root, {"TMP": str(root.tmp), "TEMP": str(root.tmp)}) as built:
                self.assertEqual(built, root)
                self.assertEqual((root.source / "file").read_text(), "source")
                self.assertEqual((os.environ["TMP"], os.environ["TEMP"]), (str(root.tmp), str(root.tmp)))
            self.assertEqual(os.environ["TMP"], "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp")
            self.assertNotIn("TEMP", os.environ)
        self.assertFalse(root.directory.exists())


class FindMachinePathsTest(unittest.TestCase):
    def test_paths_are_found_in_each_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "SimpleX.exe").write_bytes(b"\0" + "C:\\Users\\runneradmin\\x".encode("utf-16-le") + b"\0")
            (root / "simplex-chat.exe").write_bytes(b"D:/a/simplex-chat/simplex-chat/dist-openssl\0")
            paths = ["C:\\Users\\runneradmin", "D:/a/simplex-chat/simplex-chat"]
            self.assertEqual(
                build_script.find_machine_paths([root], paths),
                [f"{root / 'simplex-chat.exe'}: D:/a/simplex-chat/simplex-chat"],
            )
            self.assertEqual(
                build_script.find_machine_paths([root], paths, encodings=("utf-8", "utf-16-le")),
                [f"{root / 'SimpleX.exe'}: C:\\Users\\runneradmin", f"{root / 'simplex-chat.exe'}: D:/a/simplex-chat/simplex-chat"],
            )
