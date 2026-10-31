import base64
import json
import os
import pathlib
import struct
import tempfile
import unittest
import zipfile
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

    def test_the_path_that_otool_prints_first_is_not_a_local_directory(self):
        libs = self.root / "Users/runner/libs"
        libs.mkdir(parents=True)
        (libs / "libHSsmplxmq-6.5.dylib").write_bytes(b"x")
        (libs / "libffi.dylib").write_bytes(b"x")
        crypto = self.root / "libcrypto.3.dylib"
        crypto.write_bytes(b"c")

        def fake_run(args, **kwargs):
            args = [str(a) for a in args]
            if args[0] == "otool":
                lines = [f"{args[2]}:", "Load command 0", "      cmd LC_ID_DYLIB", "         name @rpath/lib.dylib (offset 24)"]
                if "smplxmq" in args[2] and not any("-change" in c for c in changed):
                    lines.append(f"         name {crypto} (offset 24)")
                return mock.Mock(returncode=0, stdout="\n".join(lines) + "\n")
            changed.append(args)
            return mock.Mock(returncode=0, stdout="")

        changed = []
        with patch_all("run", fake_run):
            build_script.bundle_mac_libcrypto(libs, build_script.Database.SQLITE)
        self.assertTrue((libs / "libcrypto.3.0.dylib").exists())

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
        cached_times = []

        def cache_gen(args, **kwargs):
            if str(args[0]).endswith("vlc-cache-gen"):
                cached_times.append((macos / "plugins/libaudio_plugin.dylib").stat().st_mtime)
            return fake_run(args, **kwargs)

        with patch_all("run", cache_gen), patch_all("fetch_pinned", lambda u, d, t: t):
            build_script.add_vlc_mac(libs, "aarch64", work)
        plugin = str(macos / "plugins/libaudio_plugin.dylib")
        self.assertIn(["install_name_tool", "-add_rpath", "@loader_path/../../", plugin], recorder.commands)
        self.assertIn([str(work / "vlc/vlc-cache-gen"), "plugins"], recorder.commands)
        self.assertEqual(cached_times, [int(build_script.SOURCE_DATE_EPOCH)])
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

    def test_app_signing_properties_are_written_and_removed(self):
        source = self.root / "src"
        multiplatform = source / "apps/multiplatform"
        multiplatform.mkdir(parents=True)
        app = source / build_script.DESKTOP_RELEASE_DIR / "app/SimpleX.app"
        seen = []

        def fake_run(args, **kwargs):
            seen.append(([str(a) for a in args], (multiplatform / "local.properties").read_text()))
            app.mkdir(parents=True, exist_ok=True)
            return mock.Mock(returncode=0, stdout="")

        environ = {"APPLE_SIMPLEX_NOTARIZATION_APPLE_ID": "id@x", "APPLE_SIMPLEX_NOTARIZATION_PASSWORD": "secret"}
        with patch_all("run", fake_run), mock.patch.dict(os.environ, environ):
            built = build_script.build_app(source, self.root / "k.keychain")
        self.assertEqual([args[-1] for args, _ in seen], ["createDistributable"])
        self.assertIn("desktop.mac.notarization.team_id=5NN7GUYB6T\n", seen[0][1])
        self.assertIn(f"desktop.mac.signing.keychain={self.root / 'k.keychain'}\n", seen[0][1])
        self.assertFalse((multiplatform / "local.properties").exists())
        self.assertEqual(built, app)

    def test_unsigned_app_is_built_without_properties(self):
        source = self.root / "src"
        (source / "apps/multiplatform").mkdir(parents=True)
        (source / build_script.DESKTOP_RELEASE_DIR / "app/SimpleX.app").mkdir(parents=True)
        recorder = Recorder()
        with patch_all("run", recorder):
            build_script.build_app(source, None)
        self.assertEqual([c[-1] for c in recorder.commands], ["createDistributable"])
        self.assertFalse((source / "apps/multiplatform/local.properties").exists())

    def test_dos_date_of_source_date_epoch(self):
        self.assertEqual(build_script.dos_date_time("1704067200"), struct.pack("<HH", 0, (44 << 9) | (1 << 5) | 1))
        self.assertEqual(build_script.dos_date_time("1704114245"), struct.pack("<HH", (13 << 11) | (4 << 5) | 2, (44 << 9) | (1 << 5) | 1))

    def jar(self, path, date_time):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in (("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n"), ("a/B.class", b"class" * 100)):
                z.writestr(zipfile.ZipInfo(name, date_time), data, zipfile.ZIP_DEFLATED)

    def test_jar_entries_get_the_epoch_date_in_both_headers(self):
        jar = self.root / "a.jar"
        self.jar(jar, (2026, 10, 31, 12, 34, 56))
        build_script.normalize_jar(jar, "1704067200")
        data = jar.read_bytes()
        with zipfile.ZipFile(jar) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual({i.date_time for i in z.infolist()}, {(2024, 1, 1, 0, 0, 0)})
            self.assertEqual(z.read("a/B.class"), b"class" * 100)
        for entry in build_script.central_directory(data):
            self.assertEqual(data[entry.local_offset + 10 : entry.local_offset + 14], build_script.dos_date_time("1704067200"))
        broken = bytearray(data)
        broken[0:4] = b"XXXX"
        jar.write_bytes(broken)
        with self.assertRaises(build_script.BuildError):
            build_script.normalize_jar(jar, "1704067200")

    def app_built_at(self, name, date_time, skiko_listed=1):
        app = self.root / name / "SimpleX.app"
        jars = app / "Contents/app"
        jars.mkdir(parents=True)
        self.jar(jars / f"skiko-awt-runtime-macos-arm64-0.9.4.2-{date_time[4]:02}aa.jar", date_time)
        self.jar(jars / "plain.jar", date_time)
        cfg = "[Application]\n" + f"app.classpath=$APPDIR/skiko-awt-runtime-macos-arm64-0.9.4.2-{date_time[4]:02}aa.jar\n" * skiko_listed
        (jars / "SimpleX.cfg").write_text(cfg + "app.classpath=$APPDIR/plain.jar\napp.mainclass=chat.simplex.desktop.MainKt\n")
        build_script.normalize_app(app)
        return {p.name: p.read_bytes() for p in sorted(jars.iterdir())}

    def test_apps_built_at_other_times_get_the_same_jars_and_classpath(self):
        first = self.app_built_at("a", (2026, 10, 31, 12, 34, 56))
        second = self.app_built_at("b", (2026, 10, 31, 13, 45, 2))
        self.assertEqual(first, second)
        skiko = next(name for name in first if name.startswith("skiko"))
        self.assertRegex(skiko, r"^skiko-awt-runtime-macos-arm64-0\.9\.4\.2-[0-9a-f]{32}\.jar$")
        self.assertIn(f"app.classpath=$APPDIR/{skiko}\n".encode(), first["SimpleX.cfg"])
        self.assertIn(b"app.classpath=$APPDIR/plain.jar\n", first["SimpleX.cfg"])

    def test_a_renamed_jar_that_the_classpath_lists_other_than_once_fails(self):
        for listed in (0, 2):
            with self.subTest(listed=listed), self.assertRaises(build_script.BuildError) as raised:
                self.app_built_at(f"listed-{listed}", (2026, 10, 31, 12, 34, 56), skiko_listed=listed)
            self.assertIn("skiko-awt-runtime-macos-arm64-0.9.4.2-34aa.jar", str(raised.exception))

    def test_app_is_signed_again_as_compose_signed_it(self):
        app = self.root / "SimpleX.app"
        app.mkdir()
        recorder = Recorder()
        with patch_all("run", recorder):
            build_script.sign_app(app, None)
            (app / "Contents/_CodeSignature").mkdir(parents=True)
            build_script.sign_app(app, None)
            build_script.sign_app(app, self.root / "k.keychain")
        preserve = "--preserve-metadata=entitlements,flags,runtime"
        self.assertEqual(
            recorder.commands,
            [
                ["codesign", "--force", preserve, "--sign", "-", str(app)],
                [
                    "codesign",
                    "--force",
                    preserve,
                    "--timestamp",
                    "--sign",
                    build_script.APPLE_IDENTITY,
                    "--keychain",
                    str(self.root / "k.keychain"),
                    str(app),
                ],
            ],
        )

    def test_signed_dmg_is_notarized_itself_and_stapled(self):
        dmg = self.root / "simplex-desktop-macos-aarch64.dmg"
        commands = []

        def fake_run(args, **kwargs):
            args = [str(a) for a in args]
            commands.append(args)
            stdout = json.dumps({"id": "abc", "status": "Accepted"}) if "submit" in args else ""
            return mock.Mock(returncode=0, stdout=stdout)

        environ = {"APPLE_SIMPLEX_NOTARIZATION_APPLE_ID": "id@x", "APPLE_SIMPLEX_NOTARIZATION_PASSWORD": "secret"}
        with patch_all("run", fake_run), mock.patch.dict(os.environ, environ):
            build_script.sign_dmg(dmg, self.root / "k.keychain", self.root)
        self.assertEqual(commands[0][:3], ["codesign", "--timestamp", "--sign"])
        self.assertEqual(commands[1][:5], ["xcrun", "notarytool", "submit", str(dmg), "--wait"])
        self.assertFalse(any(c[0] == "ditto" for c in commands))
        self.assertEqual(commands[-1], ["xcrun", "stapler", "staple", str(dmg)])

    def test_java_home_must_be_set(self):
        with mock.patch.dict(os.environ, {"JAVA_HOME": "/jdk"}):
            self.assertEqual(build_script.java_home(), pathlib.Path("/jdk"))
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(build_script.BuildError):
            build_script.java_home()
