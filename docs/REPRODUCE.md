---
title: Verify and reproduce builds
permalink: /reproduce/index.html
revision: 31.10.2026
---

# Verifying and reproducing release builds

- [Obtain release signing key](#obtain-release-signing-key)
- [Verify release signature](#verify-release-signature)
- [How to reproduce builds](#how-to-reproduce-builds)
   - [Server binaries](#server-binaries)
   - [Linux desktop apps and CLI](#linux-desktop-apps-and-cli)
   - [Android apps](#android-apps)
   - [macOS desktop app and CLI](#macos-desktop-app-and-cli)
   - [Windows desktop app and CLI](#windows-desktop-app-and-cli)
   - [iOS app](#ios-app)
   - [Compare one file](#compare-one-file)
   - [Releases of forks](#releases-of-forks)

## Obtain release signing key

To verify the signature of `_sha256sums` or apks you need to obtain the signing key. You can do it from keyservers:

```sh
gpg --keyserver hkps://keys.openpgp.org --search build@simplex.chat
gpg --keyserver hkps://keyserver.ubuntu.com --search build@simplex.chat
```

```sh
gpg --list-keys build@simplex.chat
```

Once you obtain the signing key, verify that its fingerprint is:

```
BBDF 7BDA D154 8B16 836A F5B9 D53B DFD1 53C3 66BA
```

Additionally, compare the key fingerprint with:

- [simplexchat.eth](https://app.ens.domains/simplexchat.eth) (release key record)
- [Mastodon](https://mastodon.social/@simplex) (profile)
- [Reddit](https://www.reddit.com/r/SimpleXChat/) (side panel)

You can set the imported key as "ultimately trusted":

```sh
echo -e "trust\n5\ny\nquit" | gpg --command-fd 0 --edit-key build@simplex.chat
```

## Verify release signature

**Linux desktop apps and CLI**:

Download the file with executable hashes and the signature. For example, to verify the `v6.5.0-beta.3` release:

```sh
curl -LO 'https://github.com/simplex-chat/simplex-chat/releases/download/v6.5.0-beta.3/_sha256sums.asc'
curl -LO 'https://github.com/simplex-chat/simplex-chat/releases/download/v6.5.0-beta.3/_sha256sums'
```

Verify the signature:

```sh
gpg --verify _sha256sums.asc _sha256sums
```

**Android APKs**:

Download the APK files and signatures. For example, to verify the `v6.5.0-beta.3` release:

```sh
curl -LO 'https://github.com/simplex-chat/simplex-chat/releases/download/v6.5.0-beta.3/simplex-aarch64.apk'
curl -LO 'https://github.com/simplex-chat/simplex-chat/releases/download/v6.5.0-beta.3/_simplex-aarch64.apk.asc'
curl -LO 'https://github.com/simplex-chat/simplex-chat/releases/download/v6.5.0-beta.3/simplex-armv7a.apk'
curl -LO 'https://github.com/simplex-chat/simplex-chat/releases/download/v6.5.0-beta.3/_simplex-armv7a.apk.asc'
```

Verify the signatures:

```sh
gpg --verify _simplex-armv7a.apk.asc simplex-armv7a.apk
gpg --verify _simplex-aarch64.apk.asc simplex-aarch64.apk
```

## How to reproduce builds

To reproduce the build you must have:

- Linux machine
- `x86-64` architecture
- Installed `docker`, `curl` and `git`, and Python 3.12 or newer for the apps

The macOS, Windows and iOS apps are reproduced on their own platforms, as their sections describe.

### Server binaries

1. Download script:

   ```sh
   curl -LO 'https://raw.githubusercontent.com/simplex-chat/simplexmq/refs/heads/master/scripts/simplexmq-reproduce-builds.sh'
   ```

2. Make it executable:

   ```sh
   chmod +x simplexmq-reproduce-builds.sh
   ```

3. Execute the script with the required tag:

   ```sh
   ./simplexmq-reproduce-builds.sh 'v6.3.1'
   ```

   The script executes these steps (please review the script to confirm):

   1) builds all server binaries for the release in docker container.
   2) downloads binaries from the same GitHub release and compares them with the built binaries.
   3) if they all match, generates _sha256sums file with their checksums.

   This will take a while.

4. After compilation, you should see the folder named as the tag and repository name (e.g., `v6.3.1-simplexmq`) with two subfolders:

   ```sh
   ls v6.3.1-simplexmq
   ```

   ```sh
   from-source  prebuilt  _sha256sums
   ```

   The file _sha256sums contains the hashes of all builds - you can compare it with the same file in GitHub release.

### Linux desktop apps and CLI

1. Download the script with its `simplex_build` package:

   ```sh
   git clone --depth 1 https://github.com/simplex-chat/simplex-chat simplex-chat-scripts
   ```

2. Execute the script with the required tag:

   ```sh
   simplex-chat-scripts/scripts/simplex-chat-build.py reproduce 'v6.4.8'
   ```

   The script executes these steps (please review the script to confirm):

   1) builds all Linux CLI and Desktop binaries for the release in docker container.
   2) downloads binaries from the same GitHub release and compares them with the built binaries.
   3) if they all match, generates _sha256sums file with their checksums.

   This will take a while.

3. After compilation, you should see the folder named as the tag and repository name (e.g., `v6.4.8-simplex-chat`) with two subfolders:

   ```sh
   ls v6.4.8-simplex-chat
   ```

   ```sh
   from-source  prebuilt  _sha256sums
   ```

   The file _sha256sums contains the hashes of all builds - you can compare it with the same file in GitHub release.

### Android apps

In addition to basic requirements, Android build will:

- Take ~150gb of disc space
- Take ~20h to build all the architectures (depends on core count)
- Require at least 16gb of RAM

1. Download the script with its `simplex_build` package:

   ```sh
   git clone --depth 1 https://github.com/simplex-chat/simplex-chat simplex-chat-scripts
   ```

2. Execute the script with the required tag and the `android` target:

   ```sh
   simplex-chat-scripts/scripts/simplex-chat-build.py reproduce 'v6.5.0-beta.3' android
   ```

   `--arches aarch64` or `--arches armv7a` builds one architecture. `--nix-cache` downloads the Nix toolchains and dependencies from the zw3rk and IOG caches instead of building them, which takes hours instead of a day; the paths are the same, built by those caches' builders. Running `simplex-chat-scripts/scripts/simplex-chat-build.py reproduce 'v6.5.0-beta.3' desktop android` reproduces both.

   The script executes these steps (please review the script to confirm):

   1) Downloads and checks that APKs from GitHub are signed with valid key.
   2) Builds Android APKs in a docker container.
   3) Compares the releases by copying the signature from downloaded APKs to locally built APKs.
   4) If the resulting build is bit-by-bit identical, prints the message that this tag was reproduced.

   The results are in the folder named as the tag and repository name (e.g., `v6.5.0-beta.3-simplex-chat/android`).

   This will take a while.

### macOS desktop app and CLI

The released dmg is signed with the Developer ID of SimpleX Chat Ltd and notarized by Apple, so its files can't be the same as built ones. The script builds them unsigned and checks that the signatures are their only difference. Releases whose dmg was made by jpackage, before the script made it, are not reproduced.

You need:

- a Mac of the release files' architecture: Apple silicon for `aarch64`, Intel for `x86_64`
- Xcode 26.6, which the releases are built with, selected with `xcode-select` or `DEVELOPER_DIR`, its command line tools, `cmake`, `curl`, `git` and Python 3.12 or newer
- GHC 9.6.3 and cabal 3.10.2.0, e.g. installed with `ghcup`
- the Amazon Corretto JDK 17.0.21.0.10 that the releases are built with, in `JAVA_HOME`

1. Download the script with its `simplex_build` package:

   ```sh
   git clone --depth 1 https://github.com/simplex-chat/simplex-chat simplex-chat-scripts
   ```

2. Execute the script with the required tag and the `macos` target:

   ```sh
   simplex-chat-scripts/scripts/simplex-chat-build.py reproduce 'v7.2.0' macos
   ```

   The script executes these steps (please review the script to confirm):

   1) Downloads the CLI and the dmg of the Mac's architecture from the GitHub release.
   2) Builds them from the tag, unsigned, in a copy of the checkout in `/private/tmp/simplex-chat-build`, so that they don't depend on the checkout's path.
   3) Compares the CLI: the release's code signature is copied onto the built CLI as `codesign` adds it, and the result must be the released CLI byte for byte.
   4) Compares the dmgs' volumes, file by file. Each executable and library gets the release's code signature copied onto it in the same way, and must then be the released file. The jars with native libraries hold signed copies of the libraries, so their entries are compared one by one in the same way; their names, which the build derives from their content, and the launcher's classpath that lists them, may differ by that name. jpackage's `.jpackage.xml` may differ in whether the app is signed. The `_CodeSignature` folders of the bundles are part of their signatures. All other files, folders, links and their permissions must be the same.
   5) Makes the dmg again from the released app, as the build makes it, copies the release's signature onto it and checks that it is the released dmg byte for byte. A dmg's signature, with the notarization ticket, is the only part between its contents and its last 512-byte block.
   6) Checks the signatures of the dmg and its app with `codesign`: they must be Developer ID signatures of the SimpleX Chat team, `5NN7GUYB6T`.
   7) If all match, writes `_sha256sums` with the hashes of the released files.

   The results are in the folder `v7.2.0-simplex-chat/macos`.

