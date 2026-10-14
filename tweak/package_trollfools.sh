#!/bin/bash
# package_trollfools.sh —— 把 AdsKiller 打成 TrollFools 可直接吃的注入插件
#
# 产出（在 tweak/dist/ 下）：
#   AdsKiller.dylib           裸 dylib（TrollFools 可直接选它注入）
#   AdsKiller.deb             MobileSubstrate 标准布局的 deb（TrollFools 推荐格式）
#   AdsKiller-trollfools.zip  上面两个的打包（便于传手机）
#
# 用法：在 macOS 上执行 ./package_trollfools.sh
set -euo pipefail

cd "$(dirname "$0")"

NAME=AdsKiller
BUNDLE_ID=com.sawadaru.qr
VERSION=1.0.0
DIST="$PWD/dist"
TPL="$PWD/trollfools"
STAGE="$PWD/build"

echo "[*] 1/5 编译 dylib（arm64）"
if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo "    SKIP_BUILD=1 —— 跳过编译，直接使用已有的 $NAME.dylib"
  [ -f "$NAME.dylib" ] || { echo "[!] 当前目录没有 $NAME.dylib"; exit 1; }
else
  ./build_dylib.sh
fi

echo "[*] 2/5 组装 MobileSubstrate 目录结构"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/Library/MobileSubstrate/DynamicLibraries"
cp -f "$NAME.dylib" "$STAGE/Library/MobileSubstrate/DynamicLibraries/$NAME.dylib"
cp -f "$TPL/Library/MobileSubstrate/DynamicLibraries/$NAME.plist" \
      "$STAGE/Library/MobileSubstrate/DynamicLibraries/$NAME.plist"
sed -e "s/@VERSION@/$VERSION/" "$TPL/DEBIAN/control" > "$STAGE/DEBIAN/control"

echo "[*] 3/5 签名 dylib"
if command -v ldid >/dev/null 2>&1; then
  ldid -S "$STAGE/Library/MobileSubstrate/DynamicLibraries/$NAME.dylib"
  echo "    ldid -S 完成"
else
  echo "    未安装 ldid —— 跳过（TrollFools 注入时会自己重签，通常没问题）"
  echo "    想要的话：brew install ldid"
fi

echo "[*] 4/5 打包 .deb"
mkdir -p "$DIST"
rm -f "$DIST/$NAME.deb"
if command -v dpkg-deb >/dev/null 2>&1; then
  dpkg-deb -b --root-owner-group "$STAGE" "$DIST/$NAME.deb" >/dev/null
  echo "    用 dpkg-deb 打包"
else
  # 跨平台打包器：不依赖 dpkg-deb / ar，产出标准 ar 容器的 deb
  PY=""
  for c in python3 python; do command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }; done
  if [ -n "$PY" ] && [ -f "$PWD/tools/mkdeb.py" ]; then
    "$PY" -I "$PWD/tools/mkdeb.py" "$STAGE" "$DIST/$NAME.deb"
    echo "    用 $PY (tools/mkdeb.py) 打包"
  elif command -v ar >/dev/null 2>&1; then
    # 最后的兜底：ar 容器(debian-binary + control.tar.gz + data.tar.gz)
    # COPYFILE_DISABLE 防止 macOS tar 写入 ._ AppleDouble 垃圾文件
    export COPYFILE_DISABLE=1
    ( cd "$STAGE/DEBIAN" && tar -czf "$STAGE/control.tar.gz" ./control )
    ( cd "$STAGE" && echo "2.0" > debian-binary && tar -czf "$STAGE/data.tar.gz" ./Library )
    ( cd "$STAGE" && ar rc "$DIST/$NAME.deb" debian-binary control.tar.gz data.tar.gz )
    rm -f "$STAGE/debian-binary" "$STAGE/control.tar.gz" "$STAGE/data.tar.gz"
    echo "    用 ar 打包"
  else
    echo "[!] 没有 dpkg-deb / python3 / ar，无法生成 .deb" >&2
    echo "    macOS 上 python3 随 Xcode CLT 自带，正常不会走到这里" >&2
    exit 1
  fi
fi

echo "[*] 5/5 附带裸 dylib 与 zip"
cp -f "$NAME.dylib" "$DIST/$NAME.dylib"
if command -v zip >/dev/null 2>&1; then
  ( cd "$DIST" && rm -f "$NAME-trollfools.zip" && zip -q -r "$NAME-trollfools.zip" "$NAME.dylib" "$NAME.deb" )
else
  echo "    未安装 zip —— 跳过 .zip（.deb 和 .dylib 已就绪，可单独传到手机）"
fi

echo
echo "[+] 产出："
ls -lh "$DIST"
echo
echo "TrollFools 用法："
echo "  1. 把 AdsKiller-trollfools.zip（或 AdsKiller.deb）传到 iPhone（AirDrop / 文件 App / 微信文件传输）"
echo "  2. 解压后用文件 App 打开 AdsKiller.deb -> 共享 -> 用 TrollFools 打开"
echo "     （TrollFools 里也可以直接「+」选择 .dylib 文件）"
echo "  3. 在 TrollFools 列表里选中 QR Code (SimpleQR) -> 注入"
echo "  4. 注入完成后从 TrollFools 里启动 App"
