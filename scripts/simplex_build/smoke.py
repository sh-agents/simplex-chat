"""Smoke tests of the release files: each runs as a user installs it, and the apps leave a screenshot."""

import base64
import contextlib
import os
import platform
import re
import shutil
import signal
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
DESKTOP_KEY_SETTING = "EncryptedDBPassphrase"
DEB_PACKAGE = "simplex"
DESKTOP_APP_BUNDLE = "SimpleX.app"
# The previous release's desktop packages have the names of the new ones
OLD_RELEASE_DESKTOP_SHA256 = {
    "simplex-desktop-ubuntu-22_04-x86_64.deb": "21be1e78c1089c01567ccfec58cdf3e72a2733aa232c5288b8f155e6c7caba0b",
    "simplex-desktop-ubuntu-24_04-x86_64.deb": "67fa2919fc1bc0e034a9f014e8d4b2ba9072be2056a44436c230fc4a6fa3d839",
    "simplex-desktop-ubuntu-22_04-aarch64.deb": "0d0fd94404a67e35b9218d5afb1c5fca5f3101782b19aea415f90831d874d5a2",
    "simplex-desktop-ubuntu-24_04-aarch64.deb": "3720c2428952e53d39b8cd4c76dd8e801e182bba4e40b2010786a6e465153b4e",
    "simplex-desktop-x86_64.AppImage": "404220f16f1c03879b8d7402e31e8ce525dc3ce7a6904637bd48e19974f2536a",
    "simplex-desktop-aarch64.AppImage": "8eafda1690c81a10136b5e284d80acfc44b9e8f919fa4a522d32b8ac9ebd915d",
    "simplex-desktop-macos-aarch64.dmg": "296188f3c54f181d9e4c1e54a035d42788e92f11fed2f5399c705df69f09116e",
    "simplex-desktop-macos-x86_64.dmg": "8e7be5015dd8b3f96ef08c8752f99d7db517d8461b87db1ac56bd561414219e7",
    "simplex-desktop-windows-x86_64.msi": "0162d8eb42917b59962203b74527512c6d05eb032cf6e8189b62eb39587fbc79",
}
ANDROID_DATA_DIR = f"/data/user/0/{ANDROID_PACKAGE}"
ANDROID_DATABASE_PREFIX = "files"
ANDROID_PASSPHRASE_TEXT = "Database passphrase is required"
ANDROID_PASSPHRASE_FIELD = "Enter passphrase"
ANDROID_SAVE_BUTTON = "Save passphrase and open chat"
ANDROID_UPGRADE_BUTTON = "Upgrade and open chat"
# what the app may ask once a database with a user opens: a database upgrade, background notifications, delivery receipts
ANDROID_DISMISS_BUTTONS = (ANDROID_UPGRADE_BUTTON, "Disable notifications", "Don't enable")
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


def open_old_database(
    cli: Path, prefix: Path, out: Path, name: str, migrate: bool, key: str = OLD_DATABASE_KEY, expected: str = f"#{OLD_DATABASE_GROUP}"
) -> None:
    """The CLI opens the database at prefix with key as the old release's user, which it makes in a database without
    users, and lists the groups, which must print expected; without migrate it accepts no migration, so the database
    must have been migrated already."""
    user = ["--user-display-name", OLD_DATABASE_USER]
    args: list[str | Path] = [cli, "-d", prefix, "-k", key, *user, "-e", "/groups", *(["-y"] if migrate else [])]
    result = run(args, input="", check=False, capture=True)
    record(out, name, result.stdout)
    if result.returncode != 0 or expected not in result.stdout:
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
            app = work / package.name
            shutil.copy(package, app)
            app.chmod(0o755)
            # runs the AppImage without FUSE, which CI runners lack, extracted in work
            return app, {"APPIMAGE_EXTRACT_AND_RUN": "1", "TMPDIR": str(work), "JAVA_TOOL_OPTIONS": LINUX_JAVA_OPTIONS}
        case ".dmg":
            mount, bundle = work / "dmg", work / DESKTOP_APP_BUNDLE
            run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, package])
            try:
                remove_tree(bundle)
                shutil.copytree(mount / DESKTOP_APP_BUNDLE, bundle, symlinks=True)
            finally:
                run(["hdiutil", "detach", mount])
            return bundle / "Contents/MacOS/SimpleX", {}
        case ".msi":
            msi, log_file = windows_paths(package.resolve(), work / "msiexec.log")
            run(["msiexec", "/i", msi, "/qn", "/l*v", log_file])
            return Path(os.environ["PROGRAMFILES"]) / "SimpleX/SimpleX.exe", {}
    raise BuildError(f"{package.name}: expected a .deb, .AppImage, .dmg or .msi")


