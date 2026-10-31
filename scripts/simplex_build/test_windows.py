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
