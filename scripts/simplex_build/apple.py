"""What the macOS and iOS builds share: the pinned Xcode."""

import re
from pathlib import Path

from .common import BuildError, add_summary, log, run

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
