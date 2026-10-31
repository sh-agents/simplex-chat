"""What the macOS and iOS builds share: the pinned Xcode, and the directory and settings that they build with, so that
the release files don't depend on the machine's paths and settings."""

import contextlib
import os
import re
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .common import BuildError, add_summary, log, normalize_permissions, remove_tree, run

# The Xcode of the release builds, which the macos-15, macos-15-intel and macos-26 runner images all have; its compilers,
# linker and SDKs write the binaries
XCODE_VERSION = "26.3"


XCODE_BUILD = "17C529"


XCODE_VERSION_OUTPUT = re.compile(r"^Xcode (?P<version>\S+)\nBuild version (?P<build>\S+)$", re.MULTILINE)


def xcode_version(output: str) -> tuple[str, str]:
    """The version and build of the Xcode that xcodebuild -version describes in output."""
    match = XCODE_VERSION_OUTPUT.search(output)
    if match is None:
        raise BuildError(f"no Xcode version in what xcodebuild -version prints: {output!r}")
    return match["version"], match["build"]


def require_xcode() -> Path:
    """The developer directory of the selected Xcode, which must be the pinned one."""
    version = xcode_version(run(["xcodebuild", "-version"], capture=True).stdout)
    if version != (XCODE_VERSION, XCODE_BUILD):
        raise BuildError(
            f"Xcode {version[0]} ({version[1]}) is selected, not Xcode {XCODE_VERSION} ({XCODE_BUILD}) of the release builds: "
            "set DEVELOPER_DIR to its Contents/Developer directory"
        )
    developer = Path(run(["xcrun", "--find", "xcodebuild"], capture=True).stdout.strip()).resolve().parents[2]
    log(f"Xcode {XCODE_VERSION} ({XCODE_BUILD}) in {developer}")
    add_summary(f"Built with Xcode {XCODE_VERSION} ({XCODE_BUILD}).\n\n")
    return developer


# The release builds run in a copy of the checkout here, with cabal's directory beside it: the binaries contain the paths
# of the checkout, of OpenSSL's build in it and of cabal's package store
BUILD_ROOT = Path("/private/tmp/simplex-chat-build")


# the settings of GitHub's macOS runners
BUILD_UMASK = 0o022


BUILD_LOCALE = "en_US.UTF-8"


BUILD_TIMEZONE = "UTC"


@dataclass(frozen=True, slots=True)
class BuildRoot:
    """The copy of the checkout that a release builds in, and an empty directory for the rest of its build."""

    source: Path
    work: Path


def build_env(root: Path) -> dict[str, str]:
    return {"CABAL_DIR": str(root / "cabal"), "LC_ALL": BUILD_LOCALE, "TZ": BUILD_TIMEZONE}


def clear_build_root() -> None:
    try:
        remove_tree(BUILD_ROOT)
    except OSError as e:
        raise BuildError(f"cannot remove {BUILD_ROOT}, where the release builds run: {e}") from e


@contextlib.contextmanager
def build_root(source: Path, env: Mapping[str, str]) -> Iterator[BuildRoot]:
    """source copied into BUILD_ROOT, with the modes of normalize_permissions, and BUILD_UMASK and the variables of
    build_env and env set while it is used; BUILD_ROOT is removed before and after."""
    if source.is_relative_to(BUILD_ROOT):
        raise BuildError(f"{source} is in {BUILD_ROOT}, which the build replaces: build from a checkout elsewhere")
    root = BuildRoot(BUILD_ROOT / "simplex-chat", BUILD_ROOT / "work")
    clear_build_root()
    root.work.mkdir(parents=True)
    shutil.copytree(source, root.source, symlinks=True)
    normalize_permissions(root.source)
    settings = {**build_env(BUILD_ROOT), **env}
    saved = {name: os.environ.get(name) for name in settings}
    umask = os.umask(BUILD_UMASK)
    os.environ.update(settings)
    try:
        yield root
    finally:
        os.umask(umask)
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        clear_build_root()
