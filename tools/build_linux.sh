#!/usr/bin/env bash
# 统一 Linux 打包脚本（Ubuntu 20.04 / 22.04、银河麒麟 V10 等 Debian 系）
#
# 用法（项目根目录，先激活含 QGIS 的 Python 环境）：
#   conda activate qgis_build          # 或系统已装 python3-qgis（PyQt 兼容改造后可用）
#   bash tools/build_linux.sh
#
# 产物 dist/app/ 是绿色便携包（目标机免装 QGIS），目录结构与 Windows 对齐：
#   qgis_dev                  可执行文件（Linux 不能叫 app，见 pyinstaller.spec）
#   launch_qgis.sh            启动器（约等于 Windows 的 start_runtime.bat）
#   config/ resource/         应用配置与资源（明文，用户可直接改）
#   runtime/                  QGIS 运行时（conda 风格迷你前缀）
#     share/qgis/             resources / i18n / svg / images / doc / python/plugins
#     share/proj/             proj.db 等 PROJ 数据
#     share/gdal/             GDAL 数据
#     lib/qgis/plugins/       QGIS C++ 插件（providers / geometry 等）
#     lib/gdalplugins/        GDAL 驱动插件
#     lib/qt6/plugins/        Qt 平台 / 图像 / TLS 插件（Qt5 构建为 lib/qt5）
#     bin/                    GDAL 命令行工具（processing 通过 PATH 调用）
#   _internal/                PyInstaller 运行时 + 依赖库 + 业务原生模块
#   build_info.json           构建指纹
#   logs/ .tmp/               运行期生成（deb 安装包会预创建）
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

# QGIS_SKIP_PYINSTALLER=1：复用已有 dist/app，只重跑 STAGE 3-7（运行时装配 /
# 布局自检）。tools/stage345.sh 是它的薄封装，避免两套目录逻辑各自漂移。
SKIP_PYINSTALLER="${QGIS_SKIP_PYINSTALLER:-0}"

# ---------------------------------------------------------------- 探测 QGIS
# 若当前 python 无法 import qgis，自动尝试激活 conda（QGIS_BUILD_ENV / qgis423 /
# qgis_new / qgis_build），避免依赖用户手动 conda activate（SSH 非交互 shell 无
# conda 初始化）。qgis423 = QGIS 4.2.3 + PyQt6，qgis_build = QGIS 3.44 + PyQt5，
# 两者产物互不兼容，优先用显式指定的环境。
if ! python3 -c "import qgis" >/dev/null 2>&1; then
    set +u
    _CONDA_SH=""
    for _cand in "${CONDA_SH:-}" /home/zsh/miniconda3/etc/profile.d/conda.sh \
                 "$HOME/miniconda3/etc/profile.d/conda.sh" \
                 "$HOME/anaconda3/etc/profile.d/conda.sh" /opt/conda/etc/profile.d/conda.sh; do
        if [ -n "$_cand" ] && [ -f "$_cand" ]; then
            _CONDA_SH="$_cand"
            break
        fi
    done
    if [ -n "$_CONDA_SH" ]; then
        source "$_CONDA_SH"
        for env in ${QGIS_BUILD_ENV:-} qgis423 qgis_new qgis_build; do
            if conda env list 2>/dev/null | grep -q "/$env$"; then
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
    echo "         conda activate qgis423        # QGIS 4.2 + Python 3.12 + PyQt6"
    echo "         conda activate qgis_build     # QGIS 3.44 + Python 3.10 + PyQt5"
    echo "       若尚未建环境（二选一）："
    echo "         conda create -n qgis423  -c conda-forge python=3.12 qgis=4.2.3 pyqt6 pyinstaller gdal fastapi uvicorn pydantic pyyaml psycopg2 cython setuptools wheel pip xlwt xlrd openpyxl -y"
    echo "         conda create -n qgis_build -c conda-forge python=3.10 qgis=3.44 pyqt pyinstaller gdal fastapi uvicorn pydantic pyyaml psycopg2 cython setuptools wheel pip xlwt xlrd openpyxl -y"
    echo "       注意：pysqlite3 需另装（conda-forge 没有 py312 构建）："
    echo "         pip install pysqlite3-binary"
    exit 1
