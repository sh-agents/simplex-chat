#!/bin/bash

# Builds the iOS app archive for signing at export, with the core library releases ship: the flake's
# aarch64-darwin-ios:lib:simplex-chat, built with Nix.
# Usage: scripts/ci/build-apps/ios.sh <output dir> <simplex-chat-art checkout>
# The output dir gets SimpleX.xcarchive.zip and SHA256SUMS.

set -euo pipefail

# the 2.22 installer gives build users UIDs from 301, which macOS 15 and later reserve
nix_ver=nix-2.34.6
nix_hash=bf6d12da4aeaae38ab509dc736df648597bcdee051046ea76d3d53d5b18fa54a
core_job='.#hydraJobs.aarch64-darwin.aarch64-darwin-ios:lib:simplex-chat'
core_zip=pkg-ios-aarch64-swift-json.zip
project=apps/ios/SimpleX.xcodeproj
scheme="SimpleX (iOS)"
[ $# -eq 2 ] || { echo "usage: $0 <output dir> <simplex-chat-art checkout>" >&2; exit 1; }
[ -d "$2/ios/Assets.xcassets" ] || { echo "no ios/Assets.xcassets in $2" >&2; exit 1; }
mkdir -p "$1"
out=$(cd "$1" && pwd)
art=$(cd "$2" && pwd)
work=$(mktemp -d)
archive=$work/SimpleX.xcarchive

install_nix() {
  curl --proto '=https' --tlsv1.2 -sSf -o "$work/nix-install" "https://releases.nixos.org/nix/$nix_ver/install"
  echo "$nix_hash  $work/nix-install" | shasum -a 256 -c
  # zw3rk's Hydra and IOG cache the haskell.nix toolchains; without them GHC and every dependency are compiled
  cat > "$work/nix.conf" << EOF
experimental-features = nix-command flakes
max-jobs = auto
extra-substituters = https://cache.zw3rk.com https://cache.iog.io
extra-trusted-public-keys = loony-tools:pr9m4BkM/5/eSTZlkQyRt57Jz7OMBxNSUiMC4FkcNfk= hydra.iohk.io:f/Ea+s+dFdN+3Y/G+FDgSq+a5NEWhJGzdjvKNGv0/EQ=
EOF
  sh "$work/nix-install" --daemon --yes --no-channel-add --nix-extra-conf-file "$work/nix.conf"
  set +u
  # shellcheck disable=SC1091
  . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
  set -u
  nix --version
}

build_setting() {
  awk -v name="$1" '$1 == name {print $3; exit}' "$work/build-settings"
}

# mac2ios relabels macOS objects as iOS and keeps their minimum version, which the app's deployment target must cover.
check_minimum_versions() {
  local target=$1 lib newest
  for lib in apps/ios/Libraries/ios/*.a; do
    # LC_BUILD_VERSION states it as minos, the older LC_VERSION_MIN_* as version; objects without either set no minimum
    newest=$(otool -l "$lib" | awk '$1 == "minos" {print $2} $2 ~ /^LC_VERSION_MIN_/ {min = 1} min && $1 == "version" {print $2; min = 0}' | sort -V | tail -1)
    echo "$lib: newest minimum version ${newest:-none}"
    if [ -n "$newest" ] && [ "$(printf '%s\n%s\n' "$newest" "$target" | sort -V | tail -1)" != "$target" ]; then
      echo "$lib has objects for iOS $newest, newer than the deployment target $target" >&2
      exit 1
    fi
  done
}

# The export keeps only entitlements that a bundle's signature carries, so each bundle is signed ad hoc with its own.
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

install_nix
nix build -L --out-link "$work/core" "$core_job"
mkdir -p ~/Downloads
cp "$work/core/$core_zip" ~/Downloads/
scripts/ci/ios-sim/prepare-libs.sh
scripts/ios/update-pbxproj.sh

# The brand assets, as release-mobile.py adds them: Release.xcconfig includes Local.xcconfig, which turns on SIMPLEX_ASSETS.
# copy-assets.sh runs before the build, as its build phase runs after the extensions' catalogs are compiled.
# shellcheck disable=SC2016 # $(inherited) is xcconfig syntax, written as is
printf 'SIMPLEX_ASSETS_DIR = %s\nSWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) SIMPLEX_ASSETS\n' "$art" > apps/ios/Local.xcconfig
scripts/ios/copy-assets.sh "$art"
if ! ls apps/ios/Shared/SimpleXAssets.xcassets/*.imageset > /dev/null 2>&1; then
  echo "copy-assets.sh copied no imagesets from $art" >&2
  exit 1
fi

xcodebuild -showBuildSettings -project "$project" -scheme "$scheme" -configuration Release > "$work/build-settings" 2>/dev/null
# $(AppIdentifierPrefix) is the team id: simplex.chat's apple-app-site-association lists 5NN7GUYB6T.chat.simplex.app.
team=$(build_setting DEVELOPMENT_TEAM)
deployment_target=$(build_setting IPHONEOS_DEPLOYMENT_TARGET)
if [ -z "$team" ] || [ -z "$deployment_target" ]; then
  echo "DEVELOPMENT_TEAM or IPHONEOS_DEPLOYMENT_TARGET not found" >&2
  exit 1
fi
check_minimum_versions "$deployment_target"

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

app=$archive/Products/Applications/SimpleX.app
for appex in "$app"/PlugIns/*.appex; do
  name=$(basename "$appex" .appex)
  sign_bundle "$appex" "apps/ios/$name/$name.entitlements"
done
sign_bundle "$app" "apps/ios/$scheme.entitlements"
codesign --verify --deep --strict "$app"
# the team Xcode records when it signs the archive, which Organizer shows and distributes for
plutil -replace ApplicationProperties.Team -string "$team" "$archive/Info.plist"

ditto -c -k --keepParent "$archive" "$out/SimpleX.xcarchive.zip"
(cd "$out" && shasum -a 256 SimpleX.xcarchive.zip | tee SHA256SUMS)
