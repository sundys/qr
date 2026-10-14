#!/bin/bash
# 在 macOS 上构建 AdsKiller.dylib（只需要 Xcode Command Line Tools，不需要 Theos）
# 用法: ./build_dylib.sh
set -euo pipefail

cd "$(dirname "$0")"

SDK="$(xcrun --sdk iphoneos --show-sdk-path)"

xcrun --sdk iphoneos clang \
  -arch arm64 \
  -miphoneos-version-min=14.0 \
  -isysroot "$SDK" \
  -dynamiclib \
  -O2 \
  -fobjc-arc \
  -fmodules \
  -framework Foundation \
  -framework UIKit \
  -install_name @executable_path/AdsKiller.dylib \
  -o AdsKiller.dylib \
  AdsKiller.m

echo "[+] built: $(pwd)/AdsKiller.dylib"
file AdsKiller.dylib