def windows_paths(*paths: Path) -> list[str]:
    # msiexec opens only paths with backslashes, and MSYS2's Python joins paths with slashes
    return [str(path).replace("/", "\\") for path in paths]


def uninstall_desktop(package: Path, work: Path) -> None:
    """Removes the app that install_desktop installed system-wide; the others are in work."""
    match package.suffix:
        case ".deb":
            run(["sudo", "apt-get", "remove", "-y", DEB_PACKAGE])
        case ".msi":
            msi, log_file = windows_paths(package.resolve(), work / "msiexec-uninstall.log")
            run(["msiexec", "/x", msi, "/qn", "/l*v", log_file])


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


def java_properties(text: str) -> dict[str, str]:
    """The entries that java.util.Properties.store wrote, which escapes = and : in keys and values with a backslash."""
    entries = {}
    for line in text.splitlines():
        if entry := re.fullmatch(r"((?:[^\\=:#!]|\\.)+)[=:](.*)", line):
            key, value = (re.sub(r"\\(.)", r"\1", part) for part in entry.groups())
            entries[key] = value
    return entries


def desktop_stored_key(config: Path) -> str | None:
    """The database key that the app made and stored in its settings, where its desktop Cryptor keeps it in base64."""
    settings = config / DESKTOP_SETTINGS
    encoded = java_properties(settings.read_text()).get(DESKTOP_KEY_SETTING) if settings.exists() else None
    return base64.b64decode(encoded).decode() if encoded else None


def desktop_database_stored(config: Path, data: Path) -> bool:
    files = all((data / f"{DESKTOP_DATABASE_PREFIX}{suffix}").exists() for suffix in DATABASE_SUFFIXES)
    return files and desktop_stored_key(config) is not None


@contextlib.contextmanager
def desktop_data() -> Iterator[tuple[Path, Path]]:
    """The app's settings and data directories, which must not exist yet, so that a test never touches the data of an
    installed app; removed after."""
    dirs = set(desktop_dirs())
    if existing := [d for d in dirs if d.exists()]:
        raise BuildError(f"{', '.join(map(str, existing))} exists: the test would replace this SimpleX app's data")
    try:
        yield desktop_dirs()
    finally:
        for directory in dirs:
            remove_tree(directory)


@contextlib.contextmanager
def desktop_old_database(database: Path) -> Iterator[Path]:
    """The previous release's database where the app opens it, with its key in the app's settings; yields the
    database prefix."""
    with desktop_data() as (config, data):
        copy_old_database(database, data / DESKTOP_DATABASE_PREFIX)
        config.mkdir(parents=True, exist_ok=True)
        (config / DESKTOP_SETTINGS).write_text(desktop_settings_with_key(OLD_DATABASE_KEY))
        yield data / DESKTOP_DATABASE_PREFIX


def stop_app(process: subprocess.Popen[bytes]) -> None:
    """Kills the app with the processes it started: the AppImage's runtime runs the app in a child process."""
    if host_platform() == Platform.WINDOWS:
        process.kill()
    else:
        # the group is gone when the app and its children exited before
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def run_desktop(
    app: Path,
    app_env: Mapping[str, str],
    env: Mapping[str, str],
    out: Path,
    name: str,
    ready: tuple[Callable[[], bool], str] | None = None,
) -> Path:
    """Runs the installed app until its window is open, ready's condition holds and it has drawn its screen, then stops
    it; the app must not exit by itself. Returns the screenshot, out/name.png."""
    with open(out / f"{name}.log", "w") as app_log:
        process_env = {**os.environ, **env, **app_env}
        process = subprocess.Popen([app], env=process_env, stdout=app_log, stderr=app_log, start_new_session=True)
        try:
            wait_until(lambda: window_open(process, env), WINDOW_TIMEOUT_SECONDS, "app window")
            if ready:
                condition, what = ready
                wait_until(lambda: window_open(process, env) and condition(), WINDOW_TIMEOUT_SECONDS, what)
            time.sleep(SETTLE_SECONDS)
            screen = out / f"{name}.png"
            screenshot(screen, env)
            if process.poll() is not None:
                raise BuildError(f"the app exited with {process.returncode} after opening its window")
        finally:
            stop_app(process)
    return screen


