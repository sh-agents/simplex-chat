import base64
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from .testing import FakeOtool, Recorder, build_script, otool_big_l, otool_l, patch_all


class MacLibTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_otool_parsing(self):
        fake = FakeOtool(
            big_l={"lib.dylib": otool_big_l("@rpath/lib.dylib", ["@rpath/libHSbase.dylib", "/usr/lib/libSystem.B.dylib"])},
            small_l={"lib.dylib": otool_l(["@loader_path/../lib/x", "/Users/runner/ghc"], ["/w/openssl/lib/libcrypto.3.dylib"])},
        )
        lib = pathlib.Path("lib.dylib")
        with patch_all("run", fake):
            self.assertEqual(build_script.rpath_dependencies(lib), ["libHSbase.dylib"])
            self.assertEqual(build_script.rpaths(lib), ["@loader_path/../lib/x", "/Users/runner/ghc"])
            self.assertEqual(build_script.libcrypto_path(lib), "/w/openssl/lib/libcrypto.3.dylib")

    def test_dependencies_are_copied_once_with_loader_path_rpaths(self):
        ghc = self.root / "ghc"
        (ghc / "lib").mkdir(parents=True)
        build = self.root / "build"
        deps = build / "deps"
        deps.mkdir(parents=True)
        (build / "libsimplex.dylib").write_bytes(b"simplex")
        (ghc / "lib/libHSbase.dylib").write_bytes(b"base")
        (ghc / "lib/libHSrts.dylib").write_bytes(b"rts")
        fake = FakeOtool(
            big_l={
                "libsimplex.dylib": otool_big_l("@rpath/libsimplex.dylib", ["@rpath/libHSbase.dylib", "@rpath/libHSrts.dylib"]),
                "libHSbase.dylib": otool_big_l("@rpath/libHSbase.dylib", ["@rpath/libHSrts.dylib"]),
                "libHSrts.dylib": otool_big_l("@rpath/libHSrts.dylib", []),
            },
            small_l={
                "libsimplex.dylib": otool_l([str(ghc / "lib")]),
                "libHSbase.dylib": otool_l(["@loader_path/../lib"]),
                "libHSrts.dylib": otool_l([]),
            },
        )
        with patch_all("run", fake):
            build_script.copy_mac_dependencies(build / "libsimplex.dylib", deps, ghc, set())
        self.assertEqual(sorted(p.name for p in deps.iterdir()), ["libHSbase.dylib", "libHSrts.dylib", "libsimplex.dylib"])
        add_rpath = [c for c in fake.commands if c[:2] == ["install_name_tool", "-add_rpath"]]
        self.assertEqual(add_rpath, [["install_name_tool", "-add_rpath", "@loader_path", str(deps / "libHSbase.dylib")]])

    def test_libcrypto_is_bundled_and_local_rpaths_deleted(self):
        libs = self.root / "libs"
        libs.mkdir()
        openssl = self.root / "openssl/lib"
        openssl.mkdir(parents=True)
        (openssl / "libcrypto.3.dylib").write_bytes(b"crypto")
        for name in ("libHSsmplxmq-6.5.dylib", "libsimplex.dylib", "libHSdrct-2.3.dylib", "libffi.dylib"):
            (libs / name).write_bytes(b"x")
        linked = str(openssl / "libcrypto.3.dylib")
        fake = FakeOtool(
            small_l={
                "libHSsmplxmq-6.5.dylib": otool_l(["/Users/runner/ghc/lib"], [linked]),
                "libHSdrct-2.3.dylib": otool_l([], [linked]),
                "libsimplex.dylib": otool_l(["@loader_path"]),
            }
        )
        with patch_all("run", fake):
            build_script.bundle_mac_libcrypto(libs, build_script.Database.SQLITE)
        self.assertEqual((libs / "libcrypto.3.0.dylib").read_bytes(), b"crypto")
        changes = [c[-1] for c in fake.commands if c[:2] == ["install_name_tool", "-change"]]
        self.assertEqual(sorted(pathlib.Path(c).name for c in changes), ["libHSdrct-2.3.dylib", "libHSsmplxmq-6.5.dylib"])
        self.assertIn(["install_name_tool", "-id", "libcrypto.3.0.dylib", str(libs / "libcrypto.3.0.dylib")], fake.commands)
        self.assertIn(["install_name_tool", "-delete_rpath", "/Users/runner/ghc/lib", str(libs / "libHSsmplxmq-6.5.dylib")], fake.commands)

    def test_libraries_still_pointing_to_local_directories_fail(self):
        libs = self.root / "libs"
        libs.mkdir()
        (libs / "libHSsmplxmq-6.5.dylib").write_bytes(b"x")
        (libs / "libffi.dylib").write_bytes(b"x")
        crypto = self.root / "libcrypto.3.dylib"
        crypto.write_bytes(b"c")
        fake = FakeOtool(small_l={"libHSsmplxmq-6.5.dylib": otool_l([], [str(crypto), "/opt/homebrew/lib/libgmp.dylib"])})
        with patch_all("run", fake), self.assertRaises(build_script.BuildError) as raised:
            build_script.bundle_mac_libcrypto(libs, build_script.Database.SQLITE)
        self.assertIn("libHSsmplxmq-6.5.dylib", str(raised.exception))

    def test_vlc_gets_rpaths_and_its_plugin_cache(self):
        libs, work = self.root / "libs", self.root / "work"
        libs.mkdir()
        work.mkdir()
        macos = work / "vlc/VLC.app/Contents/MacOS"

        def fake_run(args, **kwargs):
            if args[0] == "unzip":
                (macos / "lib").mkdir(parents=True)
                (macos / "plugins").mkdir()
                (macos / "lib/libvlc.dylib").write_bytes(b"vlc")
                (macos / "plugins/libaudio_plugin.dylib").write_bytes(b"p")
            return recorder(args, **kwargs)

        recorder = Recorder()
        with patch_all("run", fake_run), patch_all("fetch_pinned", lambda u, d, t: t):
            build_script.add_vlc_mac(libs, "aarch64", work)
        plugin = str(macos / "plugins/libaudio_plugin.dylib")
        self.assertIn(["install_name_tool", "-add_rpath", "@loader_path/../../", plugin], recorder.commands)
        self.assertIn([str(work / "vlc/vlc-cache-gen"), "plugins"], recorder.commands)
        self.assertTrue((libs / "vlc/libvlc.dylib").exists())
        self.assertTrue((libs / "vlc/vlc/plugins/libaudio_plugin.dylib").exists())

    def test_keychain_is_searched_while_signing_and_removed_after(self):
        work = self.root
        recorder = Recorder(answers={"list-keychains": '    "/Users/r/login.keychain-db"\n'})
        environ = {"APPLE_SIMPLEX_SIGNING_KEYCHAIN": base64.b64encode(b"keychain").decode()}
        with patch_all("run", recorder), mock.patch.dict(os.environ, environ):
            with build_script.apple_keychain(work) as keychain:
                self.assertEqual(keychain.read_bytes(), b"keychain")
            self.assertFalse(keychain.exists())
        searches = [c for c in recorder.commands if c[:3] == ["security", "list-keychains", "-s"]]
        self.assertEqual(searches, [["security", "list-keychains", "-s", "/Users/r/login.keychain-db", str(keychain)], searches[1]])
        self.assertEqual(searches[1], ["security", "list-keychains", "-s", "/Users/r/login.keychain-db"])

    def notarize(self, status):
        """notarize with the service answering status; returns the log titles of its commands."""
        titles = []

        def fake_run(args, **kwargs):
            titles.append(kwargs.get("title"))
            stdout = json.dumps({"id": "abc", "status": status}) if "submit" in [str(a) for a in args] else ""
            return mock.Mock(returncode=0, stdout=stdout)

        environ = {"APPLE_SIMPLEX_NOTARIZATION_APPLE_ID": "id@x", "APPLE_SIMPLEX_NOTARIZATION_PASSWORD": "secret"}
        with patch_all("run", fake_run), mock.patch.dict(os.environ, environ):
            build_script.notarize(self.root / "libs", self.root)
        return titles

    def test_notarization_must_be_accepted_and_hides_the_password(self):
        titles = self.notarize("Accepted")
        self.assertFalse(any(t and "secret" in t for t in titles))
        with self.assertRaises(build_script.BuildError):
            self.notarize("Invalid")

    def test_dmg_signing_properties_are_written_and_removed(self):
        source = self.root / "src"
        multiplatform = source / "apps/multiplatform"
        multiplatform.mkdir(parents=True)
        dmg_dir = source / build_script.DESKTOP_RELEASE_DIR / "dmg"
        seen = []

        def fake_run(args, **kwargs):
            seen.append(([str(a) for a in args], (multiplatform / "local.properties").read_text()))
            dmg_dir.mkdir(parents=True, exist_ok=True)
            (dmg_dir / "SimpleX-7.1.0.dmg").write_bytes(b"dmg")
            return mock.Mock(returncode=0, stdout="")

        environ = {"APPLE_SIMPLEX_NOTARIZATION_APPLE_ID": "id@x", "APPLE_SIMPLEX_NOTARIZATION_PASSWORD": "secret"}
        with patch_all("run", fake_run), mock.patch.dict(os.environ, environ):
            dmg = build_script.package_dmg(source, self.root / "k.keychain")
        self.assertEqual([args[-1] for args, _ in seen], ["packageDmg", "notarizeDmg"])
        self.assertIn("desktop.mac.notarization.team_id=5NN7GUYB6T\n", seen[0][1])
        self.assertIn(f"desktop.mac.signing.keychain={self.root / 'k.keychain'}\n", seen[0][1])
        self.assertFalse((multiplatform / "local.properties").exists())
        self.assertEqual(dmg.name, "SimpleX-7.1.0.dmg")

    def test_unsigned_dmg_is_only_packaged(self):
        source = self.root / "src"
        (source / "apps/multiplatform").mkdir(parents=True)
        dmg_dir = source / build_script.DESKTOP_RELEASE_DIR / "dmg"
        dmg_dir.mkdir(parents=True)
        (dmg_dir / "SimpleX-7.1.0.dmg").write_bytes(b"dmg")
        recorder = Recorder()
        with patch_all("run", recorder):
            build_script.package_dmg(source, None)
        self.assertEqual([c[-1] for c in recorder.commands], ["packageDmg"])
