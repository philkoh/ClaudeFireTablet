---
name: reference-android-sdk
description: "Android SDK location, build pipeline, and system packages used by the Fire tablet project"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 56052ee6-6721-45b2-ac0d-5d39b2a23e80
---

## SDK install
Android SDK at `/home/phil/android-sdk`:
- `cmdline-tools/latest/bin/sdkmanager`
- `build-tools/33.0.2/` — aapt2, d8, zipalign, apksigner
- `platforms/android-33/android.jar` — compile classpath
- `platform-tools/adb` — also installed at `/usr/bin/adb` via apt `android-tools-adb`

## System packages (apt, already installed)
- `openjdk-17-jdk-headless` — JDK 17
- `android-tools-adb` — `/usr/bin/adb`
- `ffmpeg` — provides `ffplay` for the PC-side player
- `wget`, `unzip` — for fetching the SDK

## Sudo
User's `phil` account has passwordless sudo (`sudo -n` returns 0).

## udev
`/etc/udev/rules.d/51-android-fire.rules` allows the `plugdev` group to access Lab126 vendor (1949) devices over USB.

## Build pipeline
`/home/phil/ClaudeFireTablet/build.sh` runs aapt2 compile → aapt2 link → javac → d8 → zipalign → apksigner. No Gradle. ~3-second iteration. Sources are auto-discovered from `src/` and from the aapt2-generated `R.java` in `build/gen/`.

If a new dependency is needed (an AndroidX or Google library), this pipeline won't pull jars automatically — either drop the jar in a `libs/` dir and add it to the javac/d8 classpath, or migrate to Gradle.