fi
if ! python3 -c "import PyInstaller" >/dev/null 2>&1; then
    echo "ERROR: 当前环境缺少 PyInstaller。"
    echo "       conda install -n $(basename "$(python3 -c 'import sys; print(sys.prefix)')") -c conda-forge pyinstaller"
    exit 1
fi
# local_db_util 顶层 import pysqlite3；构建环境缺它会打出一个启动即崩的产物。
if ! python3 -c "import pysqlite3" >/dev/null 2>&1; then
    echo "ERROR: 当前环境缺少 pysqlite3（local_db_util 的运行期依赖）。"
    echo "       python3 -m pip install pysqlite3-binary"
    exit 1
fi
# spec 的 hiddenimports 里声明了这三个 Excel 库；缺了 PyInstaller 只告警，
# 产物运行到本地字典/字段结构导入导出时才 ImportError。
for _mod in xlwt xlrd openpyxl; do
    if ! python3 -c "import $_mod" >/dev/null 2>&1; then
        echo "ERROR: 当前环境缺少 $_mod（pyinstaller.spec 的 hiddenimports）。"
        echo "       python3 -m pip install $_mod"
        exit 1
    fi
done
echo "QGIS python 包 : $QGIS_PKG_DIR"
echo "QGIS 前缀      : $QGIS_PREFIX"

# ---------------------------------------------------------------- STAGE 1: 清理
if [ "$SKIP_PYINSTALLER" = "1" ]; then
    echo ""
    echo "STAGE 1-2: 跳过构建（QGIS_SKIP_PYINSTALLER=1），复用现有 $DIST_PATH"
else
echo ""
echo "STAGE 1: 清理旧产物"
rm -rf "$PROJECT_ROOT/dist" "$PROJECT_ROOT/build"

# ---------------------------------------------------------------- STAGE 1.5: 版本戳
# 必须早于 Cython / PyInstaller：app/_build.py 要随 app 包进产物。
# 工作区不干净时默认拒绝；本地调试可设 QGIS_ALLOW_DIRTY=1。
echo ""
echo "STAGE 1.5: 版本戳"
STAMP_ARGS=(--project-root "$PROJECT_ROOT")
if [ "${QGIS_ALLOW_DIRTY:-0}" = "1" ]; then
    STAMP_ARGS+=(--allow-dirty)
fi
if [ -n "${QGIS_CHANNEL:-}" ]; then
    STAMP_ARGS+=(--channel "$QGIS_CHANNEL")
fi
# 源码树不是 git 仓库（rsync / SFTP 同步到构建机）时 stamp_source 取不到 commit，
# buildId 会退化成 "1.0.0+?"——所有构建都一样，更新器靠 buildId 判断"新版本
# 是否真的启动了"就会误判。允许构建方传入内容哈希等唯一值。
if [ -n "${QGIS_BUILD_ID:-}" ]; then
    STAMP_ARGS+=(--build-id "$QGIS_BUILD_ID")
fi
python3 "$SCRIPT_DIR/stamp_source.py" "${STAMP_ARGS[@]}"

# ---------------------------------------------------------------- STAGE 1.6: 编译保护
# 与 Windows 一致：把 Qt-free 纯 Python 模块编成原生 .so，产物里不留字节码。
# QGIS_PROTECT=0 可关闭；失败（缺 Cython / Python 开发文件）降级为警告，不阻断构建。
PROTECT_DIR="$PROJECT_ROOT/build/protect"
PROTECT_MANIFEST="$PROTECT_DIR/manifest.json"
if [ "${QGIS_PROTECT:-1}" = "1" ] && [ -f "$SCRIPT_DIR/protect_cython.py" ]; then
    echo ""
    echo "STAGE 1.6: Cython 原生编译保护"
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
fi

