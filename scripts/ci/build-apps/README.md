# App builds for signing

`.github/workflows/build-apps.yml` builds the Android and iOS apps of a commit without signing them and uploads them as artifacts, to be signed outside CI. No signing key or certificate reaches GitHub.

## Running it

The workflow runs only when started by hand, on any branch: in the Actions tab, `build-apps` → Run workflow, or `gh workflow run build-apps.yml --ref <branch>`. A newer run on the same branch cancels the older one.

## Jobs and artifacts

| Job | Runner | Builds | Artifact, kept 30 days |
|---|---|---|---|
| Android aarch64 APK, Android armv7a APK | ubuntu-24.04 | `android.sh apk <arch>`: `scripts/android/build-android.sh` in the `Dockerfile.build` container | `android-<arch>-unsigned`: `simplex-<arch>.apk`, `pkg-<arch>-android-libsimplex.zip`, `pkg-<arch>-android-libsupport.zip`, `SHA256SUMS` |
| Android Play bundle | ubuntu-24.04 | `android.sh bundle`: `:android:bundleGoogleRelease` in the same container, with both arches' core libraries from the APK jobs | `android-bundle-unsigned`: `simplex.aab`, `SHA256SUMS` |
| iOS archive | macos-26 | `ios.sh`: the core library with ios-sim's `build-core.sh` and `prepare-libs.sh`, then `xcodebuild archive` of the Release configuration for devices, unsigned, and each bundle signed ad hoc with its entitlements | `ios-archive-unsigned`: `SimpleX.xcarchive.zip`, `SHA256SUMS` |

`gh run download <run id> -n <artifact>` downloads one.

The APKs are the foss flavor, as `scripts/android/build-android.sh` builds them: deterministic, aligned and unsigned, with the version code of `apps/multiplatform/gradle.properties`. `scripts/simplex-chat-reproduce-builds-android.sh` builds the same APK in the same container and compares it with the released, signed one by copying the release's signature onto it (`apksigcopier`), so an APK released from this artifact, signed and otherwise unchanged, is the one that check expects. The bundle is the google flavor for Google Play; nothing checks its reproducibility.

The export of an archive keeps only the entitlements that each bundle's signature carries: an unsigned archive exports without app groups, keychain groups or push. Xcode refuses ad-hoc signing for iOS devices ("Ad Hoc code signing is not allowed with SDK"), so `ios.sh` archives with `CODE_SIGNING_ALLOWED=NO`, then signs the extensions and the app ad hoc with `codesign`, each with its `.entitlements` file and `$(AppIdentifierPrefix)` replaced by the team id, and fails unless each signature carries exactly those entitlements. The release archive, signed with an Apple Development identity, carries the same entitlements plus `get-task-allow` and the identifiers that the export takes from the distribution profile. Signing for the App Store is the same as for that archive: on a Mac with the distribution certificate, open `SimpleX.xcarchive` in Xcode's Organizer and use Distribute App, or run `xcodebuild -exportArchive`.

## Known limits

- **The Android core relies on cache.zw3rk.com** for the cross-compiling toolchains and dependencies, as android-emu does: without it the build takes 12 to 24 hours, longer than a job may run. armv7a uses the GHC 8.10.7 toolchain, so its first build after an input change may be slower than aarch64's.
- **The iOS core library is the cabal build of ios-sim**, with the same files as the Nix package `aarch64-darwin-ios:lib:simplex-chat` that releases have shipped, but not built by Nix.
- Not run yet: the unsigned archive of this project and its export. Another project (super-productivity, Xcode 26.2) exported an archive signed this way with its app group kept; check the exported app with `codesign -d --entitlements - --xml Payload/SimpleX.app` and the same for `PlugIns/*.appex`.