The notarization ticket is not checked by the script: macOS checks it when the app is opened.

### Windows desktop app and CLI

You need:

- a Windows x86-64 machine with 16gb of RAM, or of RAM and page file
- [MSYS2](https://www.msys2.org) with the UCRT64 packages `git`, `perl`, `make`, `mingw-w64-ucrt-x86_64-toolchain`, `mingw-w64-ucrt-x86_64-cmake` and `mingw-w64-ucrt-x86_64-python`
- GHC 9.6.3 and cabal 3.10.2.0, e.g. installed with `ghcup`, on the `PATH` of the UCRT64 shell
- the Amazon Corretto JDK 17.0.21.0.10 that the releases are built with, in `JAVA_HOME`

1. In a UCRT64 shell, download the script with its `simplex_build` package:

   ```sh
   git clone --depth 1 https://github.com/simplex-chat/simplex-chat simplex-chat-scripts
   ```

2. Execute the script with the required tag and the `windows` target:

   ```sh
   python simplex-chat-scripts/scripts/simplex-chat-build.py reproduce 'v7.2.0' windows
   ```

   The script executes these steps (please review the script to confirm):

   1) Downloads the CLI and the msi from the GitHub release.
   2) Builds them from the tag, in a copy of the checkout in `C:/simplex-chat-build`, which any user can create, so that they don't depend on the checkout's path.
   3) Compares the CLI: when it is signed with Authenticode, the release's certificate table is appended to the built CLI as `signtool` appends it, with the PE checksum that `signtool` updates, and the result must be the released CLI byte for byte.
   4) Compares the msi: each of its streams and storages, with the files that its cabinet holds, must be the same, but the signature streams `signtool` adds and the time of the msi's root storage, which `signtool` sets when it saves the msi.
   5) If all match, writes `_sha256sums` with the hashes of the released files.

   The results are in the folder `v7.2.0-simplex-chat/windows`.