if [ ! -d "$DIST_PATH" ]; then
    echo "ERROR: 未找到 $DIST_PATH（PyInstaller 失败，或跳过构建时产物不存在）"
    exit 1
fi
# 扩展模块若没被收集，产物启动即 ModuleNotFoundError；在打包机上先拦住。
if [ ! -d "$DIST_PATH/_internal/pysqlite3" ]; then
    echo "ERROR: 产物缺少 pysqlite3（PyInstaller 未收集扩展模块）"
    exit 1
fi

# ---------------------------------------------------------------- STAGE 3: QGIS 运行时
echo ""
echo "STAGE 3: 收集 QGIS 运行时到 $DIST_PATH/runtime"

RUNTIME="$DIST_PATH/runtime"
INTERNAL="$DIST_PATH/_internal"

mkdir -p \
    "$RUNTIME/share/qgis" \
    "$RUNTIME/share/proj" \
    "$RUNTIME/share/gdal" \
    "$RUNTIME/lib/qgis" \
    "$RUNTIME/lib/gdalplugins" \
    "$RUNTIME/bin"

# 1) QGIS python 包（spec 已收进 _internal，这里兜底）
if [ ! -d "$INTERNAL/qgis" ]; then
    cp -r "$QGIS_PKG_DIR" "$INTERNAL/qgis"
fi

# 2) QGIS C++ 插件 + libexec（providers / geometry / crssync 等，运行时 dlopen）
if [ -d "$QGIS_PREFIX/lib/qgis" ]; then
    cp -a "$QGIS_PREFIX/lib/qgis/." "$RUNTIME/lib/qgis/"
else
    echo "WARNING: 未找到 QGIS C++ 插件目录 $QGIS_PREFIX/lib/qgis"
fi

# 3) QGIS 数据：整棵 share/qgis（resources / i18n / svg / images / doc /
#    python/plugins）。排除 python/qgis —— python 绑定已由 PyInstaller 收进
#    _internal/qgis，再放一份会与冻结包重复、还可能遮蔽真正的绑定。
#    QGIS 在 Linux 上按 QGIS_PREFIX_PATH 拼 pkgDataPath=<prefix>/share/qgis，
#    并且**不校验目录是否存在**，所以这里必须一次到位。
if [ -d "$QGIS_PREFIX/share/qgis" ]; then
    tar -C "$QGIS_PREFIX/share/qgis" \
        --exclude='python/qgis' --exclude='python/qgis-*.dist-info' \
        --exclude='__pycache__' -cf - . | tar -C "$RUNTIME/share/qgis" -xf -
else
    echo "WARNING: 未找到 QGIS 数据目录 $QGIS_PREFIX/share/qgis"
fi
# processing 等 python 插件必须能被 import：for_system 会把
# runtime/share/qgis/python 与 runtime/share/qgis/python/plugins 加入 sys.path。
if [ ! -d "$RUNTIME/share/qgis/python/plugins/processing" ]; then
    echo "WARNING: 未找到 processing 插件，Processing.initialize() 将不可用"
fi

# 4) PROJ / GDAL 数据：整个 share/proj、share/gdal（PROJ 除 proj.db 还有
#    proj.ini / 网格定义；GDAL 有 gmlasconf.xml / grib2 表等）
if [ -d "$QGIS_PREFIX/share/proj" ]; then
    cp -a "$QGIS_PREFIX/share/proj/." "$RUNTIME/share/proj/"
else
    echo "WARNING: 未找到 PROJ 数据目录 $QGIS_PREFIX/share/proj，坐标转换将不可用"
fi
[ -d "$QGIS_PREFIX/share/gdal" ] && cp -a "$QGIS_PREFIX/share/gdal/." "$RUNTIME/share/gdal/" || true

