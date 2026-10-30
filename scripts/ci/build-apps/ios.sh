#!/bin/bash

# Builds the iOS app archive for signing at export, with the core library as ios-sim builds it.
# Usage: scripts/ci/build-apps/ios.sh <output dir>
# Needs GHC, cabal, and openssl@3, libffi and gmp from Homebrew. The output dir gets SimpleX.xcarchive.zip and SHA256SUMS.

set -euo pipefail

mkdir -p "$1"
out=$(cd "$1" && pwd)
archive=$(mktemp -d)/SimpleX.xcarchive

scripts/ci/ios-sim/build-core.sh ~/Downloads/pkg-ios-aarch64-swift-json.zip
scripts/ci/ios-sim/prepare-libs.sh
scripts/ios/update-pbxproj.sh

# Ad-hoc signing embeds the entitlements of each target, which the export keeps when it signs with the release identity.
xcodebuild archive -quiet \
  -project apps/ios/SimpleX.xcodeproj \
  -scheme "SimpleX (iOS)" \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath "$archive" \
  -onlyUsePackageVersionsFromResolvedFile \
  -skipPackagePluginValidation \
  -skipMacroValidation \
  CODE_SIGN_STYLE=Manual \
  CODE_SIGN_IDENTITY=- \
  DEVELOPMENT_TEAM= \
  PROVISIONING_PROFILE_SPECIFIER=

find "$archive/Products/Applications" -name '*.app' -o -name '*.appex' | while read -r bundle; do
  echo "== $bundle"
  codesign -d --entitlements - "$bundle"
done

ditto -c -k --keepParent "$archive" "$out/SimpleX.xcarchive.zip"
(cd "$out" && shasum -a 256 SimpleX.xcarchive.zip | tee SHA256SUMS)
