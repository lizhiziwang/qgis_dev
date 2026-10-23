#!/usr/bin/env bash
# 便携包免 .deb 直接部署（内网 / 临时机器）：
#   1. 把 dist/app 整个拷到 /opt/qgis_app
#   2. 注册桌面入口 + 图标
# 用法（项目根目录）：bash tools/kylin/install.sh
# 卸载：sudo rm -rf /opt/qgis_app /usr/share/applications/qgis-dev-app.desktop \
#       /usr/share/icons/hicolor/512x512/apps/qgis-dev-app.png

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PORTABLE="$PROJECT_ROOT/dist/app"
PKG_NAME="qgis-dev-app"
APP_DIR="/opt/qgis_app"

if [ ! -d "$PORTABLE" ] || [ ! -x "$PORTABLE/qgis_dev" ]; then
    echo "ERROR: 未找到便携包 $PORTABLE（先跑 bash tools/build_linux.sh）" >&2
    exit 1
fi

echo "== 复制便携包到 $APP_DIR（约 $(du -sh "$PORTABLE" | cut -f1)）"
sudo rm -rf "$APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo cp -a "$PORTABLE/." "$APP_DIR/"
sudo mkdir -p "$APP_DIR/logs" "$APP_DIR/.tmp"
sudo chmod 777 "$APP_DIR/logs" "$APP_DIR/.tmp"
sudo chmod 755 "$APP_DIR/launch_qgis.sh" "$APP_DIR/qgis_dev"

# 自动更新提权程序：root:root 0755，固定路径（客户端 UpdaterLauncher 只认这个位置）。
echo "== 安装更新器 → /usr/local/lib/qgis_dev/qgis-dev-updater"
sudo install -D -m 0755 "$PROJECT_ROOT/tools/linux/qgis-dev-updater" \
    /usr/local/lib/qgis_dev/qgis-dev-updater

echo "== 注册桌面入口"
sudo cp "$SCRIPT_DIR/qgis_dev.desktop" "/usr/share/applications/${PKG_NAME}.desktop"
sudo mkdir -p /usr/share/icons/hicolor/512x512/apps
ICON_SRC="$PROJECT_ROOT/resource/png/app_icon.png"
[ -f "$ICON_SRC" ] && sudo cp "$ICON_SRC" "/usr/share/icons/hicolor/512x512/apps/${PKG_NAME}.png"

sudo update-desktop-database /usr/share/applications 2>/dev/null || true
sudo gtk-update-icon-cache /usr/share/icons/hicolor 2>/dev/null || true

echo ""
echo "=============================================="
echo "  部署完成！开始菜单搜索『图斑核实平台』启动"
echo "  或命令行：/opt/qgis_app/launch_qgis.sh"
echo "=============================================="
