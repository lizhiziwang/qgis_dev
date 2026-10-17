#!/usr/bin/env bash
# 统一 Linux 打包脚本（Ubuntu 20.04 / 22.04、银河麒麟 V10 等 Debian 系）
#
# 用法（项目根目录，先激活含 QGIS 的 Python 环境）：
#   conda activate qgis_build          # 或系统已装 python3-qgis（PyQt 兼容改造后可用）
#   bash tools/build_linux.sh
#
# 产物 dist/app/ 是绿色便携包（目标机免装 QGIS），结构：
#   app / launch_qgis.sh       可执行文件 + 启动器
#   qgis/                      QGIS python 包
#   lib/                       收集到的动态库（.so，含 Qt / QGIS / GDAL / PROJ）
#   lib/qgis/plugins/          QGIS C++ 插件（providers / geometry 等）
#   plugins/                   处理工具箱等 python 插件
#   share/qgis/                QGIS 数据（resources / i18n / python/plugins）
#   platforms/ imageformats/   Qt 平台 / 图像插件（xcb 等）
#   gdal_data/ gdalplugins/    GDAL 数据与驱动
#   proj.db resources/srs.db   PROJ / QGIS 坐标数据
#   config/ resource/          应用配置与资源
#
# 部署：tools/kylin/build_deb.sh 打 .deb，或 tools/kylin/install.sh 直接安装。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_NAME="app"
DIST_PATH="$PROJECT_ROOT/dist/$APP_NAME"
SPEC_FILE="$PROJECT_ROOT/tools/pyinstaller.spec"

MACHINE="$(uname -m)"
case "$MACHINE" in
  x86_64)  SYS_LIB="/usr/lib/x86_64-linux-gnu" ;;
  aarch64) SYS_LIB="/usr/lib/aarch64-linux-gnu" ;;
  *) echo "ERROR: 不支持的架构 $MACHINE（仅支持 x86_64 / aarch64）"; exit 1 ;;
esac

# ---------------------------------------------------------------- 探测 QGIS
# 若当前 python 无法 import qgis，自动尝试激活 conda（qgis_new / qgis_build），
# 避免依赖用户手动 conda activate（SSH 非交互 shell 无 conda 初始化）。
if ! python3 -c "import qgis" >/dev/null 2>&1; then
    set +u
    if [ -f /home/zsh/miniconda3/etc/profile.d/conda.sh ]; then
        source /home/zsh/miniconda3/etc/profile.d/conda.sh
        for env in qgis_new qgis_build; do
            if conda env list 2>/dev/null | grep -q "/envs/$env$"; then
                echo "自动激活 conda 环境: $env"
                conda activate "$env"
                break
            fi
        done
    fi
    set -u
fi

QGIS_PKG_DIR=$(python3 - <<'PY' 2>/dev/null
import qgis, os
print(os.path.dirname(os.path.abspath(qgis.__file__)))
PY
) || true
QGIS_PREFIX=$(python3 - <<'PY'
import qgis, os
pkg = os.path.dirname(os.path.abspath(qgis.__file__))   # .../qgis
# 向上找 conda 环境根（含 conda-meta 的目录）：兼容两种 conda 布局
#   - share 布局：    <env>/share/qgis/python/qgis
#   - site-packages： <env>/lib/python3.x/site-packages/qgis
cur = pkg
while True:
    parent = os.path.dirname(cur)
    if parent == cur:
        break
    if os.path.isdir(os.path.join(parent, "conda-meta")):
        print(parent)   # conda env 根 = QGIS 前缀
        break
    cur = parent
else:
    print("/usr")      # apt（python3-qgis）布局：/usr/lib/python3/dist-packages/qgis
PY
 2>/dev/null) || true

if [ ! -d "$QGIS_PKG_DIR" ]; then
    echo "ERROR: 未找到 QGIS python 包（python3 -c 'import qgis' 失败）。"
    echo "       请先激活 conda 打包环境："
    echo "         conda activate qgis_build"
    echo "       若尚未建环境："
    echo "         conda create -n qgis_build -c conda-forge python=3.10 qgis pyqt pyinstaller gdal rasterio networkx scipy fastapi uvicorn pydantic pyyaml psycopg2 -y"
    exit 1
fi
if ! python3 -c "import PyInstaller" >/dev/null 2>&1; then
    echo "ERROR: 当前环境缺少 PyInstaller。"
    echo "       conda install -n $(basename "$(python3 -c 'import sys; print(sys.prefix)')") -c conda-forge pyinstaller"
    exit 1
