# App builds for signing

`.github/workflows/build-apps.yml` builds the Android and iOS apps of a commit without signing them and uploads them as artifacts, to be signed outside CI. No signing key or certificate reaches GitHub.

## Running it

The workflow runs only when started by hand, on any branch: in the Actions tab, `build-apps` → Run workflow, or `gh workflow run build-apps.yml --ref <branch>`. A newer run on the same branch cancels the older one.

## Jobs and artifacts

| Job | Runner | Builds | Artifact, kept 30 days |
|---|---|---|---|
| Android aarch64 APK, Android armv7a APK | ubuntu-24.04 | `android.sh apk <arch>`: `scripts/android/build-android.sh` in the `Dockerfile.build` container | `android-<arch>-unsigned`: `simplex-<arch>.apk`, `pkg-<arch>-android-libsimplex.zip`, `pkg-<arch>-android-libsupport.zip`, `SHA256SUMS` |
| Android Play bundle | ubuntu-24.04 | `android.sh bundle`: `:android:bundleGoogleRelease` in the same container, with both arches' core libraries from the APK jobs | `android-bundle-unsigned`: `simplex.aab`, `SHA256SUMS` |
| iOS archive | macos-26 | `ios.sh`: the core library with ios-sim's `build-core.sh` and `prepare-libs.sh`, then `xcodebuild archive` of the Release configuration for devices, signed ad hoc | `ios-archive-unsigned`: `SimpleX.xcarchive.zip`, `SHA256SUMS` |

`gh run download <run id> -n <artifact>` downloads one.

The APKs are the foss flavor, as `scripts/android/build-android.sh` builds them: deterministic, aligned and unsigned, with the version code of `apps/multiplatform/gradle.properties`. `scripts/simplex-chat-reproduce-builds-android.sh` builds the same APK in the same container and compares it with the released, signed one by copying the release's signature onto it (`apksigcopier`), so an APK released from this artifact, signed and otherwise unchanged, is the one that check expects. The bundle is the google flavor for Google Play; nothing checks its reproducibility.

The iOS archive keeps the entitlements of the app and its extensions in their ad-hoc signatures, which the job prints; signing it with the release identity is `xcodebuild -exportArchive` on a Mac with the distribution certificate and profiles.

## Known limits

- **The Android core relies on cache.zw3rk.com** for the cross-compiling toolchains and dependencies, as android-emu does: without it the build takes 12 to 24 hours, longer than a job may run. armv7a uses the GHC 8.10.7 toolchain, so its first build after an input change may be slower than aarch64's.
- **The iOS core library is the cabal build of ios-sim**, with the same files as the Nix package `aarch64-darwin-ios:lib:simplex-chat` that releases have shipped, but not built by Nix.
- Not run yet: whether `xcodebuild archive` accepts ad-hoc signing for a device build with these entitlements, and whether `-exportArchive` re-signs the archive with them.
