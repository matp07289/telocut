#!/usr/bin/env bash
# =============================================================================
# DavinTelop macOS ビルドスクリプト
# 生成物: dist/DavinTelop.app  /  dist/DavinTelop-1.0.0.pkg
#
# 前提:
#   pip3 install pyinstaller pillow
#   Xcode Command Line Tools (codesign / pkgbuild)
# =============================================================================
set -euo pipefail

VERSION="1.0.0"
APP_NAME="DavinTelop"
BUNDLE_ID="com.davintelop.app"

# プロジェクトルートへ移動
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

echo "============================================="
echo "  DavinTelop macOS Build v${VERSION}"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================="

# ---- 依存チェック ----
check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        echo "ERROR: '$1' が見つかりません。${2}"
        exit 1
    fi
}

check_cmd python3   "Python 3 をインストールしてください"
check_cmd pyinstaller "pip3 install pyinstaller"
check_cmd codesign  "Xcode Command Line Tools: xcode-select --install"
check_cmd pkgbuild  "Xcode Command Line Tools: xcode-select --install"

python3 -c "import PIL" 2>/dev/null || {
    echo "ERROR: Pillow が必要です: pip3 install Pillow"
    exit 1
}

echo ""
echo "[1/4] アイコン生成..."
python3 build/create_icon.py mac

echo ""
echo "[2/4] PyInstaller ビルド..."
pyinstaller davintelop_mac.spec --noconfirm --clean

APP_PATH="dist/${APP_NAME}.app"
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: ビルド失敗 — ${APP_PATH} が見つかりません"
    exit 1
fi
echo "  ✓ ${APP_PATH}"

echo ""
echo "[3/4] アドホック コードサイン..."
codesign \
    --force \
    --deep \
    --sign - \
    --options runtime \
    "$APP_PATH"
echo "  ✓ コードサイン完了"

echo ""
echo "[4/4] PKG パッケージ作成..."
PKG_PATH="dist/${APP_NAME}-${VERSION}.pkg"
pkgbuild \
    --component "$APP_PATH" \
    --install-location /Applications \
    --identifier "$BUNDLE_ID" \
    --version "$VERSION" \
    "$PKG_PATH"
echo "  ✓ ${PKG_PATH}"

echo ""
echo "============================================="
echo "  ビルド完了!"
echo "  アプリ : ${APP_PATH}"
echo "  PKG    : ${PKG_PATH}"
echo "============================================="
echo ""
echo "動作確認:"
echo "  open ${APP_PATH}"
echo ""
echo "PKG インストールテスト (要 sudo):"
echo "  sudo installer -pkg ${PKG_PATH} -target /"