fi
echo "QGIS python 包 : $QGIS_PKG_DIR"
echo "QGIS 前缀      : $QGIS_PREFIX"

# ---------------------------------------------------------------- STAGE 1: 清理
echo ""
echo "STAGE 1: 清理旧产物"
rm -rf "$PROJECT_ROOT/dist" "$PROJECT_ROOT/build"

# ---------------------------------------------------------------- STAGE 1.5: 编译保护
# 与 Windows 一致：把 Qt-free 纯 Python 模块编成原生 .so，产物里不留字节码。
# QGIS_PROTECT=0 可关闭；失败（缺 Cython / Python 开发文件）降级为警告，不阻断构建。
PROTECT_DIR="$PROJECT_ROOT/build/protect"
PROTECT_MANIFEST="$PROTECT_DIR/manifest.json"
if [ "${QGIS_PROTECT:-1}" = "1" ] && [ -f "$SCRIPT_DIR/protect_cython.py" ]; then
    echo ""
    echo "STAGE 1.5: Cython 原生编译保护"
    if python3 "$SCRIPT_DIR/protect_cython.py" \
        --project-root "$PROJECT_ROOT" \
        --config "$PROJECT_ROOT/config/app.yaml" \
        --out "$PROTECT_DIR" \
        --manifest "$PROTECT_MANIFEST" \
        --clean; then
        export QGIS_PROTECT_MANIFEST="$PROTECT_MANIFEST"
        echo "  保护清单: $PROTECT_MANIFEST"
    else
        echo "  WARN: 编译保护失败（缺 Cython 或 Python 开发文件？），本次跳过保护继续构建"
        unset QGIS_PROTECT_MANIFEST
    fi
fi

# ---------------------------------------------------------------- STAGE 2: PyInstaller
echo ""
echo "STAGE 2: PyInstaller（$SPEC_FILE）"
python3 -m PyInstaller --noconfirm "$SPEC_FILE"

if [ ! -d "$DIST_PATH" ]; then
    echo "ERROR: PyInstaller 失败，未找到 $DIST_PATH"
    exit 1
fi

# ---------------------------------------------------------------- STAGE 3: QGIS 运行时
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

# 1) QGIS python 包（spec 已打一份，这里兜底，避免 spec 解析不到时缺失）
if [ ! -d "$DIST_PATH/qgis" ]; then
    cp -r "$QGIS_PKG_DIR" "$DIST_PATH/qgis"
fi

# 2) QGIS C++ 插件（providers / geometry 等，运行时 dlopen，PyInstaller 看不到）
if [ -d "$QGIS_PREFIX/lib/qgis/plugins" ]; then
    cp -r "$QGIS_PREFIX/lib/qgis/plugins/." "$DIST_PATH/lib/qgis/plugins/"
else
    echo "WARNING: 未找到 QGIS C++ 插件目录 $QGIS_PREFIX/lib/qgis/plugins"
fi

# 3) python 插件（processing 等）：打包缺了它 processing.run 是 stub
for src in "$QGIS_PREFIX/share/qgis/python/plugins" "$QGIS_PKG_DIR/plugins"; do
    if [ -d "$src" ]; then
        cp -r "$src/." "$DIST_PATH/plugins/" || true
    fi
done
[ -d "$QGIS_PREFIX/share/qgis/python/plugins" ] && \
    cp -r "$QGIS_PREFIX/share/qgis/python/plugins/." "$DIST_PATH/share/qgis/python/plugins/" || true

# 4) QGIS 数据：resources / i18n（Linux 布局 = <prefix>/share/qgis/...）
if [ -d "$QGIS_PREFIX/share/qgis/resources" ]; then
    cp -r "$QGIS_PREFIX/share/qgis/resources/." "$DIST_PATH/share/qgis/resources/"
fi
if [ -d "$QGIS_PREFIX/share/qgis/i18n" ]; then
    cp -r "$QGIS_PREFIX/share/qgis/i18n/." "$DIST_PATH/share/qgis/i18n/"
    cp -r "$QGIS_PREFIX/share/qgis/i18n/." "$DIST_PATH/i18n/" 2>/dev/null || true
fi
SRS_DB=$(find "$QGIS_PREFIX/share/qgis" -name srs.db 2>/dev/null | head -1 || true)
[ -n "$SRS_DB" ] && cp "$SRS_DB" "$DIST_PATH/resources/srs.db" || true

