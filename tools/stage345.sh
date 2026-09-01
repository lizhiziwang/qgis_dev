#!/usr/bin/env bash
# 补跑 STAGE 3/4/5（PyInstaller STAGE 2 已手动完成时用）：
#   - STAGE 3: 收集 QGIS 运行时（C++ 插件 / python 插件 / 数据 / Qt 插件）
#   - STAGE 4: 递归收集动态库到 dist/app/lib（find 递归，覆盖 plugins 子目录）
#   - STAGE 5: 复制启动器
# 用法: bash tools/stage345.sh   （在项目根运行，无需激活 conda，内部用环境 python）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_PATH="$PROJECT_ROOT/dist/app"
APP_NAME="app"

# conda 的 qgis 是 share 布局（<env>/share/qgis/python），不在 site-packages，
# 必须靠 activate 的 activate.d 脚本设 PYTHONPATH 才能 import。SSH 非交互 shell
# 无 conda 初始化，这里显式 source。activate.d 脚本在 set -u 下会因未绑定变量
# （如 PDAL_DRIVER_PATH）报错，故激活期间临时关闭 nounset。
set +u
source /home/zsh/miniconda3/bin/activate
conda activate qgis_new
set -u
# 确认激活后 qgis 可用
python3 -c "import qgis" 2>/dev/null || { echo "ERROR: 激活 qgis_new 后仍无法 import qgis"; exit 1; }

MACHINE="$(uname -m)"
case "$MACHINE" in
  x86_64)  SYS_LIB="/usr/lib/x86_64-linux-gnu" ;;
  aarch64) SYS_LIB="/usr/lib/aarch64-linux-gnu" ;;
  *) echo "ERROR: 不支持的架构 $MACHINE"; exit 1 ;;
esac

# ---------------------------------------------------------------- 探测 QGIS
QGIS_PKG_DIR=$(python3 - <<'PY'
import qgis, os
print(os.path.dirname(os.path.abspath(qgis.__file__)))
PY
)
QGIS_PREFIX=$(python3 - <<'PY'
import qgis, os
pkg = os.path.dirname(os.path.abspath(qgis.__file__))
cur = pkg
while True:
    parent = os.path.dirname(cur)
    if parent == cur:
        break
    if os.path.isdir(os.path.join(parent, "conda-meta")):
        print(parent)
        break
    cur = parent
else:
    print("/usr")
PY
)
echo "QGIS python 包 : $QGIS_PKG_DIR"
echo "QGIS 前缀      : $QGIS_PREFIX"

# ---------------------------------------------------------------- STAGE 3
echo ""
echo "STAGE 3: 收集 QGIS 运行时到 $DIST_PATH"

mkdir -p \
    "$DIST_PATH/lib/qgis/plugins" \
    "$DIST_PATH/plugins" \
    "$DIST_PATH/share/qgis/python/plugins" \
    "$DIST_PATH/resources" \
    "$DIST_PATH/platforms" \
    "$DIST_PATH/imageformats" \
    "$DIST_PATH/gdal_data" \
    "$DIST_PATH/gdalplugins"

# 1) QGIS python 包兜底
if [ ! -d "$DIST_PATH/qgis" ]; then
    cp -r "$QGIS_PKG_DIR" "$DIST_PATH/qgis"
fi

# 2) QGIS C++ 插件
if [ -d "$QGIS_PREFIX/lib/qgis/plugins" ]; then
    cp -r "$QGIS_PREFIX/lib/qgis/plugins/." "$DIST_PATH/lib/qgis/plugins/"
else
    echo "WARNING: 未找到 QGIS C++ 插件目录 $QGIS_PREFIX/lib/qgis/plugins"
fi

# 3) python 插件
for src in "$QGIS_PREFIX/share/qgis/python/plugins" "$QGIS_PKG_DIR/plugins"; do
    [ -d "$src" ] && cp -r "$src/." "$DIST_PATH/plugins/" || true
done
[ -d "$QGIS_PREFIX/share/qgis/python/plugins" ] && \
    cp -r "$QGIS_PREFIX/share/qgis/python/plugins/." "$DIST_PATH/share/qgis/python/plugins/" || true

# 4) QGIS 数据
[ -d "$QGIS_PREFIX/share/qgis/resources" ] && cp -r "$QGIS_PREFIX/share/qgis/resources/." "$DIST_PATH/share/qgis/resources/"
[ -d "$QGIS_PREFIX/share/qgis/i18n" ] && {
    cp -r "$QGIS_PREFIX/share/qgis/i18n/." "$DIST_PATH/share/qgis/i18n/"
    cp -r "$QGIS_PREFIX/share/qgis/i18n/." "$DIST_PATH/i18n/" 2>/dev/null || true
}
SRS_DB=$(find "$QGIS_PREFIX/share/qgis" -name srs.db 2>/dev/null | head -1 || true)
[ -n "$SRS_DB" ] && cp "$SRS_DB" "$DIST_PATH/resources/srs.db" || true

