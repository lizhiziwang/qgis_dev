#!/usr/bin/env bash
# 把便携包 dist/app 打成银河麒麟 V10 / Ubuntu 可安装的 .deb 桌面软件包。
#
# 用法（项目根目录，在打包机或目标机任一机器上均可）：
#   bash tools/kylin/build_deb.sh              # 版本取 pyproject.toml，架构自动探测
#   bash tools/kylin/build_deb.sh 1.0.0 amd64  # 指定版本 / 架构（aarch64 目标用 arm64）
#
# 产物：dist/qgis-dev-app_<版本>_<架构>.deb
#
# 目标机安装：sudo dpkg -i qgis-dev-app_*.deb
# 目标机卸载：sudo dpkg -r qgis-dev-app
#
# 说明：整个便携包（含 QGIS/Qt/GDAL 运行时）装到 /opt/qgis_app，
#       .deb 只依赖目标机基础库（glibc / libstdc++ / X11 / GL，麒麟桌面自带）。

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

# 版本号：优先参数，其次 pyproject.toml
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    VERSION=$(grep -m1 '^version' "$PROJECT_ROOT/pyproject.toml" | sed -E 's/.*= *"([^"]+)".*/\1/')
fi
# 架构：优先参数，其次本机（交叉打包时用参数指定）
ARCH="${2:-}"
if [ -z "$ARCH" ]; then
    case "$(uname -m)" in
        x86_64)  ARCH="amd64" ;;
        aarch64) ARCH="arm64" ;;
        *) echo "ERROR: 无法识别架构 $(uname -m)，请用参数指定（amd64 / arm64）" >&2; exit 1 ;;
    esac
fi

ICON_SRC="$PROJECT_ROOT/resource/png/app_icon.png"
if [ ! -f "$ICON_SRC" ]; then
    echo "WARNING: 未找到 $ICON_SRC，用 QGIS 图标代替（也可手动放图标到 tools/kylin/）"
    ICON_SRC="/usr/share/icons/hicolor/512x512/apps/qgis.png"
fi

ROOT="$PROJECT_ROOT/dist/debroot"
DEB_FILE="$PROJECT_ROOT/dist/${PKG_NAME}_${VERSION}_${ARCH}.deb"

echo "== 打包参数 =="
echo "  版本   : $VERSION"
echo "  架构   : $ARCH"
echo "  便携包 : $PORTABLE"
echo "  输出   : $DEB_FILE"

# ---------------------------------------------------------------- 组装目录
rm -rf "$ROOT"
mkdir -p \
    "$ROOT/DEBIAN" \
    "$ROOT/${APP_DIR}" \
    "$ROOT/usr/share/applications" \
    "$ROOT/usr/share/icons/hicolor/512x512/apps" \
    "$ROOT/usr/share/icons/hicolor/256x256/apps"

echo "== 复制便携包 → $ROOT${APP_DIR}"
cp -a "$PORTABLE/." "$ROOT${APP_DIR}/"

# 应用可写目录（logs / .tmp）预创建并放开权限，普通用户也能跑。
# 先清掉便携包调试/测试残留（旧日志为 root:root，会导致普通用户 PermissionError）
rm -rf "$ROOT${APP_DIR}/logs" "$ROOT${APP_DIR}/.tmp"
mkdir -p "$ROOT${APP_DIR}/logs" "$ROOT${APP_DIR}/.tmp"
chmod 777 "$ROOT${APP_DIR}/logs" "$ROOT${APP_DIR}/.tmp"

# 桌面入口 + 图标
cp "$SCRIPT_DIR/qgis_dev.desktop" "$ROOT/usr/share/applications/${PKG_NAME}.desktop"
cp "$ICON_SRC" "$ROOT/usr/share/icons/hicolor/512x512/apps/${PKG_NAME}.png"
cp "$ICON_SRC" "$ROOT/usr/share/icons/hicolor/256x256/apps/${PKG_NAME}.png"

# ---------------------------------------------------------------- DEBIAN/control
cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG_NAME
Version: $VERSION
Section: science
Priority: optional
Architecture: $ARCH
Maintainer: zsh <zsh@example.com>
Installed-Size: $(du -sk "$ROOT${APP_DIR}" | cut -f1)
Depends: libc6 (>= 2.31), libstdc++6 (>= 9), libx11-6, libxcb1, libgl1, libegl1, libfontconfig1, libfreetype6
Recommends: fonts-noto-cjk, fonts-wqy-zenhei
Description: 图斑核实平台（PyQGIS 桌面 GIS 应用）
 基于 PyQGIS 的桌面 GIS 应用，含图斑质检、绘制、查询、
 流程编排等功能。自包含 QGIS/Qt/GDAL 运行时，免装 QGIS。
EOF

# 安装后：注册桌面入口、刷新图标缓存
cat > "$ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
chmod 755 /opt/qgis_app/launch_qgis.sh
chmod 755 /opt/qgis_app/qgis_dev
# 日志/临时目录放开写权限（含旧文件），普通用户可写
chmod -R a+rwX /opt/qgis_app/logs /opt/qgis_app/.tmp 2>/dev/null || true
update-desktop-database /usr/share/applications 2>/dev/null || true
gtk-update-icon-cache /usr/share/icons/hicolor 2>/dev/null || true
exit 0
EOF
chmod 755 "$ROOT/DEBIAN/postinst"

# 卸载后：清理图标缓存
cat > "$ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
update-desktop-database /usr/share/applications 2>/dev/null || true
gtk-update-icon-cache /usr/share/icons/hicolor 2>/dev/null || true
exit 0
EOF
chmod 755 "$ROOT/DEBIAN/postrm"

# ---------------------------------------------------------------- 打 .deb
echo "== dpkg-deb --build =="
dpkg-deb --build --root-owner-group "$ROOT" "$DEB_FILE"
rm -rf "$ROOT"

echo ""
echo "=============================================="
echo "  D E B   C O M P L E T E !"
echo "=============================================="
echo "  $DEB_FILE"
echo ""
echo "目标麒麟 V10 机器上："
echo "  sudo bash tools/kylin/install_deb.sh $(basename "$DEB_FILE")"
echo "  注意：麒麟 V10 dpkg 会拒绝无签名的 deb（verification failed），"
echo "  install_deb.sh 会自动关闭 /etc/dpkg/udeblist.xml 的黑白名单校验。"
echo "  桌面菜单/开始菜单搜索『图斑核实平台』启动"
