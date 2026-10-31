"""Smoke tests of the release files: each runs as a user installs it, and the apps leave a screenshot."""

import contextlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from .common import (
    BuildError,
    Platform,
    add_summary,
    host_platform,
    log,
    require_tools,
    run,
)

# How long an app may take to open its window, and to draw its first screen after that
WINDOW_TIMEOUT_SECONDS = 180
SETTLE_SECONDS = 20
LINUX_DISPLAY = ":99"
LINUX_SCREEN = "1440x900x24"
# Compose's GPU rendering needs an OpenGL that Xvfb lacks
LINUX_JAVA_OPTIONS = "-Dskiko.renderApi=SOFTWARE"
DESKTOP_WINDOW_TITLE = "SimpleX"
ANDROID_PACKAGE = "chat.simplex.app"
ANDROID_ACTIVITY = f"{ANDROID_PACKAGE}/.MainActivity"
# Screenshots with fewer colours are blank, or show only a background
MIN_SCREENSHOT_COLOURS = 16
WINDOWS_SCREENSHOT = """Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
[System.Drawing.Graphics]::FromImage($bitmap).CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bitmap.Save($args[0], [System.Drawing.Imaging.ImageFormat]::Png)
"""


def record(out: Path, name: str, text: str) -> None:
    (out / f"{name}.txt").write_text(text)
    log(f"{name}: {text.strip().splitlines()[0] if text.strip() else '(no output)'}")


def test_cli(binary: Path, out: Path) -> None:
    """The CLI starts and prints its version."""
    with tempfile.TemporaryDirectory() as tmp:
        runnable = binary
        if host_platform() == Platform.WINDOWS and binary.suffix != ".exe":
            # Windows runs only files named .exe
            runnable = Path(tmp) / f"{binary.name}.exe"
            shutil.copy(binary, runnable)
        runnable.chmod(0o755)
        version = run([runnable, "--version"], capture=True).stdout
    record(out, binary.name, version)
    if "SimpleX Chat" not in version:
        raise BuildError(f"{binary.name} --version printed no SimpleX Chat version: {version!r}")
    add_summary(f"`{binary.name}`: {version.strip()}\n\n")


@contextlib.contextmanager
def linux_display(out: Path) -> Iterator[dict[str, str]]:
    """A virtual display with a window manager, as the apps' users have one."""
    env = {"DISPLAY": LINUX_DISPLAY}
    with open(out / "xvfb.log", "w") as xvfb_log, open(out / "openbox.log", "w") as openbox_log:
        xvfb_command = ["Xvfb", LINUX_DISPLAY, "-screen", "0", LINUX_SCREEN, "-nolisten", "tcp"]
        xvfb = subprocess.Popen(xvfb_command, stdout=xvfb_log, stderr=xvfb_log)
        try:
            geometry = ["xdotool", "getdisplaygeometry"]
            wait_until(lambda: run(geometry, env=env, check=False, capture=True).returncode == 0, 20, "display")
            openbox = subprocess.Popen(["openbox", "--sm-disable"], env={**os.environ, **env}, stdout=openbox_log, stderr=openbox_log)
            try:
                yield env
            finally:
                openbox.terminate()
                openbox.wait()
        finally:
            xvfb.terminate()
            xvfb.wait()


def wait_until(condition: Callable[[], bool], seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise BuildError(f"no {what} after {seconds} s")
        time.sleep(1)


def install_desktop(package: Path, work: Path) -> tuple[Path, dict[str, str]]:
    """The app's executable once installed from package, and the environment it runs with."""
    match package.suffix:
        case ".deb":
            run(["sudo", "apt-get", "install", "-y", "--no-install-recommends", package.resolve()])
            return Path("/opt/simplex/bin/simplex"), {"JAVA_TOOL_OPTIONS": LINUX_JAVA_OPTIONS}
        case ".AppImage":
            package.chmod(0o755)
            # runs the AppImage without FUSE, which CI runners lack
            return package, {"APPIMAGE_EXTRACT_AND_RUN": "1", "JAVA_TOOL_OPTIONS": LINUX_JAVA_OPTIONS}
        case ".dmg":
            mount = work / "dmg"
            run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, package])
            try:
                shutil.copytree(mount / "SimpleX.app", work / "SimpleX.app", symlinks=True)
            finally:
                run(["hdiutil", "detach", mount])
            return work / "SimpleX.app/Contents/MacOS/SimpleX", {}
        case ".msi":
            # msiexec opens only paths with backslashes, and MSYS2's Python joins paths with slashes
            msi, log_file = (str(path).replace("/", "\\") for path in (package.resolve(), work / "msiexec.log"))
            run(["msiexec", "/i", msi, "/qn", "/l*v", log_file])
            return Path(os.environ["PROGRAMFILES"]) / "SimpleX/SimpleX.exe", {}
    raise BuildError(f"{package.name}: expected a .deb, .AppImage, .dmg or .msi")


