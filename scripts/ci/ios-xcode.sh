#!/bin/bash

# Moves the Xcode of the release builds, XCODE_VERSION in scripts/simplex_build/apple.py, to /Applications/Xcode.app,
# where the iOS build needs it; the runner images link that path to their default Xcode.

set -euo pipefail

release=/Applications/Xcode_26.3.app
usual=/Applications/Xcode.app

if [ -L "$usual" ]; then
  sudo rm "$usual"
fi
if [ -e "$usual" ]; then
  echo "::error::$usual is not a link to the image's default Xcode"
  exit 1
fi
sudo mv "$release" "$usual"
