#!/bin/bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
package_root="$project_root/macos"
sdk_path="${MACOS_SDK_PATH:-/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk}"
cache_root="${TMPDIR:-/private/tmp}/class-knowledge-native-build"

mkdir -p "$cache_root/swift-modules" "$cache_root/clang-modules" "$cache_root/swiftpm-cache" "$cache_root/swiftpm-config"

cd "$package_root"
SDKROOT="$sdk_path" \
SWIFT_MODULE_CACHE_PATH="$cache_root/swift-modules" \
CLANG_MODULE_CACHE_PATH="$cache_root/clang-modules" \
swift build \
  --cache-path "$cache_root/swiftpm-cache" \
  --config-path "$cache_root/swiftpm-config" \
  --scratch-path .build \
  --disable-sandbox

binary_path="$package_root/.build/out/Products/Debug/ClassKnowledgeApp"
if [[ ! -f "$binary_path" ]]; then
  binary_path="$package_root/.build/debug/ClassKnowledgeApp"
fi
app_path="$package_root/Build/Class Knowledge.app"
contents="$app_path/Contents"
rm -rf "$app_path"
mkdir -p "$contents/MacOS"
cp "$binary_path" "$contents/MacOS/ClassKnowledgeApp"
mkdir -p "$contents/Resources"
cp -R "$package_root/Sources/ClassKnowledgeApp/Resources/zh-Hans.lproj" "$contents/Resources/"

cat > "$contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleExecutable</key>
    <string>ClassKnowledgeApp</string>
    <key>CFBundleIdentifier</key>
    <string>local.classknowledge.library.dev</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>Class Knowledge Dev</string>
    <key>CFBundleDisplayName</key>
    <string>Class Knowledge Dev</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

/usr/bin/xattr -cr "$app_path"
/usr/bin/codesign --force --deep --sign - "$app_path"
/usr/bin/codesign --verify --deep --strict "$app_path"
echo "Built $app_path"