# 5) GDAL 驱动插件
# conda-forge 的 GDAL 驱动插件在 lib/gdalplugins（不是 lib/gdal）；麒麟 apt 的
# python3-qgis 则在 lib/gdal。两处都试，缺驱动插件只是部分格式不可用，不阻断。
for _gdal_plugins in "$QGIS_PREFIX/lib/gdalplugins" "$QGIS_PREFIX/lib/gdal"; do
    [ -d "$_gdal_plugins" ] && cp -a "$_gdal_plugins/." "$RUNTIME/lib/gdalplugins/" || true
done

# 5.1) SpatiaLite 扩展：SQLite 的 load_extension('mod_spatialite') 是运行期
# dlopen，PyInstaller 分析不到；缺了它 SpatiaLite 容器/离线下载不可用。
# 放到 _internal/（PyInstaller 自己维护的库搜索目录），launcher 也会加进
# LD_LIBRARY_PATH。
for _so in "$QGIS_PREFIX"/lib/mod_spatialite.so*; do
    [ -e "$_so" ] && cp -aL "$_so" "$INTERNAL/" || true
done

# 5.2) GDAL 命令行工具：processing 的 gdal provider 通过 PATH 调
# gdal_translate / gdalwarp / gdalbuildvrt / gdal_rasterize / gdaldem 等
# （GdalUtils.runGdal）。不拷的话干净目标机上这些算法报
# "Process gdal_xxx failed to start… missing"。conda 前缀 bin 下这些都是
# ELF 可执行文件；gdal_*.py（gdal_merge.py / gdal_calc.py 等）的 shebang
# 指向构建环境的 conda python，目标机没有 osgeo，拷了也跑不起来，跳过。
mkdir -p "$RUNTIME/bin"
for _tool in gdal_translate gdalwarp gdalbuildvrt gdalinfo gdaladdo \
             gdal_rasterize gdal_create gdaldem gdaltransform \
             ogr2ogr ogrinfo ogrmerge; do
    _src="$QGIS_PREFIX/bin/$_tool"
    if [ -f "$_src" ]; then
        cp -aL "$_src" "$RUNTIME/bin/" || true
    fi
done

# 6) Qt 平台 / 图像插件（GUI 显示必需，libqxcb.so 等）
#    只取**一套** Qt 插件目录：conda Qt6 在 $PREFIX/lib/qt6/plugins，conda Qt5 在
#    $PREFIX/plugins，系统 Qt 在 $SYS_LIB/qt{6,5}/plugins。依次挑选第一个含
#    platforms/ 的目录即停——顺序遍历会把系统 Qt5 插件覆盖进 conda Qt6 包，
#    ABI 不匹配的 libqxcb.so 会让 GUI 直接起不来。
#    目标目录保持 Qt 版本语义（lib/qt6 或 lib/qt5），for_system 按顺序探测。
QT_PLUGIN_ROOT=""
for qt_plugins in \
    "$QGIS_PREFIX/lib/qt6/plugins" \
    "$QGIS_PREFIX/lib/qt5/plugins" \
    "$QGIS_PREFIX/plugins" \
    "$SYS_LIB/qt6/plugins" \
    "$SYS_LIB/qt5/plugins" \
    /usr/lib/qt6/plugins \
    /usr/lib/qt5/plugins; do
    if [ -d "$qt_plugins/platforms" ]; then
        QT_PLUGIN_ROOT="$qt_plugins"
        break
    fi
done
if [ -n "$QT_PLUGIN_ROOT" ]; then
    echo "  Qt 插件目录: $QT_PLUGIN_ROOT"
    case "$QT_PLUGIN_ROOT" in
        */lib/qt6/plugins) RT_QT_PLUGINS="$RUNTIME/lib/qt6/plugins" ;;
        */lib/qt5/plugins) RT_QT_PLUGINS="$RUNTIME/lib/qt5/plugins" ;;
        */plugins)         RT_QT_PLUGINS="$RUNTIME/plugins" ;;
        *)                 RT_QT_PLUGINS="$RUNTIME/lib/qt6/plugins" ;;
    esac
    mkdir -p "$RT_QT_PLUGINS"
    # 整套复制（platforms / imageformats / xcbglintegrations / tls /
    # iconengines / styles / platformthemes ...），只拷 4 类会缺图标/主题插件。
    cp -a "$QT_PLUGIN_ROOT/." "$RT_QT_PLUGINS/"
