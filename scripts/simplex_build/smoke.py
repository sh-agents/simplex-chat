"""Smoke tests of the release files: each runs as a user installs it, and the apps leave a screenshot."""

import base64
import contextlib
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from xml.etree import ElementTree

from .common import (
    BuildError,
    Platform,
    add_summary,
    fetch_pinned,
    host_platform,
    log,
    release_url,
    remove_tree,
    require_tools,
    run,
    warn,
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
EMULATOR_SCREENSHOT_SECONDS = 10
# the x86_64 image translates the armv7a APK's ARM code, which opens the database several times slower
ANDROID_FIRST_SCREEN_SECONDS = 300
ANDROID_LOADING_TEXT = "Opening database"
# Screenshots with fewer colours are blank, or show only a background
MIN_SCREENSHOT_COLOURS = 16
# The release before the pinned OpenSSL: its CLI makes the encrypted database that every new build must open
OLD_RELEASE = "v7.0.3"
OLD_RELEASE_CLI_SHA256 = {
    "x86_64": "5afb1d25efe5ccf564a1ab124bc7f410a7a73171c974a4d8b7f4d8f2e3d62e77",
    "aarch64": "91f332edc813cab78a848da7ba75ed91bcb8d09abf1fc92eca74330dc29ec970",
}
# adb types text without quoting, so the key has no spaces
OLD_DATABASE_KEY = "old-release-key"
OLD_DATABASE_PREFIX = "simplex"
DATABASE_SUFFIXES = ("_chat.db", "_agent.db")
OLD_DATABASE_USER = "alice"
OLD_DATABASE_GROUP = "team"
DESKTOP_DATABASE_PREFIX = "simplex_v1"
DESKTOP_SETTINGS = "settings.properties"
ANDROID_DATA_DIR = f"/data/user/0/{ANDROID_PACKAGE}"
ANDROID_DATABASE_PREFIX = "files"
ANDROID_PASSPHRASE_TEXT = "Database passphrase is required"
ANDROID_UPGRADE_BUTTON = "Upgrade and open chat"
WINDOWS_SCREENSHOT = """Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
[System.Drawing.Graphics]::FromImage($bitmap).CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bitmap.Save($args[0], [System.Drawing.Imaging.ImageFormat]::Png)
"""


def record(out: Path, name: str, text: str) -> None:
    (out / f"{name}.txt").write_text(text)
    log(f"{name}: {text.strip().splitlines()[0] if text.strip() else '(no output)'}")


def make_old_database(out: Path) -> None:
    """An encrypted database with a user and a group, made by the previous release's CLI with its own OpenSSL."""
    arch = platform.machine()
    if arch not in OLD_RELEASE_CLI_SHA256:
        raise BuildError(f"no {OLD_RELEASE} CLI is pinned for {arch}")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        name = f"simplex-chat-ubuntu-22_04-{arch}"
        cli = fetch_pinned(release_url(OLD_RELEASE, name), OLD_RELEASE_CLI_SHA256[arch], Path(tmp) / name)
        cli.chmod(0o755)
        old: list[str | Path] = [cli, "-d", out / OLD_DATABASE_PREFIX, "-k", OLD_DATABASE_KEY, "--user-display-name", OLD_DATABASE_USER]
        run([*old, "-e", f"/g {OLD_DATABASE_GROUP}"], input="")
    log(f"{OLD_RELEASE} made an encrypted database in {out}")


def copy_old_database(database: Path, prefix: Path) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    for suffix in DATABASE_SUFFIXES:
        shutil.copy(database / f"{OLD_DATABASE_PREFIX}{suffix}", f"{prefix}{suffix}")


def open_old_database(cli: Path, prefix: Path, out: Path, name: str, migrate: bool) -> None:
    """The CLI opens the database at prefix with its key and lists the old release's group; without migrate it accepts
    no migration, so the database must have been migrated already."""
    args: list[str | Path] = [cli, "-d", prefix, "-k", OLD_DATABASE_KEY, "-e", "/groups", *(["-y"] if migrate else [])]
    result = run(args, input="", check=False, capture=True)
    record(out, name, result.stdout)
    if result.returncode != 0 or f"#{OLD_DATABASE_GROUP}" not in result.stdout:
        raise BuildError(f"{cli.name} did not open the {OLD_RELEASE} database: {name}.txt")


def runnable_cli(binary: Path, work: Path) -> Path:
    runnable = binary
    if host_platform() == Platform.WINDOWS and binary.suffix != ".exe":
        # Windows runs only files named .exe
        runnable = work / f"{binary.name}.exe"
        shutil.copy(binary, runnable)
    runnable.chmod(0o755)
    return runnable


def test_cli(binary: Path, out: Path, database: Path | None = None) -> None:
    """The CLI starts and prints its version, and opens the previous release's database when it is given."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        runnable = runnable_cli(binary, work)
        version = run([runnable, "--version"], capture=True).stdout
        record(out, binary.name, version)
        if "SimpleX Chat" not in version:
            raise BuildError(f"{binary.name} --version printed no SimpleX Chat version: {version!r}")
        if database:
            prefix = work / "db" / OLD_DATABASE_PREFIX
            copy_old_database(database, prefix)
            open_old_database(runnable, prefix, out, f"{binary.name}.database", migrate=True)
            wrong_key: list[str | Path] = [runnable, "-d", prefix, "-k", f"not-{OLD_DATABASE_KEY}", "-e", "/groups"]
            if run(wrong_key, input="", check=False, capture=True).returncode == 0:
                raise BuildError(f"{binary.name} opened the encrypted database with a wrong key")
    opened = f", opens the {OLD_RELEASE} database" if database else ""
    add_summary(f"`{binary.name}`: {version.strip()}{opened}\n\n")


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
    if host_platform() == Platform.LINUX:
        search = ["xdotool", "search", "--onlyvisible", "--name", f"^{DESKTOP_WINDOW_TITLE}$"]
        return run(search, env=env, check=False, capture=True).returncode == 0
    # macOS lists other apps' windows only to apps with the accessibility permission, and on GitHub's Windows runners
    # the app's process has no main window handle: the screenshot shows the window
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


def desktop_dirs() -> tuple[Path, Path]:
    """The app's settings and data directories, as Platform.desktop.kt finds them."""
    if host_platform() == Platform.WINDOWS:
        appdata = Path(os.environ["APPDATA"]) / "SimpleX"
        return appdata, appdata
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "simplex"
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "simplex"
    return config, data


def desktop_settings_with_key(key: str) -> str:
    """The app's settings with the stored database key: on desktop its Cryptor keeps the key itself, in base64."""
    encoded = base64.b64encode(key.encode()).decode()
    return f"StoreDBPassphrase=true\nEncryptedDBPassphrase={encoded}\nInitializationVectorDBPassphrase={encoded}\n"


@contextlib.contextmanager
def desktop_old_database(database: Path) -> Iterator[Path]:
    """The previous release's database where the app opens it, with its key in the app's settings; yields the
    database prefix and removes both directories after, so it refuses to run where the app has its own data."""
    dirs = set(desktop_dirs())
    if existing := [d for d in dirs if d.exists()]:
        raise BuildError(f"{', '.join(map(str, existing))} exists: the test would replace this SimpleX app's data")
    config, data = desktop_dirs()
    try:
        copy_old_database(database, data / DESKTOP_DATABASE_PREFIX)
        config.mkdir(parents=True, exist_ok=True)
        (config / DESKTOP_SETTINGS).write_text(desktop_settings_with_key(OLD_DATABASE_KEY))
        yield data / DESKTOP_DATABASE_PREFIX
    finally:
        for directory in dirs:
            remove_tree(directory)


def test_desktop(package: Path, out: Path, database: Path | None = None, cli: Path | None = None) -> None:
    """The desktop app installs from package, opens its window and draws its first screen without exiting; given the
    previous release's database and the new CLI, the app must open and migrate that database."""
    if bool(database) != bool(cli):
        raise BuildError("the old database test needs both the database and the CLI that checks it")
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        work = Path(tmp).resolve()
        prefix = stack.enter_context(desktop_old_database(database)) if database else None
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
        if prefix and cli:
            open_old_database(runnable_cli(cli, work), prefix, out, f"{package.name}.database", migrate=False)
    opened = f", opens and migrates the {OLD_RELEASE} database" if database else ""
    log(f"{package.name}: the app runs{opened}, {screen.name}")
    add_summary(f"`{package.name}`: the app runs{opened}, screenshot `{screen.name}`\n\n")


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


def emulator_screenshot(target: Path, work: Path) -> None:
    """The emulator's screen: without a window it composes the screen on the host, where the guest's screencap can't
    read it."""
    shots = work / "screenshot"
    shots.mkdir()
    run(["adb", "emu", "screenrecord", "screenshot", shots], capture=True)
    wait_until(lambda: any(png.stat().st_size for png in shots.glob("*.png")), EMULATOR_SCREENSHOT_SECONDS, "emulator screenshot")
    shutil.move(next(png for png in shots.glob("*.png") if png.stat().st_size), target)


def apk_abis(apk: Path) -> set[str]:
    with zipfile.ZipFile(apk) as z:
        return {Path(name).parent.name for name in z.namelist() if name.startswith("lib/") and name.endswith(".so")}


def android_screen() -> str:
    return run(["adb", "exec-out", "uiautomator", "dump", "/dev/tty"], check=False, capture=True).stdout


def android_screen_loaded() -> bool:
    ui = android_screen()
    return "<node" in ui and ANDROID_LOADING_TEXT not in ui


def android_node_centre(ui: str, text: str) -> tuple[int, int] | None:
    """The centre of the first node whose text starts with text, from uiautomator's dump."""
    hierarchy = ElementTree.fromstring(ui[ui.index("<?xml") : ui.rindex(">") + 1])
    for node in hierarchy.iter("node"):
        bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if node.get("text", "").startswith(text) and bounds:
            left, top, right, bottom = map(int, bounds.groups())
            return (left + right) // 2, (top + bottom) // 2
    return None


def android_tap(text: str) -> bool:
    if centre := android_node_centre(android_screen(), text):
        run(["adb", "shell", "input", "tap", str(centre[0]), str(centre[1])])
        return True
    return False


def push_android_old_database(database: Path) -> None:
    """The previous release's database in the installed app's data directory, owned by the app as Android requires;
    adb runs as root on the emulator's Google APIs image."""
    run(["adb", "root"])
    run(["adb", "wait-for-device"])
    owner = run(["adb", "shell", "stat", "-c", "%u:%g", ANDROID_DATA_DIR], capture=True).stdout.strip()
    for suffix in DATABASE_SUFFIXES:
        target = f"{ANDROID_DATA_DIR}/{ANDROID_DATABASE_PREFIX}{suffix}"
        run(["adb", "push", database / f"{OLD_DATABASE_PREFIX}{suffix}", target])
        run(["adb", "shell", "chown", owner, target])
        run(["adb", "shell", "restorecon", target])


def open_android_old_database() -> None:
    """Enters the key on the app's passphrase screen and waits for the chat list with the previous release's group."""
    wait_until(lambda: ANDROID_PASSPHRASE_TEXT in android_screen(), ANDROID_FIRST_SCREEN_SECONDS, "passphrase screen")
    run(["adb", "shell", "input", "text", OLD_DATABASE_KEY])
    # the passphrase field opens the chat on Enter, while the keyboard may cover the button
    run(["adb", "shell", "input", "keyevent", "KEYCODE_ENTER"])

    def chat_list_shown() -> bool:
        ui = android_screen()
        if ANDROID_UPGRADE_BUTTON in ui:
            android_tap(ANDROID_UPGRADE_BUTTON)
        return f'text="{OLD_DATABASE_GROUP}"' in ui

    wait_until(chat_list_shown, ANDROID_FIRST_SCREEN_SECONDS, f"chat list with the group {OLD_DATABASE_GROUP}")


def test_apk(apk: Path, out: Path, database: Path | None = None) -> None:
    """The APK installs in the running emulator, starts and draws its first screen without exiting; given the previous
    release's database, the app opens it with its key and shows its chat list."""
    require_tools(["adb", "keytool"])
    device_abis = set(run(["adb", "shell", "getprop", "ro.product.cpu.abilist"], capture=True).stdout.strip().split(","))
    if not apk_abis(apk) & device_abis:
        warn(f"{apk.name} is for {', '.join(sorted(apk_abis(apk)))}, which the emulator can't run ({', '.join(sorted(device_abis))})")
        add_summary(f"`{apk.name}`: not tested, the emulator runs {', '.join(sorted(device_abis))}\n\n")
        return
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        unsigned = run([find_apksigner(), "verify", apk], check=False, capture=True).returncode != 0
        installable = signed_for_test(apk, work) if unsigned else apk
        run(["adb", "uninstall", ANDROID_PACKAGE], check=False, capture=True)
        run(["adb", "install", "-g", installable])
        if database:
            push_android_old_database(database)
        run(["adb", "shell", "am", "start", "-W", "-n", ANDROID_ACTIVITY])
        if database:
            open_android_old_database()
        else:
            wait_until(android_screen_loaded, ANDROID_FIRST_SCREEN_SECONDS, "screen past the database loading")
        time.sleep(SETTLE_SECONDS)
        screen = out / f"{apk.name}.png"
        emulator_screenshot(screen, work)
    if not run(["adb", "shell", "pidof", ANDROID_PACKAGE], check=False, capture=True).stdout.strip():
        logcat = run(["adb", "logcat", "-d", "-t", "200"], check=False, capture=True).stdout
        record(out, f"{apk.name}.logcat", logcat)
        raise BuildError(f"{ANDROID_PACKAGE} is not running after it started: logcat in {apk.name}.logcat.txt")
    opened = f", opens the {OLD_RELEASE} database" if database else ""
    log(f"{apk.name}: the app runs{opened}, {screen.name}")
    add_summary(f"`{apk.name}`: the app runs{opened}, screenshot `{screen.name}`\n\n")
