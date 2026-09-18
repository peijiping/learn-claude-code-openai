#!/usr/bin/env bash
# 组装 macOS 应用「个人AI助手.app」，统一系统展示的图标与名称。
# 用法：cd frontend && bash scripts/package_mac.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
APP_NAME="个人AI助手"
DIST_DIR="$ROOT/dist"
APP="$DIST_DIR/$APP_NAME.app"
SRC_APP="$ROOT/node_modules/electron/dist/Electron.app"
PBY="/usr/libexec/PlistBuddy"

echo "[1/4] 是否已存在 out/ 构建产物？"
if [ ! -f "$ROOT/out/main/index.js" ]; then
  echo "缺少 out/，先执行 electron-vite build"
  npm run build
fi

echo "[2/4] 组装 $APP_NAME.app（基于本地 Electron.app，离线）"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp -R "$SRC_APP/Contents/Frameworks" "$APP/Contents/"
cp -R "$SRC_APP/Contents/MacOS"/.     "$APP/Contents/MacOS/"
cp -R "$SRC_APP/Contents"/[a-z]*.lproj "$APP/Contents/Resources/" 2>/dev/null || true
cp    "$SRC_APP/Contents/Info.plist"  "$APP/Contents/Info.plist"
cp    "$SRC_APP/Contents/PkgInfo"     "$APP/Contents/PkgInfo" 2>/dev/null || true

# 图标（.icns 提供给 Finder / Dock / Launchpad / 强制退出等）
cp "$ROOT/build/icon.icns" "$APP/Contents/Resources/icon.icns"

# 应用代码 + 打包后主进程仍要读取的 dock 图标
mkdir -p "$APP/Contents/Resources/app/build"
cp -R "$ROOT/out/." "$APP/Contents/Resources/app/"
cp "$ROOT/build/icon.png" "$APP/Contents/Resources/app/build/icon.png"
# Electron 依据 app/package.json 的 main 定位入口（out 被扁平复制后入口变为 ./main/index.js）
printf '{\n  "name": "frontend",\n  "main": "./main/index.js"\n}\n' > \
  "$APP/Contents/Resources/app/package.json"

echo "[3/4] 改写 Info.plist 应用名 / 图标 / bundle id"
PL="$APP/Contents/Info.plist"
set_plist() { # key type value
  local k="$1" t="$2" v="$3"
  if /usr/libexec/PlistBuddy -c "Print :$k" "$PL" >/dev/null 2>&1; then
    $PBY -c "Set :$k $v" "$PL"
  else
    $PBY -c "Add :$k $t $v" "$PL"
  fi
}
set_plist ":CFBundleName"            string "$APP_NAME"
set_plist ":CFBundleDisplayName"     string "$APP_NAME"
set_plist ":CFBundleIdentifier"      string "com.aigent.desktop"
set_plist ":CFBundleExecutable"      string "Electron"
set_plist ":CFBundleIconFile"        string "icon.icns"
set_plist ":CFBundleShortVersionString" string "0.1.0"
set_plist ":CFBundleVersion"         string "0.1.0"

echo "[4/4] ad-hoc 深层签名（修正复制导致的签名失效，避免 Gatekeeper 拦截）"
codesign --force --deep --sign - "$APP"
echo "完成：$APP"