# 5) GDAL 数据与驱动
[ -d "$QGIS_PREFIX/share/gdal" ] && cp -r "$QGIS_PREFIX/share/gdal/." "$DIST_PATH/gdal_data/" || true
[ -d "$QGIS_PREFIX/lib/gdal" ] && cp -r "$QGIS_PREFIX/lib/gdal/." "$DIST_PATH/gdalplugins/" || true

# 6) proj.db
PROJ_DB=$(find "$QGIS_PREFIX/share" -name proj.db 2>/dev/null | head -1 || true)
if [ -n "$PROJ_DB" ]; then
    cp "$PROJ_DB" "$DIST_PATH/proj.db"
else
    echo "WARNING: 未找到 proj.db"
fi

# 7) Qt 平台 / 图像插件
for qt_plugins in \
    "$QGIS_PREFIX/plugins" \
    "$SYS_LIB/qt6/plugins" \
    "$SYS_LIB/qt5/plugins" \
    /usr/lib/qt6/plugins \
    /usr/lib/qt5/plugins; do
    [ -d "$qt_plugins/platforms" ] && cp -r "$qt_plugins/platforms/." "$DIST_PATH/platforms/" || true
    [ -d "$qt_plugins/imageformats" ] && cp -r "$qt_plugins/imageformats/." "$DIST_PATH/imageformats/" || true
    [ -d "$qt_plugins/xcbglintegrations" ] && cp -r "$qt_plugins/xcbglintegrations/." "$DIST_PATH/xcbglintegrations/" || true
    [ -d "$qt_plugins/tls" ] && cp -r "$qt_plugins/tls/." "$DIST_PATH/tls/" || true
done
if [ ! -f "$DIST_PATH/platforms/libqxcb.so" ] && [ ! -f "$DIST_PATH/platforms/qxcb.so" ]; then
    echo "WARNING: 未找到 Qt xcb 平台插件，GUI 可能无法启动"
fi

# ---------------------------------------------------------------- STAGE 4
echo ""
echo "STAGE 4: 收集动态库到 $DIST_PATH/lib"
LIB_DIR="$DIST_PATH/lib"

collect_libs() {
    local deps
    deps=$(ldd "$@" 2>/dev/null | grep -oE '/[^ ]+\.so[^ ]*' | sort -u) || true
    for so in $deps; do
        case "$so" in
            */ld-linux*|*/libc.so*|*/libm.so*|*/libpthread*|*/libdl.so*|*/librt.so*|*/libutil.so*|*/libstdc++*|*/libgcc_s*|*/libGL*|*/libEGL*|*/libGLX*|*/libOpenGL*|*/libX11*|*/libXext*|*/libXrandr*|*/libxcb*|*/libXau*|*/libXdmcp*|*/libfontconfig*|*/libfreetype*|*/libz.so*|*/libexpat*|*/libuuid*|*/libbsd*|*/libdrm*)
                ;;
            *)
                cp -aLn "$so" "$LIB_DIR/" 2>/dev/null || true
                ;;
        esac
    done
}

ALL_SO=$(find "$DIST_PATH"/qgis "$DIST_PATH"/lib \
    "$DIST_PATH"/gdalplugins "$DIST_PATH"/platforms \
    "$DIST_PATH"/imageformats "$DIST_PATH"/tls 2>/dev/null \
    -name '*.so*' ! -type d || true)

for _round in 1 2; do
    collect_libs $ALL_SO 2>/dev/null
done

find "$LIB_DIR" -maxdepth 1 -type l -delete 2>/dev/null || true

echo "收集到 $(find "$LIB_DIR" -maxdepth 1 -type f | wc -l) 个动态库，共 $(du -sh "$LIB_DIR" | cut -f1)"

# ---------------------------------------------------------------- STAGE 5
echo ""
echo "STAGE 5: 复制启动器"
cp "$SCRIPT_DIR/runtime/launch_qgis.sh" "$DIST_PATH/launch_qgis.sh"
chmod +x "$DIST_PATH/launch_qgis.sh" "$DIST_PATH/qgis_dev" 2>/dev/null || true

echo ""
echo "=============================================="
echo "  S T A G E  3 / 4 / 5   C O M P L E T E !"
echo "=============================================="
echo "便携包: $DIST_PATH"
