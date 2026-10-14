#!/bin/bash
# 把 AdsKiller.dylib 注入 SimpleQR 主程序并重新打包 IPA
#
# 用法:
#   ./inject_and_repack.sh <解包目录(含 Payload)> <AdsKiller.dylib 路径> [输出 ipa 名]
#
# 例:
#   ./inject_and_repack.sh ./decrypted ./AdsKiller.dylib SimpleQR-no-ads.ipa
#
# 依赖: insert_dylib (https://github.com/Tyilo/insert_dylib)
#       或 optool (https://github.com/alexzielenski/optool)
#       ldid 或 macOS 自带 codesign
set -euo pipefail

WORK="${1:?解包目录 (包含 Payload/SimpleQR.app)}"
DYLIB="${2:?AdsKiller.dylib 路径}"
OUTIPA="${3:-SimpleQR-no-ads.ipa}"
# 输出到调用时的当前目录（后面会 cd 进 WORK，所以先转成绝对路径）
case "$OUTIPA" in
  /*) OUTPATH="$OUTIPA" ;;
  *)  OUTPATH="$(pwd)/$OUTIPA" ;;
esac

APP="$WORK/Payload/SimpleQR.app"
BIN="$APP/SimpleQR"

[ -f "$BIN" ] || { echo "[!] 找不到主程序: $BIN"; exit 1; }
[ -f "$DYLIB" ] || { echo "[!] 找不到 dylib: $DYLIB"; exit 1; }

echo "[*] 1/4 拷贝 dylib 进 app bundle"
cp "$DYLIB" "$APP/AdsKiller.dylib"

echo "[*] 2/4 给主程序插入 LC_LOAD_DYLIB"
if command -v insert_dylib >/dev/null 2>&1; then
  # --strip-codesig 会在插入后移除原签名, 结尾再重签
  insert_dylib --strip-codesig --inplace @executable_path/AdsKiller.dylib "$BIN"
elif command -v optool >/dev/null 2>&1; then
  optool install -c load -p @executable_path/AdsKiller.dylib -t "$BIN"
else
  echo "[!] 没找到 insert_dylib / optool, 请先安装其中之一"; exit 1
fi

echo "[*] 3/4 重新签名"
if command -v ldid >/dev/null 2>&1; then
  # 保留原始 entitlements（IPA 解包后可用 ldid -e 从备份导出）
  ldid -S "$APP/AdsKiller.dylib" 2>/dev/null || true
  ldid -S "$BIN"
  ldid -S "$APP" 2>/dev/null || true
else
  codesign -f -s - --deep --timestamp=none "$APP"
fi

echo "[*] 4/4 打包 IPA"
rm -f "$OUTPATH"
( cd "$WORK" && zip -qry "$OUTPATH" Payload )
echo "[+] 完成: $OUTPATH"
echo
echo "安装方式(任选):"
echo "  * TrollStore  : 直接把 ipa 丢进 TrollStore 安装（自带签名，最省事）"
echo "  * Sideloadly  : 用你自己的 Apple ID 证书重签安装（会自动重签全部内容）"
echo "  * AltStore    : 同上"