The releases are not signed with Authenticode now, so the CLI and the msi must be the same as the built ones. The script doesn't check who signed a signed file: Windows checks the signature when the msi is installed.

### iOS app

The App Store app can't be compared with a build: Apple signs it again and encrypts its executables for each device, and it can't be downloaded from GitHub. What a release can publish, and the script reproduces, is the unsigned archive that the App Store app is exported from, and the core library package it links:

- `SimpleX.xcarchive.zip`
- `pkg-ios-aarch64-swift-json.zip`

The script builds them from the tag and checks that they are the released files byte for byte. This proves that the published archive is built from the published source; that the App Store app is exported from this archive is not verified.

You need:

- a Mac with Apple silicon, with Xcode 26.6, which the releases are built with, at `/Applications/Xcode.app`, where the archive's debug symbols name it: the script refuses a release archived with another Xcode before it builds
- Nix, or the `--install-nix` option, which installs it
- the brand assets of the release, a checkout of `simplex-chat-art`, passed with `--art`: an archive built without them has the default icons and differs from the release

```sh
simplex-chat-scripts/scripts/simplex-chat-build.py reproduce 'v7.2.0' ios --art simplex-chat-art
```

The results are in the folder `v7.2.0-simplex-chat/ios`.

### Compare one file

To compare a released file with one built by the script's `build` command, without building again:

```sh
simplex-chat-scripts/scripts/simplex-chat-build.py compare simplex-desktop-macos-aarch64.dmg built/simplex-desktop-macos-aarch64.dmg
```

The comparison is the one of `reproduce` for the file's format: a dmg, on macOS; an msi; a Mach-O or PE executable or library; any other file must be the same byte for byte.

### Releases of forks

`--repo <owner>/<name>` reproduces a release of another GitHub repository, e.g. a fork that builds its own releases with this script:

```sh
simplex-chat-scripts/scripts/simplex-chat-build.py reproduce 'v7.1.99-repro-test' macos --repo sh-agents/simplex-chat
```

The script clones that repository and downloads its release; the results are in the folder named as the tag and the repository's name. The signers of a fork's files are not checked: its APKs need not be signed with the SimpleX key, nor its dmg with the SimpleX team's Developer ID.
