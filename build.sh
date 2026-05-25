#!/usr/bin/env bash
# Build a debug APK without Gradle. Fast iteration loop.
set -euo pipefail

cd "$(dirname "$0")"

export ANDROID_HOME=${ANDROID_HOME:-/home/phil/android-sdk}
BT=$ANDROID_HOME/build-tools/33.0.2
PLATFORM=$ANDROID_HOME/platforms/android-33
ANDROID_JAR=$PLATFORM/android.jar

PKG=com.example.firehello
OUT=build
APK_UNSIGNED=$OUT/app-unsigned.apk
APK_ALIGNED=$OUT/app-aligned.apk
APK=$OUT/app-debug.apk
KEYSTORE=$OUT/debug.keystore

rm -rf $OUT
mkdir -p $OUT/gen $OUT/obj $OUT/dex

echo "==> aapt2 compile resources"
$BT/aapt2 compile --dir res -o $OUT/res.zip

echo "==> aapt2 link"
$BT/aapt2 link \
    -I $ANDROID_JAR \
    --manifest AndroidManifest.xml \
    -o $APK_UNSIGNED \
    --java $OUT/gen \
    $OUT/res.zip

echo "==> javac"
find src -name "*.java" > $OUT/sources.txt
find $OUT/gen -name "*.java" >> $OUT/sources.txt
javac -d $OUT/obj \
    -classpath $ANDROID_JAR \
    -source 1.8 -target 1.8 \
    @$OUT/sources.txt

echo "==> d8 (dex)"
$BT/d8 --output $OUT/dex --lib $ANDROID_JAR \
    $(find $OUT/obj -name "*.class")

echo "==> add classes.dex to apk"
(cd $OUT/dex && zip -q -u ../../$APK_UNSIGNED classes.dex)

echo "==> zipalign"
$BT/zipalign -f 4 $APK_UNSIGNED $APK_ALIGNED

if [ ! -f $KEYSTORE ]; then
  echo "==> generate debug keystore"
  keytool -genkeypair -v \
    -keystore $KEYSTORE -storepass android -alias androiddebugkey \
    -keypass android -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null
fi

echo "==> apksigner sign"
$BT/apksigner sign \
    --ks $KEYSTORE --ks-pass pass:android \
    --key-pass pass:android --ks-key-alias androiddebugkey \
    --out $APK $APK_ALIGNED

echo "==> done: $APK"
ls -lh $APK