# 5) GDAL 数据与驱动
[ -d "$QGIS_PREFIX/share/gdal" ] && cp -r "$QGIS_PREFIX/share/gdal/." "$DIST_PATH/gdal_data/" || true
[ -d "$QGIS_PREFIX/lib/gdal" ] && cp -r "$QGIS_PREFIX/lib/gdal/." "$DIST_PATH/gdalplugins/" || true

# 6) proj.db（PROJ 数据）
PROJ_DB=$(find "$QGIS_PREFIX/share" -name proj.db 2>/dev/null | head -1 || true)
if [ -n "$PROJ_DB" ]; then
    cp "$PROJ_DB" "$DIST_PATH/proj.db"
else
    echo "WARNING: 未找到 proj.db，坐标转换将不可用"
fi

# 7) Qt 平台 / 图像插件（GUI 显示必需，libqxcb.so 等）
#    候选目录：conda（$PREFIX/plugins）、Ubuntu 22.04+（qt6）、Ubuntu 20.04（qt5）
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

# ---------------------------------------------------------------- STAGE 4: 收集动态库
# 把 qgis python 扩展、C++ 插件、Qt 插件依赖的 .so 全量收集到 lib/，目标机免装 QGIS/Qt。
# 只跳过目标机必有的基础库（glibc / libstdc++ / X11 / GL / fontconfig —— 麒麟桌面自带）。
echo ""
echo "STAGE 4: 收集动态库到 $DIST_PATH/lib"
LIB_DIR="$DIST_PATH/lib"

collect_libs() {
    # 用法: collect_libs <so 文件...> ；把 ldd 解析出的依赖复制到 LIB_DIR
    local deps
    deps=$(ldd "$@" 2>/dev/null | grep -oE '/[^ ]+\.so[^ ]*' | sort -u) || true
    for so in $deps; do
        case "$so" in
            */ld-linux*|*/libc.so*|*/libm.so*|*/libpthread*|*/libdl.so*|*/librt.so*|*/libutil.so*|*/libstdc++*|*/libgcc_s*|*/libGL*|*/libEGL*|*/libGLX*|*/libOpenGL*|*/libX11*|*/libXext*|*/libXrandr*|*/libxcb*|*/libXau*|*/libXdmcp*|*/libfontconfig*|*/libfreetype*|*/libz.so*|*/libexpat*|*/libuuid*|*/libbsd*|*/libdrm*)
                # 基础系统库：目标机（麒麟桌面）必有，跳过以减小体积
                ;;
            *)
                cp -aLn "$so" "$LIB_DIR/" 2>/dev/null || true
                ;;
        esac
    done
}

# 递归收集两轮：新拷入的 .so 的依赖也要收（Qt → ICU → ...）
# 找出包内所有需要解析依赖的 .so（递归：qgis python 扩展 / C++ 插件 / Qt 插件）
# 注意：C++ 插件的 .so 在 lib/qgis/plugins/<providers|geometry|...>/ 子目录，
#       顶层通配符匹配不到，必须 find 递归。
ALL_SO=$(find "$DIST_PATH"/qgis "$DIST_PATH"/lib \
    "$DIST_PATH"/gdalplugins "$DIST_PATH"/platforms \
    "$DIST_PATH"/imageformats "$DIST_PATH"/tls 2>/dev/null \
    -name '*.so*' ! -type d || true)

# 递归收集两轮：新拷入的 .so 的依赖也要（Qt → ICU → ...）
for _round in 1 2; do
    collect_libs $ALL_SO 2>/dev/null
done

# conda 的 .so 常带版本号软链；cp -aL 已解引用复制真实文件，这里清掉残留软链避免断链
find "$LIB_DIR" -maxdepth 1 -type l -delete 2>/dev/null || true

echo "收集到 $(find "$LIB_DIR" -maxdepth 1 -type f | wc -l) 个动态库，共 $(du -sh "$LIB_DIR" | cut -f1)"

# ---------------------------------------------------------------- STAGE 5: 启动器
echo ""
echo "STAGE 5: 复制启动器"
cp "$SCRIPT_DIR/runtime/launch_qgis.sh" "$DIST_PATH/launch_qgis.sh"
chmod +x "$DIST_PATH/launch_qgis.sh" "$DIST_PATH/qgis_dev" 2>/dev/null || true

echo ""
echo "=============================================="
echo "  B U I L D   C O M P L E T E !"
echo "=============================================="
echo "便携包: $DIST_PATH"
echo "本机试跑: $DIST_PATH/launch_qgis.sh"
echo "部署到麒麟 V10:"
echo "  bash tools/kylin/install.sh          # 直接装到 /opt"
echo "  bash tools/kylin/build_deb.sh        # 打成 .deb 安装包"
