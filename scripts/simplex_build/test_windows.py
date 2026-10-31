import pathlib
import tempfile
import unittest
import zipfile
from unittest import mock

from .testing import Recorder, build_script, patch_all


class WindowsLibTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.source = self.root / "src"
        (self.source / "src/Simplex/Chat").mkdir(parents=True)
        (self.source / "src/Simplex/Chat/Mobile.hs").write_text('foreign export ccall "chat_send_cmd" c :: X\n')
        (self.source / "libsimplex.dll.def").write_text("chat_send_cmd\n")
        (self.source / "flake.nix").write_text('"chat_send_cmd"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_ghc_settings_stop_using_lld(self):
        libdir = self.root / "ghc-lib"
        libdir.mkdir()
        (libdir / "settings").write_text('[("ld command", "ld.lld.exe"), ("x", "ld.lld.exe")]\n')
        with patch_all("run", Recorder(answers={"print-libdir": f"{libdir}\n"})):
            build_script.disable_ghc_lld()
        self.assertEqual((libdir / "settings").read_text(), '[("ld command", "abracadabra.exe"), ("x", "ld.lld.exe")]\n')

    def test_library_links_the_pinned_openssl(self):
        openssl = self.root / "openssl-3.5.9"
        openssl.mkdir()
        (openssl / "libcrypto-3-x64.dll").write_bytes(b"crypto")
        (self.source / "libsimplex.dll").write_bytes(b"simplex")
        recorder = Recorder()
        with patch_all("run", recorder), patch_all("disable_ghc_lld") as disable:
            libs = build_script.build_windows_lib(self.source, build_script.OpenSsl(openssl / "include", openssl))
        disable.assert_called_once()
        cabal_local = (self.source / "cabal.project.local").read_text()
        self.assertIn(f"extra-include-dirs: {openssl.as_posix()}/include", cabal_local)
        options = (
            f"-shared -threaded -optl-Wl,--no-insert-timestamp -optl-L{openssl.as_posix()} -optl-lcrypto-3-x64 "
            "-o libsimplex.dll libsimplex.dll.def"
        )
        self.assertIn(f"ghc-options: {options}", cabal_local)
        self.assertIn("simplex-chat +client_library", recorder.commands[-1])
        self.assertEqual(sorted(p.name for p in libs.iterdir()), ["libcrypto-3-x64.dll", "libsimplex.dll"])

    def test_vlc_plugins_get_the_jar_time_before_the_cache(self):
        libs, work = self.root / "libs", self.root / "work"
        libs.mkdir()
        work.mkdir()
        archive = work / "vlc-win-x86_64.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("vlc-3.0.23/libvlc.dll", b"vlc")
            z.writestr("vlc-3.0.23/plugins/audio/libplugin.dll", b"p")
        times = []

        def cache_gen(args, **kwargs):
            times.append((work / "vlc/vlc-3.0.23/plugins/audio/libplugin.dll").stat().st_mtime)
            return mock.Mock(returncode=0, stdout="")

        with patch_all("run", cache_gen), patch_all("fetch_pinned", lambda u, d, t: t):
            build_script.add_vlc_windows(libs, work)
        self.assertEqual(times, [0])
        self.assertTrue((libs / "vlc/libvlc.dll").exists())
        self.assertEqual((libs / "vlc/vlc/plugins/audio/libplugin.dll").stat().st_mtime, 0)


class MsiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.source = self.root / "src"
        self.work = self.root / "work"
        self.work.mkdir()
        self.msi = self.root / "SimpleX-7.1.7.msi"
        self.msi.write_bytes(b"msi")

    def tearDown(self):
        self.tmp.cleanup()

    def persisting_summary(self, recorder, scripts):
        """Stands in for run(): powershell writes the package code it is given into the msi, as Persist does."""

        def fake_run(args, **kwargs):
            args = [str(a) for a in args]
            if args[0] == "powershell":
                scripts.append(pathlib.Path(args[args.index("-File") + 1]).read_text())
                self.msi.write_bytes(b"msi " + args[-2].encode())
            return recorder(args, **kwargs)

        return fake_run

    def test_summary_script_gets_the_msi_package_code_and_epoch(self):
        recorder, scripts = Recorder(), []
        with patch_all("run", self.persisting_summary(recorder, scripts)):
            build_script.set_msi_summary(self.msi, "{A}", self.work)
        script = str(self.work / "msi-summary.ps1")
        msi = str(pathlib.PureWindowsPath(self.msi))
        self.assertEqual(
            recorder.commands,
            [["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, msi, "{A}", "1704067200"]],
        )
        self.assertEqual(scripts, [build_script.MSI_SUMMARY_SCRIPT])
        self.assertIn("$RevisionNumber, $CreateTime, $LastSaveTime = 9, 12, 13", scripts[0])
        self.assertEqual(list(self.work.iterdir()), [])

    def test_package_code_is_a_uuid_of_the_content(self):
        code = build_script.msi_package_code(self.msi)
        self.assertRegex(code, r"^\{[0-9A-F]{8}-[0-9A-F]{4}-5[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}\}$")
        self.assertEqual(build_script.msi_package_code(self.msi), code)
        self.msi.write_bytes(b"msi 2")
        self.assertNotEqual(build_script.msi_package_code(self.msi), code)

    def test_package_code_is_derived_from_the_msi_with_the_placeholder(self):
        recorder, scripts = Recorder(), []
        with patch_all("run", self.persisting_summary(recorder, scripts)):
            build_script.normalize_msi_summary(self.msi, self.work)
        self.msi.write_bytes(b"msi " + build_script.MSI_PLACEHOLDER_PACKAGE_CODE.encode())
        expected = build_script.msi_package_code(self.msi)
        self.assertEqual([command[-2] for command in recorder.commands], [build_script.MSI_PLACEHOLDER_PACKAGE_CODE, expected])

    def test_msi_packs_the_app_image_without_skiko_runtime_at_the_epoch(self):
        image = self.source / build_script.DESKTOP_RELEASE_DIR / "app/SimpleX"
        release_msi = self.source / build_script.DESKTOP_RELEASE_DIR / "msi/SimpleX-7.1.7.msi"
        recorder = Recorder()
        seen = {}

        def fake_run(args, **kwargs):
            args = [str(a) for a in args]
            if args[-1] == "createDistributable" and "-x" not in args:
                (image / "app").mkdir(parents=True)
                (image / "SimpleX.exe").write_bytes(b"exe")
                (image / "app/desktop-jvm-7.1.7-854b.jar").write_bytes(b"jar")
                (image / "app/skiko-awt-runtime-windows-x64-0.9.4.2-1f63.jar").write_bytes(b"jar")
                (image / "app/SimpleX.cfg").write_text("app.classpath=$APPDIR\\skiko-awt-runtime-windows-x64-0.9.4.2-1f63.jar\n")
            if "packageMsi" in args:
                files = list(image.rglob("*"))
                seen["names"] = sorted(p.relative_to(image).as_posix() for p in files)
                seen["times"] = {p.stat().st_mtime for p in [image, *files]}
                release_msi.parent.mkdir(parents=True)
                release_msi.write_bytes(b"msi")
            return recorder(args, **kwargs)

        with patch_all("run", fake_run), patch_all("normalize_msi_summary") as normalize:
            msi = build_script.package_msi(self.source, self.work)
        self.assertEqual(msi, release_msi)
        normalize.assert_called_once_with(release_msi, self.work)
        gradle = [str(self.source / "apps/multiplatform/gradlew.bat"), "-Psimplex.assets.dir=../../assets"]
        self.assertEqual(recorder.commands, [[*gradle, "createDistributable"], [*gradle, "packageMsi", "-x", "createDistributable"]])
        self.assertEqual(recorder.envs, [{"LDFLAGS": "-Wl,--no-insert-timestamp"}] * 2)
        self.assertEqual(seen["names"], ["SimpleX.exe", "app", "app/SimpleX.cfg", "app/desktop-jvm-7.1.7-854b.jar"])
        self.assertEqual(seen["times"], {1704067200})
        self.assertEqual((image / "app/SimpleX.cfg").read_text(), "")