else
    echo "WARNING: 未找到 Qt 插件目录（platforms/），GUI 可能无法启动"
fi

# ---------------------------------------------------------------- STAGE 3.5: 应用配置
# config/、resource/ 不放 PyInstaller datas（那样会落进 _internal/），改在产物
# 根目录显式复制，与 Windows 的 build_windows.ps1 STAGE 4.2 一致：用户能直接
# 改配置，升级/更新也不会把它们埋进 _internal。
echo ""
echo "STAGE 3.5: 复制 config/ resource/ 到产物根"
mkdir -p "$DIST_PATH/config" "$DIST_PATH/resource"
cp -a "$PROJECT_ROOT/config/." "$DIST_PATH/config/"
cp -a "$PROJECT_ROOT/resource/." "$DIST_PATH/resource/"

# ---------------------------------------------------------------- STAGE 4: 收集动态库
# 把 qgis python 扩展、C++ 插件、Qt 插件依赖的 .so 全量收集到 _internal/，
# 目标机免装 QGIS/Qt。
# 不另建 _internal/lib：PyInstaller 保证 _internal 在动态库搜索路径里，子目录
# 不保证；平铺进 _internal 还能用 cp -n 跳过 PyInstaller 已收集的库，顺便去重。
# 只跳过目标机必有的基础库（glibc / X11 / GL / fontconfig —— 麒麟桌面自带）。
# libstdc++ / libgcc_s 例外：conda 的 Qt 6 / GDAL 是用较新的 GCC 编的，麒麟
# V10 自带的 libstdc++.so.6 缺 GLIBCXX_3.4.30 / CXXABI_1.3.15，直接加载报
# "version `GLIBCXX_3.4.30' not found"；必须随包带 conda 那份。
echo ""
echo "STAGE 4: 收集动态库到 $DIST_PATH/_internal"
LIB_DIR="$DIST_PATH/_internal"
mkdir -p "$LIB_DIR"

collect_libs() {
    # 用法: collect_libs <so 文件...> ；把 ldd 解析出的依赖复制到 LIB_DIR
    #
    # 关键：产物里的 .so 是从 conda 前缀拷来的，RUNPATH 多为 $ORIGIN 相对路径，
    # 拷到 dist/app 后已指向错误目录；不把构建前缀放进 LD_LIBRARY_PATH 的话，
    # ldd 对 libQt6XcbQpa / libspatialite / libpq 等一律 "not found"，grep 只取
    # 绝对路径就把它们全丢了。libxcb 的辅助库（-cursor/-icccm/-keysyms…）也不能
    # 按前缀一刀切跳过：Qt 6 的 libqxcb.so 硬依赖 libxcb-cursor.so.0。
    local deps
    deps=$(LD_LIBRARY_PATH="$QGIS_PREFIX/lib:$LIB_DIR:$DIST_PATH/runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
        ldd "$@" 2>/dev/null | grep -oE '/[^ ]+\.so[^ ]*' | sort -u) || true
    for so in $deps; do
        case "$so" in
            */ld-linux*|*/libc.so*|*/libm.so*|*/libpthread*|*/libdl.so*|*/librt.so*|*/libutil.so*|*/libGL*|*/libEGL*|*/libGLX*|*/libOpenGL*|*/libX11.so*|*/libXext.so*|*/libXrandr.so*|*/libxcb.so*|*/libXau*|*/libXdmcp*|*/libfontconfig*|*/libfreetype*|*/libz.so*|*/libexpat*|*/libuuid*|*/libbsd*|*/libdrm*)
                # 基础系统库：目标机（麒麟桌面）必有，跳过以减小体积
                ;;
            *)
                cp -aLn "$so" "$LIB_DIR/" 2>/dev/null || true
                ;;
        esac
    done
}

