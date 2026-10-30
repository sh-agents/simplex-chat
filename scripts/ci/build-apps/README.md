# App builds for signing

`.github/workflows/build-apps.yml` builds the Android and iOS apps of a commit without signing them and uploads them as artifacts, to be signed outside CI. No signing key or certificate reaches GitHub.

Each job runs `scripts/simplex-chat-reproduce-builds.py build`, the script that also reproduces releases ([docs/REPRODUCE.md](../../../docs/REPRODUCE.md)), so a reproduce run rebuilds an APK exactly as this workflow built it. The same commands build locally: `build android-apk <arch> --out <dir>`, `build android-bundle --libs <dir> --out <dir>` with Docker, and `build ios-archive --art <simplex-chat-art> --out <dir>` on a Mac with Xcode and Nix.

## Running it

The workflow runs only when started by hand, on any branch: in the Actions tab, `build-apps` → Run workflow, or `gh workflow run build-apps.yml --ref <branch>`. A newer run on the same branch cancels the older one. Start it on master or a branch of master: armv7a is built from the same commit, which `stable` only supports once it carries the armv7a build of master.

It needs the repository secret `SIMPLEX_ART_SSH_KEY`, the private half of a read-only deploy key of simplex-chat/simplex-chat-art, for the iOS brand assets; without it the iOS job fails at "Checkout brand assets".

## Jobs and artifacts

| Job | Runner | Builds | Artifact, kept 30 days |
|---|---|---|---|
| Android aarch64 APK, Android armv7a APK | ubuntu-24.04 | `build android-apk <arch>`: `scripts/android/build-android.sh` in the `Dockerfile.build` container | `android-<arch>-unsigned`: `simplex-<arch>.apk`, `pkg-<arch>-android-libsimplex.zip`, `pkg-<arch>-android-libsupport.zip`, `SHA256SUMS` |
| Android Play bundle | ubuntu-24.04 | `build android-bundle`: `:android:bundleGoogleRelease` in the same container, with both arches' core libraries from the APK jobs and release-mobile.py's `local.properties` | `android-bundle-unsigned`: `simplex.aab`, `SHA256SUMS` |
| iOS archive | macos-26 | `build ios-archive`: the core library `aarch64-darwin-ios:lib:simplex-chat` with Nix, the brand assets of simplex-chat-art, then `xcodebuild archive` of the Release configuration for devices, unsigned, and each bundle signed ad hoc with its entitlements | `ios-archive-unsigned`: `SimpleX.xcarchive.zip`, `SHA256SUMS` |

`gh run download <run id> -n <artifact>` downloads one. The version and build numbers are the commit's: bump them before the run.

## Android: signing reproducibly

The APKs are the foss flavor, as `scripts/android/build-android.sh` builds them: AGP's unsigned output, which is deterministic. Two runs on different runners built byte-identical APKs, core libraries and bundles.

Releases are signed by Gradle, with the release signing config that `apps/multiplatform/android/build.gradle.kts` reads from `ANDROID_KEYSTORE`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS` and `ANDROID_KEY_PASSWORD`, or with the `android.injected.signing.*` properties that release-mobile.py passes. With the variables set, `build android-apk` and `build android-bundle` mount the keystore read-only and pass the passwords to Docker by name, so that they are in no command line; without them the script and Gradle warn that the build is unsigned. CI never has the key. An APK of this artifact signed outside Gradle stays verifiable when its layout is kept: `apksigner sign --alignment-preserved`.

The reproduce script rebuilds the APK, copies the release's signature onto it with `apksigcopier` and compares the two. Releases signed before Gradle signed them, by apksigner after a repack, are checked by aligning the rebuilt APK to 16 KB pages with `repro-apk zipalign` first, padded as the released APK is (apksigner 35 and later pad with their `0xd935` extra field, older ones with zeros), and restoring the ZIP flags of the v1 signature files that apksigcopier 1.1.1 drops.

The bundle is the google flavor for Google Play; nothing checks its reproducibility.

## iOS: signing at export

The export of an archive keeps only the entitlements that each bundle's signature carries: an unsigned archive exports without app groups, keychain groups or push. Xcode refuses ad-hoc signing for iOS devices ("Ad Hoc code signing is not allowed with SDK"), so the script archives with `CODE_SIGNING_ALLOWED=NO`, then signs the extensions and the app ad hoc with `codesign`, each with its `.entitlements` file and `$(AppIdentifierPrefix)` replaced by the team id, and fails unless each signature carries exactly those entitlements. It records the team in the archive's Info.plist, as Xcode does when it signs.

Compared with the 7.1-beta.7 release archive, which Xcode signed with an Apple Development identity, the bundles have the same files except the development profiles (`embedded.mobileprovision`), and the same entitlements except `application-identifier`, `com.apple.developer.team-identifier` and `get-task-allow`, which the export takes from the distribution profile.

To sign for the App Store: on a Mac with the distribution certificate, unzip the archive, open `SimpleX.xcarchive` in Xcode's Organizer and use Distribute App, or run `xcodebuild -exportArchive`.

It also fails when an object of the core library is marked for a newer iOS than the app's deployment target: mac2ios relabels the library's macOS objects as iOS and keeps their minimum version.

## Known limits

- **The core libraries rely on cache.zw3rk.com** for the cross-compiling toolchains and dependencies, as android-emu does: without it they take 12 to 24 hours, longer than a job may run. On a recent master commit only simplexmq and simplex-chat are built. armv7a uses GHC 8.10.7, whose Template Haskell runs under qemu, so it takes longer than aarch64.
- Not tested yet: the export and App Store upload of an archive from this workflow; check the exported app with `codesign -d --entitlements - --xml Payload/SimpleX.app` and the same for `PlugIns/*.appex`.
