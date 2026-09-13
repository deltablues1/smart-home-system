#!/usr/bin/env bash
# Build and sign Jarvis Launcher without Gradle or the Android Studio SDK.
#
# Debian supplies the JDK, aapt, zipalign and apksigner:
#   sudo apt install -y default-jdk aapt zipalign apksigner
#
# Two pieces are fetched once and cached next to this script:
#   android.jar  compile stub for the android.* classes
#   r8.jar       Google's D8 dex compiler
#
# Do NOT install Debian's "dx" for the dex step — that package is IBM OpenDX,
# a scientific visualisation tool that happens to share the name. D8 is a plain
# jar, so it runs on the Pi's arm64 just as well as on x86.
#
#   ./build.sh   ->   build/jarvis-launcher.apk
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/build"
ANDROID_JAR="$HERE/android.jar"
R8_JAR="$HERE/r8.jar"
KEYSTORE="$HERE/jarvis-launcher.keystore"
STOREPASS="${JARVIS_KEYSTORE_PASS:-jarvis-launcher}"

R8_VERSION="${R8_VERSION:-8.7.18}"
R8_URL="https://maven.google.com/com/android/tools/r8/${R8_VERSION}/r8-${R8_VERSION}.jar"
# API 30 stub. Compiling against a newer platform than the TV runs is fine:
# the app only touches APIs that have existed since API 1.
ANDROID_JAR_URL="https://raw.githubusercontent.com/Sable/android-platforms/master/android-30/android.jar"

need() { command -v "$1" >/dev/null || { echo "nedostaje: $1 (sudo apt install $2)" >&2; exit 1; }; }
need javac default-jdk
need java default-jdk
need keytool default-jdk
need aapt aapt
need zipalign zipalign
need apksigner apksigner

[ -f "$ANDROID_JAR" ] || {
    echo "== dohvacam android.jar (jednokratno)"
    curl -fsSL "$ANDROID_JAR_URL" -o "$ANDROID_JAR"
}
[ -f "$R8_JAR" ] || {
    echo "== dohvacam D8 $R8_VERSION (jednokratno)"
    curl -fsSL "$R8_URL" -o "$R8_JAR"
}

rm -rf "$BUILD"
mkdir -p "$BUILD/classes" "$BUILD/apk"

echo "== javac"
javac -source 8 -target 8 -nowarn \
      -bootclasspath "$ANDROID_JAR" -classpath "$ANDROID_JAR" \
      -d "$BUILD/classes" \
      $(find "$HERE/src" -name '*.java')

echo "== dex (D8)"
java -cp "$R8_JAR" com.android.tools.r8.D8 \
     --lib "$ANDROID_JAR" --min-api 21 --output "$BUILD/apk" \
     $(find "$BUILD/classes" -name '*.class')

echo "== aapt: manifest -> apk"
aapt package -f -M "$HERE/AndroidManifest.xml" -I "$ANDROID_JAR" \
     -F "$BUILD/unsigned.apk"
( cd "$BUILD/apk" && aapt add -f "$BUILD/unsigned.apk" classes.dex >/dev/null )

echo "== potpis"
[ -f "$KEYSTORE" ] || keytool -genkeypair -v -keystore "$KEYSTORE" \
    -alias jarvis -keyalg RSA -keysize 2048 -validity 10950 \
    -storepass "$STOREPASS" -keypass "$STOREPASS" \
    -dname "CN=Jarvis Launcher, O=Home, C=HR" >/dev/null

zipalign -f 4 "$BUILD/unsigned.apk" "$BUILD/aligned.apk"
apksigner sign --ks "$KEYSTORE" --ks-key-alias jarvis \
    --ks-pass "pass:$STOREPASS" --key-pass "pass:$STOREPASS" \
    --out "$BUILD/jarvis-launcher.apk" "$BUILD/aligned.apk"
apksigner verify "$BUILD/jarvis-launcher.apk"

rm -f "$BUILD/unsigned.apk" "$BUILD/aligned.apk"
echo
echo "gotovo: $BUILD/jarvis-launcher.apk"
ls -la "$BUILD/jarvis-launcher.apk"
