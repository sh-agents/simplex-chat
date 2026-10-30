#!/bin/bash

# Builds unsigned Android apps in the Dockerfile.build container, the environment scripts/simplex-chat-reproduce-builds-android.sh checks.
# Usage:
#   scripts/ci/build-apps/android.sh apk <aarch64|armv7a> <output dir>
#     the deterministic APK of scripts/android/build-android.sh, as simplex-<arch>.apk, and the two core library zips
#   scripts/ci/build-apps/android.sh bundle <dir with both arches' core library zips> <output dir>
#     the Play bundle (google flavor) as simplex.aab
# Each output dir also gets SHA256SUMS.

set -euo pipefail

image=simplex-build-apps
container=simplex-build-apps
libs_dir=apps/multiplatform/common/src/commonMain/cpp/android/libs
# zw3rk's Hydra builds the Android jobs of master and IOG caches the haskell.nix toolchains;
# without them the core library takes 12-24 hours to build.
nix_config="extra-substituters = https://cache.zw3rk.com https://cache.iog.io
extra-trusted-public-keys = loony-tools:pr9m4BkM/5/eSTZlkQyRt57Jz7OMBxNSUiMC4FkcNfk= hydra.iohk.io:f/Ea+s+dFdN+3Y/G+FDgSq+a5NEWhJGzdjvKNGv0/EQ=
cores = 0"

abi() {
  case $1 in
    aarch64) echo arm64-v8a ;;
    armv7a) echo armeabi-v7a ;;
    *) echo "unknown arch: $1" >&2; exit 1 ;;
  esac
}

start_container() {
  docker build -f Dockerfile.build -t "$image" --build-arg USER_UID="$(id -u)" --build-arg USER_GID="$(id -g)" .
  docker run -t -d --name "$container" \
    --device /dev/fuse \
    --cap-add SYS_ADMIN \
    --security-opt apparmor:unconfined \
    --security-opt seccomp:unconfined \
    -v "$PWD:/project" \
    -v "$out:/out" \
    "$image"
  trap 'docker rm --force "$container" > /dev/null' EXIT
  # build-android.sh needs the gradle of the wrapper's version on PATH
  docker exec "$container" sh -c 'cd apps/multiplatform && ./gradlew --version'
}

checksums() {
  local sums
  sums=$(cd "$out" && find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%P\n' | sort | xargs sha256sum)
  printf '%s\n' "$sums" | tee "$out/SHA256SUMS"
}

build_apk() {
  local arch=$1 android_abi vercode
  android_abi=$(abi "$arch")
  vercode=$(sed -n 's/^android.version_code=//p' apps/multiplatform/gradle.properties)
  start_container
  docker exec -e NIX_CONFIG="$nix_config" -e ARCH="$arch" -e ABI="$android_abi" -e VERCODE="$vercode" "$container" sh -eu -c '
    gradle_bin=$(find "$HOME/.gradle/wrapper/dists" -name gradle -type f -executable | head -1)
    export PATH="$(dirname "$gradle_bin"):$PATH"
    ARCHES="$ARCH" ./scripts/android/build-android.sh -gs "$VERCODE"
    cp "simplex-chat-$ABI.apk" "/out/simplex-$ARCH.apk"
    cp "result-$ARCH-libsimplex/pkg-$ARCH-android-libsimplex.zip" "result-$ARCH-libsupport/pkg-$ARCH-android-libsupport.zip" /out/'
  checksums
}

build_bundle() {
  local libs=$1 arch android_abi
  for arch in aarch64 armv7a; do
    android_abi=$(abi "$arch")
    mkdir -p "$libs_dir/$android_abi"
    unzip -o "$libs/pkg-$arch-android-libsimplex.zip" -d "$libs_dir/$android_abi"
    unzip -o "$libs/pkg-$arch-android-libsupport.zip" -d "$libs_dir/$android_abi"
  done
  start_container
  docker exec "$container" sh -eu -c '
    cd apps/multiplatform
    ./gradlew --no-daemon -Psimplex.assets.dir=../../assets :android:bundleGoogleRelease
    cp android/build/outputs/bundle/googleRelease/*.aab /out/simplex.aab'
  checksums
}

case "${1:-}" in
  apk) [ $# -eq 3 ] || { echo "usage: $0 apk <aarch64|armv7a> <output dir>" >&2; exit 1; } ;;
  bundle) [ $# -eq 3 ] || { echo "usage: $0 bundle <core library zips dir> <output dir>" >&2; exit 1; } ;;
  *) echo "usage: $0 apk|bundle ..." >&2; exit 1 ;;
esac
mkdir -p "$3"
out=$(cd "$3" && pwd)
case "$1" in
  apk) build_apk "$2" ;;
  bundle) build_bundle "$(cd "$2" && pwd)" ;;
esac
