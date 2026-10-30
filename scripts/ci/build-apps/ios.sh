#!/bin/bash

# Builds the iOS app archive for signing at export, with the core library as ios-sim builds it.
# Usage: scripts/ci/build-apps/ios.sh <output dir>
# Needs GHC, cabal, and openssl@3, libffi and gmp from Homebrew. The output dir gets SimpleX.xcarchive.zip and SHA256SUMS.

set -euo pipefail

project=apps/ios/SimpleX.xcodeproj
scheme="SimpleX (iOS)"
mkdir -p "$1"
out=$(cd "$1" && pwd)
work=$(mktemp -d)
archive=$work/SimpleX.xcarchive

scripts/ci/ios-sim/build-core.sh ~/Downloads/pkg-ios-aarch64-swift-json.zip
scripts/ci/ios-sim/prepare-libs.sh
scripts/ios/update-pbxproj.sh

# Xcode refuses ad-hoc signing for iOS devices ("Ad Hoc code signing is not allowed with SDK"), so the archive is built unsigned.
xcodebuild archive -quiet \
  -project "$project" \
  -scheme "$scheme" \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath "$archive" \
  -onlyUsePackageVersionsFromResolvedFile \
  -skipPackagePluginValidation \
  -skipMacroValidation \
  CODE_SIGNING_ALLOWED=NO

# The export keeps only entitlements that a bundle's signature carries, so each bundle is signed ad hoc with its own.
# $(AppIdentifierPrefix) is the team id: simplex.chat's apple-app-site-association lists 5NN7GUYB6T.chat.simplex.app.
team=$(xcodebuild -showBuildSettings -project "$project" -scheme "$scheme" -configuration Release 2>/dev/null |
  awk '$1 == "DEVELOPMENT_TEAM" {print $3; exit}')
[ -n "$team" ] || { echo "DEVELOPMENT_TEAM not found" >&2; exit 1; }

sign_nested() {
  local frameworks=$1/Frameworks code
  [ -d "$frameworks" ] || return 0
  find "$frameworks" -maxdepth 1 \( -name '*.framework' -o -name '*.dylib' \) | while read -r code; do
    codesign --force --sign - --timestamp=none "$code"
  done
}

sign_bundle() {
  local bundle=$1 entitlements=$2 resolved signed
  [ -f "$entitlements" ] || { echo "no entitlements for $bundle: $entitlements" >&2; exit 1; }
  resolved=$work/$(basename "$bundle").entitlements
  signed=$work/$(basename "$bundle").signed.entitlements
  sed "s/\$(AppIdentifierPrefix)/$team./g" "$entitlements" > "$resolved"
  sign_nested "$bundle"
  codesign --force --sign - --timestamp=none --entitlements "$resolved" "$bundle"
  codesign -d --entitlements - --xml "$bundle" > "$signed"
  if ! python3 -c 'import plistlib, sys; sys.exit(plistlib.load(open(sys.argv[1], "rb")) != plistlib.load(open(sys.argv[2], "rb")))' "$resolved" "$signed"; then
    echo "the signature of $bundle carries other entitlements than $entitlements" >&2
    exit 1
  fi
  echo "== $bundle"
  cat "$resolved"
}

app=$archive/Products/Applications/SimpleX.app
for appex in "$app"/PlugIns/*.appex; do
  name=$(basename "$appex" .appex)
  sign_bundle "$appex" "apps/ios/$name/$name.entitlements"
done
sign_bundle "$app" "apps/ios/$scheme.entitlements"
codesign --verify --deep --strict "$app"

ditto -c -k --keepParent "$archive" "$out/SimpleX.xcarchive.zip"
(cd "$out" && shasum -a 256 SimpleX.xcarchive.zip | tee SHA256SUMS)