def test_desktop(package: Path, out: Path, database: Path | None = None, cli: Path | None = None) -> None:
    """The desktop app installs from package, opens its window and draws its first screen without exiting; given the
    previous release's database and the new CLI, the app must open and migrate that database."""
    if bool(database) != bool(cli):
        raise BuildError("the old database test needs both the database and the CLI that checks it")
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        work = Path(tmp).resolve()
        prefix = None
        if database:
            prefix = stack.enter_context(desktop_old_database(database))
        else:
            stack.enter_context(desktop_data())
        app, app_env = install_desktop(package, work)
        env = stack.enter_context(linux_display(out)) if host_platform() == Platform.LINUX else {}
        screen = run_desktop(app, app_env, env, out, package.name)
        if prefix and cli:
            open_old_database(runnable_cli(cli, work), prefix, out, f"{package.name}.database", migrate=False)
    opened = f", opens and migrates the {OLD_RELEASE} database" if database else ""
    log(f"{package.name}: the app runs{opened}, {screen.name}")
    add_summary(f"`{package.name}`: the app runs{opened}, screenshot `{screen.name}`\n\n")


def test_desktop_upgrade(package: Path, out: Path, cli: Path | None) -> None:
    """The previous release's app installs from its package of the same name, makes its encrypted database and stores
    its key in its settings; package then installs over it as users upgrade, and its app must open and migrate that
    database, which the new CLI then opens without migrations."""
    if not cli:
        raise BuildError("the upgrade test needs the CLI that checks the database")
    if package.name not in OLD_RELEASE_DESKTOP_SHA256:
        raise BuildError(f"no {OLD_RELEASE} package is pinned for {package.name}")
    with tempfile.TemporaryDirectory() as tmp, desktop_data() as (config, data), contextlib.ExitStack() as stack:
        work = Path(tmp).resolve()
        (work / OLD_RELEASE).mkdir()
        old_url = release_url(OLD_RELEASE, package.name)
        old_package = fetch_pinned(old_url, OLD_RELEASE_DESKTOP_SHA256[package.name], work / OLD_RELEASE / package.name)
        env = stack.enter_context(linux_display(out)) if host_platform() == Platform.LINUX else {}
        old_app, old_env = install_desktop(old_package, work)
        stack.callback(uninstall_desktop, package, work)
        stored = (lambda: desktop_database_stored(config, data), "database with its key in the app's settings")
        old_screen = run_desktop(old_app, old_env, env, out, f"{package.name}.{OLD_RELEASE}", stored)
        key = desktop_stored_key(config)
        if key is None:
            raise BuildError(f"the {OLD_RELEASE} app removed its database key from {DESKTOP_SETTINGS}")
        app, app_env = install_desktop(package, work)
        screen = run_desktop(app, app_env, env, out, f"{package.name}.upgrade")
        prefix, checked, user = data / DESKTOP_DATABASE_PREFIX, f"{package.name}.upgrade.database", f"Current user: {OLD_DATABASE_USER}"
        open_old_database(runnable_cli(cli, work), prefix, out, checked, migrate=False, key=key, expected=user)
    upgraded = f"upgrades from {OLD_RELEASE} and opens and migrates the database that {OLD_RELEASE} made"
    log(f"{package.name}: the app {upgraded}, {old_screen.name}, {screen.name}")
    add_summary(f"`{package.name}`: the app {upgraded}, screenshots `{old_screen.name}`, `{screen.name}`\n\n")


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


def open_android_old_database(out: Path, work: Path) -> None:
    """Enters the key on the app's passphrase screen and waits for the chat list with the previous release's group;
    on a timeout it records the screen's tree and a screenshot."""

    def chat_list_shown() -> bool:
        ui = android_screen()
        for button in ANDROID_DISMISS_BUTTONS:
            if f'text="{button}' in ui:
                android_tap(button)
                return False
        return f'text="{OLD_DATABASE_GROUP}"' in ui

    try:
        wait_until(lambda: ANDROID_PASSPHRASE_TEXT in android_screen(), ANDROID_FIRST_SCREEN_SECONDS, "passphrase screen")
        # the emulator has a hardware keyboard, so no on-screen keyboard covers the button
        wait_until(lambda: android_tap(ANDROID_PASSPHRASE_FIELD), SETTLE_SECONDS, "passphrase field")
        run(["adb", "shell", "input", "text", OLD_DATABASE_KEY])
        wait_until(lambda: android_tap(ANDROID_SAVE_BUTTON), SETTLE_SECONDS, "button to open the chat")
        wait_until(chat_list_shown, ANDROID_FIRST_SCREEN_SECONDS, f"chat list with the group {OLD_DATABASE_GROUP}")
    except BuildError:
        record(out, "android-old-database-screen", android_screen())
        emulator_screenshot(out / "android-old-database.png", work)
        raise


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
            open_android_old_database(out, work)
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