# 递归收集两轮：新拷入的 .so 的依赖也要收（Qt → ICU → ...）
# 第一轮必须整棵 _internal 递归扫描，不能只看顶层：PyInstaller 把 qgis /
# PyQt6 / numpy / python3.12/lib-dynload 放在子目录，它们的依赖（libffi、
# ICU、openblas、ncurses…）同样要随包——只扫顶层实测启动即
# ImportError: libffi.so.8。C++ 插件则在 runtime/lib/qgis/plugins/<...>/ 子目录。
# 第二轮只扫 _internal 顶层（第一轮新拷进来的库），避免重复扫描上千个文件。
for _round in 1 2; do
    if [ "$_round" = "1" ]; then
        ALL_SO=$(find "$INTERNAL" "$RUNTIME" -name '*.so*' ! -type d 2>/dev/null || true)
    else
        ALL_SO=$(find "$LIB_DIR" -maxdepth 1 -name '*.so*' ! -type l 2>/dev/null || true)
    fi
    # GDAL 命令行工具不是 .so，但同样要在打包机上用 ldd 解析依赖
    BIN_EXES=$(find "$RUNTIME"/bin -maxdepth 1 -type f 2>/dev/null || true)
    collect_libs $ALL_SO $BIN_EXES 2>/dev/null || true
done

# 注意：不能在这里删除 _internal 里的 soname 软链（如 libffi.so.8 ->
# libffi.so.8.4.0）。删掉后 DT_NEEDED=libffi.so.8 的模块会直接 ImportError；
# 统一由下面的 _deref_links() 把软链替换成**同名普通文件**，既满足更新器
# “包内无软链”，又保留 loader 需要的 soname。

# PyInstaller 会在 _internal/ 里留下 soname 软链（libzstd.so -> libzstd.so.1）。
# 提权更新器的 tar 解压白名单拒绝符号链接（防越界写入），所以更新包里必须全是
# 普通文件；这里统一解引用，deb 与更新包共用同一棵无软链的树。
_deref_links() {
    find "$DIST_PATH" -type l -print0 2>/dev/null | while IFS= read -r -d '' link; do
        target="$(readlink -f -- "$link" 2>/dev/null || true)"
        if [ -n "$target" ] && [ -f "$target" ]; then
            rm -f -- "$link"
            cp -fL -- "$target" "$link"
        else
            echo "WARNING: 删除断链 $link"
            rm -f -- "$link"
        fi
    done
}
_deref_links

echo "收集到 $(find "$LIB_DIR" -maxdepth 1 -name '*.so*' -type f | wc -l) 个动态库，_internal 共 $(du -sh "$LIB_DIR" | cut -f1)"

# ---------------------------------------------------------------- STAGE 5: 启动器
echo ""
echo "STAGE 5: 复制启动器"
cp "$SCRIPT_DIR/runtime/launch_qgis.sh" "$DIST_PATH/launch_qgis.sh"
chmod +x "$DIST_PATH/launch_qgis.sh" "$DIST_PATH/qgis_dev" 2>/dev/null || true

# ---------------------------------------------------------------- STAGE 6: 构建戳
echo ""
echo "STAGE 6: 写 build_info.json"
STAMP_BUILD_ARGS=(--project-root "$PROJECT_ROOT" --dist "$DIST_PATH")
if [ -n "${QGIS_CHANNEL:-}" ]; then
    STAMP_BUILD_ARGS+=(--channel "$QGIS_CHANNEL")
fi
python3 "$SCRIPT_DIR/stamp_build.py" "${STAMP_BUILD_ARGS[@]}"