def window_open(process: subprocess.Popen[bytes], env: Mapping[str, str]) -> bool:
    if process.poll() is not None:
        raise BuildError(f"the app exited with {process.returncode} before opening its window")
    match host_platform():
        case Platform.LINUX:
            search = ["xdotool", "search", "--onlyvisible", "--name", f"^{DESKTOP_WINDOW_TITLE}$"]
            return run(search, env=env, check=False, capture=True).returncode == 0
        case Platform.WINDOWS:
            handle = f"(Get-Process -Id {process.pid}).MainWindowHandle"
            return run(["powershell", "-NoProfile", "-Command", handle], check=False, capture=True).stdout.strip() not in ("", "0")
    # macOS lets only apps with the accessibility permission list other apps' windows
    return True


def screenshot(target: Path, env: Mapping[str, str]) -> None:
    match host_platform():
        case Platform.LINUX:
            run(["import", "-window", "root", target], env=env)
            colours = int(run(["identify", "-format", "%k", target], capture=True).stdout)
            if colours < MIN_SCREENSHOT_COLOURS:
                raise BuildError(f"{target.name} has {colours} colours: the app drew nothing")
        case Platform.MACOS:
            run(["screencapture", "-x", target])
        case Platform.WINDOWS:
            script = target.with_suffix(".ps1")
            script.write_text(WINDOWS_SCREENSHOT)
            run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, target])
            script.unlink()


def test_desktop(package: Path, out: Path) -> None:
    """The desktop app installs from package, opens its window and draws its first screen without exiting."""
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        work = Path(tmp).resolve()
        app, app_env = install_desktop(package, work)
        env = stack.enter_context(linux_display(out)) if host_platform() == Platform.LINUX else {}
        with open(out / f"{package.name}.log", "w") as app_log:
            process = subprocess.Popen([app], env={**os.environ, **env, **app_env}, stdout=app_log, stderr=app_log)
            try:
                wait_until(lambda: window_open(process, env), WINDOW_TIMEOUT_SECONDS, "app window")
                time.sleep(SETTLE_SECONDS)
                screen = out / f"{package.name}.png"
                screenshot(screen, env)
                if process.poll() is not None:
                    raise BuildError(f"the app exited with {process.returncode} after opening its window")
            finally:
                process.kill()
                process.wait()
    log(f"{package.name}: the app runs, {screen.name}")
    add_summary(f"`{package.name}`: the app runs, screenshot `{screen.name}`\n\n")


def find_apksigner() -> str:
    if found := shutil.which("apksigner"):
        return found
    roots = [os.environ.get(name, "") for name in ("ANDROID_SDK_ROOT", "ANDROID_HOME")] + ["/usr/local/lib/android/sdk"]
    tools = sorted((tool for root in roots if root for tool in Path(root).glob("build-tools/*/apksigner")), key=lambda p: p.parent.name)
    if not tools:
        raise BuildError("apksigner is not found: install the Android SDK build tools")
    return str(tools[-1])


def signed_for_test(apk: Path, work: Path) -> Path:
    """A copy of apk signed with a throwaway key, as Android installs only signed APKs."""
    keystore, password = work / "test.keystore", "test-key"
    keytool: list[str | Path] = ["keytool", "-genkeypair", "-keystore", keystore, "-storepass", password, "-alias", "test"]
    run([*keytool, "-keyalg", "RSA", "-keysize", "2048", "-validity", "1", "-dname", "CN=test"])
    signed = work / apk.name
    signer: list[str | Path] = [find_apksigner(), "sign", "--ks", keystore, "--ks-pass", f"pass:{password}", "--out", signed, apk]
    run(signer, title=f"apksigner sign {apk.name}")
    return signed


def test_apk(apk: Path, out: Path) -> None:
    """The APK installs in the running emulator, starts and draws its first screen without exiting."""
    require_tools(["adb", "keytool"])
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        unsigned = run([find_apksigner(), "verify", apk], check=False, capture=True).returncode != 0
        installable = signed_for_test(apk, work) if unsigned else apk
        run(["adb", "uninstall", ANDROID_PACKAGE], check=False, capture=True)
        run(["adb", "install", installable])
    run(["adb", "shell", "am", "start", "-W", "-n", ANDROID_ACTIVITY])
    time.sleep(SETTLE_SECONDS)
    screen = out / f"{apk.name}.png"
    run(["adb", "shell", "screencap", "-p", "/sdcard/screen.png"])
    run(["adb", "pull", "/sdcard/screen.png", screen])
    if not run(["adb", "shell", "pidof", ANDROID_PACKAGE], check=False, capture=True).stdout.strip():
        logcat = run(["adb", "logcat", "-d", "-t", "200"], check=False, capture=True).stdout
        record(out, f"{apk.name}.logcat", logcat)
        raise BuildError(f"{ANDROID_PACKAGE} is not running after it started: logcat in {apk.name}.logcat.txt")
    log(f"{apk.name}: the app runs, {screen.name}")
    add_summary(f"`{apk.name}`: the app runs, screenshot `{screen.name}`\n\n")
