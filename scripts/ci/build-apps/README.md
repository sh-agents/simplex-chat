# App builds for signing

`.github/workflows/build-apps.yml` builds the Android and iOS apps of a commit without signing them and uploads them as artifacts, to be signed outside CI. No signing key or certificate reaches GitHub.

## Running it

The workflow runs only when started by hand, on any branch: in the Actions tab, `build-apps` → Run workflow, or `gh workflow run build-apps.yml --ref <branch>`. A newer run on the same branch cancels the older one. Start it on master or a branch of master: armv7a is built from the same commit, which `stable` only supports once it carries the armv7a build of master.

It needs the repository secret `SIMPLEX_ART_SSH_KEY`, the private half of a read-only deploy key of simplex-chat/simplex-chat-art, for the iOS brand assets; without it the iOS job fails at "Checkout brand assets".

## Jobs and artifacts

| Job | Runner | Builds | Artifact, kept 30 days |
|---|---|---|---|
| Android aarch64 APK, Android armv7a APK | ubuntu-24.04 | `android.sh apk <arch>`: `scripts/android/build-android.sh` in the `Dockerfile.build` container | `android-<arch>-unsigned`: `simplex-<arch>.apk`, `pkg-<arch>-android-libsimplex.zip`, `pkg-<arch>-android-libsupport.zip`, `SHA256SUMS` |
| Android Play bundle | ubuntu-24.04 | `android.sh bundle`: `:android:bundleGoogleRelease` in the same container, with both arches' core libraries from the APK jobs | `android-bundle-unsigned`: `simplex.aab`, `SHA256SUMS` |
| iOS archive | macos-26 | `ios.sh`: the core library `aarch64-darwin-ios:lib:simplex-chat` with Nix, the brand assets of simplex-chat-art, then `xcodebuild archive` of the Release configuration for devices, unsigned, and each bundle signed ad hoc with its entitlements | `ios-archive-unsigned`: `SimpleX.xcarchive.zip`, `SHA256SUMS` |

`gh run download <run id> -n <artifact>` downloads one. The version and build numbers are the commit's: bump them before the run.

## Android: signing reproducibly

The APKs are the foss flavor, as `scripts/android/build-android.sh` builds them: deterministic, aligned to 4 KB pages and unsigned. Two runs on different runners built byte-identical APKs and core libraries.

`scripts/simplex-chat-reproduce-builds-android.sh` rebuilds the APK, aligns it to 16 KB pages with `repro-apk zipalign --page-size 16`, copies the released APK's signature onto it with `apksigcopier`, and compares the two. An APK from this artifact signed this way passes that check, tested with apksigner 37.0.0 and the script's apksigcopier 1.1.1 and repro-apk 0.3.0:

```sh
repro-apk zipalign --page-size 16 simplex-<arch>.apk aligned.apk
apksigner sign --alignment-preserved --v1-signing-enabled false --ks <keystore> --out simplex-<arch>.apk aligned.apk
```

- `--alignment-preserved`: apksigner 35 and later otherwise replace the zip padding.
- `--v1-signing-enabled false`: apksigner 35 and later set the UTF-8 flag on the v1 signature files, which apksigcopier 1.1.1 drops, so the copy differs in 6 bytes. The app's minSdk is 26 and Android uses v1 signatures only below 24.

The check rebuilds the core library and the app from one commit, as this workflow does. The 7.1-beta.7 release APK, built by release-mobile.py, has the core library of the last commit that changed simplex-chat.cabal (5a02b906e) and the app of the tag, which a later core commit separates; built from the tag, this workflow's APK matches the release in every file except `libsimplex.so`. That release is also signed with apksigner's padding and a v1 signature, which the reproduce script's alignment step does not expect.

The bundle is the google flavor for Google Play; nothing checks its reproducibility.

## iOS: signing at export

The export of an archive keeps only the entitlements that each bundle's signature carries: an unsigned archive exports without app groups, keychain groups or push. Xcode refuses ad-hoc signing for iOS devices ("Ad Hoc code signing is not allowed with SDK"), so `ios.sh` archives with `CODE_SIGNING_ALLOWED=NO`, then signs the extensions and the app ad hoc with `codesign`, each with its `.entitlements` file and `$(AppIdentifierPrefix)` replaced by the team id, and fails unless each signature carries exactly those entitlements. It records the team in the archive's Info.plist, as Xcode does when it signs.

Compared with the 7.1-beta.7 release archive, which Xcode signed with an Apple Development identity, the bundles have the same files except the development profiles (`embedded.mobileprovision`), and the same entitlements except `application-identifier`, `com.apple.developer.team-identifier` and `get-task-allow`, which the export takes from the distribution profile.

To sign for the App Store: on a Mac with the distribution certificate, unzip the archive, open `SimpleX.xcarchive` in Xcode's Organizer and use Distribute App, or run `xcodebuild -exportArchive`.

`ios.sh` also fails when an object of the core library is marked for a newer iOS than the app's deployment target: mac2ios relabels the library's macOS objects as iOS and keeps their minimum version.

## Known limits

- **The core libraries rely on cache.zw3rk.com** for the cross-compiling toolchains and dependencies, as android-emu does: without it they take 12 to 24 hours, longer than a job may run. On a recent master commit only simplexmq and simplex-chat are built. armv7a uses GHC 8.10.7, whose Template Haskell runs under qemu, so it takes longer than aarch64.
- Not tested yet: the export and App Store upload of an archive from this workflow; check the exported app with `codesign -d --entitlements - --xml Payload/SimpleX.app` and the same for `PlugIns/*.appex`.