# ---------------------------------------------------------------- STAGE 7: 布局自检
# QGIS 的路径是纯字符串拼接，缺目录不会报错，只会在运行时静默丢 provider /
# 坐标库 / 翻译。构建机这里硬性拦住关键项，避免“能启动但功能残缺”的包流出。
echo ""
echo "STAGE 7: 便携包布局自检"
_missing=0
_require() {
    if [ ! -e "$DIST_PATH/$1" ]; then
        echo "ERROR: 便携包缺少 $1" >&2
        _missing=1
    fi
}
_require "qgis_dev"
_require "launch_qgis.sh"
_require "build_info.json"
_require "config/app.yaml"
_require "resource"
_require "runtime/share/qgis/resources/srs.db"
_require "runtime/share/qgis/python/plugins/processing"
_require "runtime/share/proj/proj.db"
_require "runtime/lib/qgis/plugins"
_require "_internal/qgis"
_require "_internal/pysqlite3"

_qt_ok=0
for _cand in "$RUNTIME/lib/qt6/plugins" "$RUNTIME/lib/qt5/plugins" "$RUNTIME/plugins"; do
    if [ -f "$_cand/platforms/libqxcb.so" ] || [ -f "$_cand/platforms/qxcb.so" ]; then
        _qt_ok=1
        break
    fi
done
if [ "$_qt_ok" != "1" ]; then
    echo "ERROR: 便携包缺少 Qt xcb 平台插件（runtime/lib/qt{6,5}/plugins/platforms）" >&2
    _missing=1
fi
if [ ! -f "$RUNTIME/bin/gdal_translate" ]; then
    echo "WARNING: 未打包 gdal_translate，processing 的部分 GDAL 算法可能不可用"
fi
# 更新器解压白名单拒绝符号链接，包内必须是纯普通文件
_links=$(find "$DIST_PATH" -type l 2>/dev/null | head -5 || true)
if [ -n "$_links" ]; then
    echo "ERROR: 便携包内仍有符号链接（更新器会拒绝解压）：" >&2
    echo "$_links" | sed 's/^/  /' >&2
    _missing=1
fi

# 7.1) 依赖闭合检查：包内每个 .so 的 DT_NEEDED 都要能解析。
# 只收集根目录/指定目录时，PyQt6、python3.12/lib-dynload、numpy 子目录里的
# libffi / ICU / openblas 会全漏掉，表现是启动即 ImportError: libffi.so.8；
# 系统 libstdc++ 过旧则报 version `GLIBCXX_3.4.30' not found。这里统一拦截。
# 较慢（约 1 分钟）；调试可设 QGIS_SKIP_LDD_CHECK=1 跳过。
if [ "${QGIS_SKIP_LDD_CHECK:-0}" != "1" ]; then
    echo "STAGE 7.1: 动态库依赖自检"
    _ldd_missing=$(find "$LIB_DIR" "$RUNTIME" -type f -name '*.so*' -print0 2>/dev/null \
        | xargs -0 -r -P 8 -n 16 env LC_ALL=C \
            LD_LIBRARY_PATH="$LIB_DIR:$RUNTIME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
            bash -c 'for f in "$@"; do ldd "$f" 2>/dev/null | grep "not found"; done' _ \
        | sed 's/^[[:space:]]*//' | sort -u | head -20) || true
    if [ -n "$_ldd_missing" ]; then
        echo "ERROR: 以下动态库依赖无法解析（目标机会启动失败/功能缺失）：" >&2
        echo "$_ldd_missing" | sed 's/^/  /' >&2
        _missing=1
    fi
fi

if [ "$_missing" != "0" ]; then
    echo "ERROR: 便携包布局不完整，终止构建" >&2
    exit 1
fi
echo "  布局自检通过"

echo ""
echo "=============================================="
echo "  B U I L D   C O M P L E T E !"
echo "=============================================="
echo "便携包: $DIST_PATH"
echo "本机试跑: $DIST_PATH/launch_qgis.sh"
echo "部署到麒麟 V10:"
echo "  bash tools/kylin/install.sh          # 直接装到 /opt"
echo "  bash tools/kylin/build_deb.sh        # 打成 .deb 安装包"
