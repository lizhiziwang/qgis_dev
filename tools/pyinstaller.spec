# -*- mode: python ; coding: utf-8 -*-
"""统一 PyInstaller spec。

- 入口 ``app/main.py``（不再用 root ``main.py``）
- QGIS 路径从 ``config/app.yaml`` 读，不在 spec 写死
- 单一 spec 给 Windows + Linux 共用；DLL 路径根据平台不同

用法::

    pyinstaller tools/pyinstaller.spec
"""

import json
import os
import re
import sys
from pathlib import Path

# PyInstaller 执行 spec 时命名空间里没有 __file__，改用 SPECPATH（spec 所在目录 tools/）
SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# install_path 值支持 bash 风格 ${VAR} / ${VAR:-默认} 环境变量引用（语义与
# app/env/config.py 的 _expand_env 一致，保证 Linux/Windows 共用同一份 yaml）；
# 直接传给 Path() 会把字面量当相对路径，导致 PyInstaller 找不到 DLL。
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(s: str) -> str:
    def _repl(m: "re.Match") -> str:
        var, default = m.group(1), m.group(2)
        v = os.environ.get(var)
        if v:
            return v
        return default if default is not None else ""

    return _ENV_REF.sub(_repl, s)


# 读 yaml 配置
try:
    import yaml
    cfg = yaml.safe_load((PROJECT_ROOT / "config" / "app.yaml").read_text(encoding="utf-8"))
except Exception:
    cfg = {}

IS_WIN = sys.platform == "win32"

if IS_WIN:
    # ------------------------------------------------------------------ Windows
    # OSGeo4W 布局：config/app.yaml 的 qgis.install_path 指向安装根（D:/zsh/app/qgis4.2）
    QGIS_INSTALL = _expand_env(cfg.get("qgis", {}).get("install_path", "D:/zsh/app/qgis4.2"))
    QGIS_PYTHON = Path(QGIS_INSTALL) / "apps" / "qgis" / "python"
    QGIS_BIN = Path(QGIS_INSTALL) / "apps" / "qgis" / "bin"
    MAIN_BIN = Path(QGIS_INSTALL) / "bin"
    QT6_BIN = Path(QGIS_INSTALL) / "apps" / "qt6" / "bin"

    datas = [
        (str(QGIS_PYTHON / "qgis"), "qgis"),
        # SAM sidecar 由外部 Python 直接执行（不进 PYZ），必须作为数据文件落盘
        (str(PROJECT_ROOT / "app" / "core" / "ai" / "sam_sidecar.py"), "app/core/ai"),
        # 训练 sidecar + trainlib 同样由外部 Python 执行，必须整目录落盘
        (str(PROJECT_ROOT / "app" / "core" / "training" / "train_sidecar.py"), "app/core/training"),
        (str(PROJECT_ROOT / "app" / "core" / "training" / "trainlib"), "app/core/training/trainlib"),
        # 推理 sidecar + inferlib（复用训练 trainlib）
        (str(PROJECT_ROOT / "app" / "core" / "inference" / "infer_sidecar.py"), "app/core/inference"),
        (str(PROJECT_ROOT / "app" / "core" / "inference" / "inferlib"), "app/core/inference/inferlib"),
        # 下面这些"可整体替换/用户可见"的东西**不**放 datas（那会落到 _internal/），
        # 改由 build_windows.ps1 的 STAGE 4 复制，让根目录干净：
        #   config/ resource/                     -> dist/app/（用户可改）
        #   etc/ plugins/ i18n/ providers/         -> dist/app/runtime/
        #   platforms/ resources/srs.db proj.db    -> dist/app/runtime/
    ]

    binaries = [
        (str(QGIS_BIN / "qgis_core.dll"), "."),
        (str(QGIS_BIN / "qgis_gui.dll"), "."),
        (str(QGIS_BIN / "qgis_analysis.dll"), "."),  # processing 插件 / qgis._analysis 需要
        (str(QGIS_BIN / "qgis_native.dll"), "."),    # qgis_core 依赖
        (str(QGIS_BIN / "qgispython.dll"), "."),     # python 集成
        (str(MAIN_BIN / "gdal313.dll"), "."),
        (str(MAIN_BIN / "geos_c.dll"), "."),
        (str(MAIN_BIN / "pdalcpp.dll"), "."),
        (str(MAIN_BIN / "proj_9.dll"), "proj"),
        (str(QT6_BIN / "Qt6Core.dll"), "."),
        (str(QT6_BIN / "Qt6Gui.dll"), "."),
        (str(QT6_BIN / "Qt6Widgets.dll"), "."),
        (str(QT6_BIN / "Qt6Network.dll"), "."),
        (str(QT6_BIN / "Qt6Xml.dll"), "."),
        (str(QT6_BIN / "Qt6Svg.dll"), "."),
        (str(QT6_BIN / "Qt6Concurrent.dll"), "."),
        # Qt 平台插件不放这里（那会落到 _internal/platforms），由 STAGE 4 复制到
        # dist/app/runtime/platforms/，配合 QT_QPA_PLATFORM_PLUGIN_PATH 使用。
    ]
else:
    # ------------------------------------------------------------------ Linux
    # QGIS 由构建环境提供（conda env / apt）。路径不写死：
    # - qgis python 包取当前 Python 能 import 到的 qgis 包目录
    # - 动态库（.so）、Qt 插件、GDAL 数据等由 tools/build_linux.sh 收集进 dist/app/lib 等目录，
    #   不在这里列（Linux 下 dlopen 加载，PyInstaller 静态分析不到，硬列还会因路径不存在而跳过）
    try:
        import qgis  # 构建环境必须有 QGIS（conda activate 后跑）
        _qgis_pkg = Path(qgis.__file__).resolve().parent  # .../site-packages/qgis
    except ImportError:
        raise SystemExit(
            "Linux 打包需要先激活含 QGIS 的 Python 环境（conda activate qgis_build 或系统已装 python3-qgis）"
        )

    datas = [
        (str(_qgis_pkg), "qgis"),
        (str(PROJECT_ROOT / "resource"), "resource"),
        (str(PROJECT_ROOT / "config"), "config"),
        # SAM sidecar 由外部 Python 直接执行（不进 PYZ），必须作为数据文件落盘
        (str(PROJECT_ROOT / "app" / "core" / "ai" / "sam_sidecar.py"), "app/core/ai"),
        # 训练 sidecar + trainlib 同样由外部 Python 执行，必须整目录落盘
        (str(PROJECT_ROOT / "app" / "core" / "training" / "train_sidecar.py"), "app/core/training"),
        (str(PROJECT_ROOT / "app" / "core" / "training" / "trainlib"), "app/core/training/trainlib"),
        # 推理 sidecar + inferlib（复用训练 trainlib）
        (str(PROJECT_ROOT / "app" / "core" / "inference" / "infer_sidecar.py"), "app/core/inference"),
        (str(PROJECT_ROOT / "app" / "core" / "inference" / "inferlib"), "app/core/inference/inferlib"),
    ]
    binaries = []

# 隐藏导入：Win 用 PyQt6（OSGeo4W 版 QGIS 4.2），Linux 用 PyQt5（conda-forge / 麒麟源 QGIS）
_PYQT_PREFIX = "PyQt6" if IS_WIN else "PyQt5"
_PYQT_MODULES = [
    "pylupdate",
    "pyrcc",
    "Qt",
    "QtBluetooth",
    "QtCore",
    "QtDBus",
    "QtDesigner",
    "QtGui",
    "QtHelp",
    "QtMultimedia",
    "QtMultimediaWidgets",
    "QtNetwork",
    "QtOpenGL",
    "QtPositioning",
    "QtPrintSupport",
    "QtQml",
    "QtQuick",
    "QtQuickWidgets",
    "QtRemoteObjects",
    "QtSensors",
    "QtSerialPort",
    "QtSql",
    "QtSvg",
    "QtTest",
    "QtTextToSpeech",
    "QtWebChannel",
    "QtWebSockets",
    "QtWidgets",
    "QtXml",
    "sip",
]
# QtOpenGLWidgets/QtSvgWidgets 仅在 PyQt6 有独立模块（PyQt5 的 QOpenGLWidget 在
# QtWidgets 里，无此绑定模块，conda pyqt 下会报 Hidden import not found）
if IS_WIN:
    _PYQT_MODULES += ["QtOpenGLWidgets", "QtSvgWidgets"]

hiddenimports = [
    "qgis.core",
    "qgis.gui",
    "qgis.utils",
    "qgis.PyQt",
    *(f"{_PYQT_PREFIX}.{m}" for m in _PYQT_MODULES),
    # processing 插件（运行时加载，PyInstaller 分析不到它的 import）需要：
    f"{_PYQT_PREFIX}.uic",  # processing/gui 用 qgis.PyQt.uic 加载 .ui
    "psycopg2",             # processing/algs/gdal/GdalUtils 顶层 import
]

try:
    from PyInstaller.utils.hooks import collect_all, collect_submodules

    # app 包全量打进 PYZ：入口只 import 了 app.bootstrap，但运行时 app.* 大量
    # 动态导入（算法库、工具注册、流程模板等），纯依赖分析易漏——麒麟真机已见
    # 打包后启动报 ``No module named 'app.bootstrap'``。collect_submodules 兜底。
    hiddenimports += collect_submodules("app")

    for pkg in ("setuptools",):
        try:
            tmp = collect_all(pkg)
            datas += tmp[0]
            binaries += tmp[1]
            hiddenimports += tmp[2]
        except Exception:
            pass
except Exception:
    pass

block_cipher = None

a = Analysis(
    [str(PROJECT_ROOT / "scripts" / "entry.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    # 运行期钩子：隔离随包 QGIS/Qt 运行时与宿主机上的同类 DLL
    runtime_hooks=[str(SPEC_DIR / "rthook_dll_dirs.py")] if IS_WIN else [],
    excludes=["processing"],  # 项目根的 processing/ 是 QGIS 临时目录，namespace 包会遮蔽 plugins/processing
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# --------------------------------------------------------------------------- #
# 原生编译保护（可选，见 tools/protect_cython.py）
#
# 分析阶段仍用原始 .py（保证 import 图 / 动态导入收集完整）；这里把已编译成
# 原生扩展的模块从 PYZ 里剔除，改为把 .pyd 作为二进制收进产物目录。
# 运行期 PyInstaller 冻结导入器在 PYZ 找不到模块时回退到磁盘 FileFinder
# （PyInstaller/loader/pyimod02_importers.py 的 PyiFrozenFinder.find_spec），
# 于是加载 .pyd。反编译产物时这些模块既没有 .py 也没有 .pyc，只剩机器码。
# --------------------------------------------------------------------------- #
_MANIFEST_CANDIDATES = [
    os.environ.get("QGIS_PROTECT_MANIFEST", ""),
    str(PROJECT_ROOT / "build" / "protect" / "manifest.json"),
]
native_entries = []
for _cand in _MANIFEST_CANDIDATES:
    if _cand and os.path.isfile(_cand):
        try:
            with open(_cand, encoding="utf-8") as _f:
                native_entries = json.load(_f).get("native", []) or []
            print(f"[protect] manifest: {_cand}")
        except Exception as _exc:  # noqa: BLE001
            print(f"[protect] 读取 manifest 失败，跳过原生保护: {_exc}")
            native_entries = []
        break

if native_entries:
    _native_modules = {e["module"] for e in native_entries}
    _before = len(a.pure)
    a.pure = [item for item in a.pure if item[0] not in _native_modules]
    _removed = _before - len(a.pure)
    _added = 0
    for _e in native_entries:
        _src, _dest = _e.get("path"), _e.get("dest")
        if _src and _dest and os.path.isfile(_src):
            a.binaries.append((_dest, _src, "EXTENSION"))
            _added += 1
    print(f"[protect] 原生编译保护：PYZ 剔除 {_removed} 个模块，收进 {_added} 个原生扩展")
    if _added < len(native_entries):
        print(f"[protect] 警告：manifest 有 {len(native_entries)} 项，但只有 {_added} 个产物存在")
else:
    print("[protect] 未启用原生编译保护（未找到 manifest）")

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# EXE 名：Windows 用 app（app.exe）；Linux 用 qgis_dev。
# Linux 不能叫 app：PyInstaller 6.x 的 PyiFrozenFinder 对包路径做 isfile 检查
# （#8767 防 runpy），而 Python 包名也是 app → 包 __path__ = dist/app/app 恰好是
# 可执行文件 → 子包（app.bootstrap 等）导入被拒 → No module named 'app.bootstrap'。
_EXE_NAME = "app" if IS_WIN else "qgis_dev"

# --------------------------------------------------------------------------- #
# 应用文件图标（Windows）：从 resource/png/app_icon.png 生成多尺寸 .ico 嵌进 exe
#
# 不直接把 png 交给 PyInstaller：它会用 Pillow 盲转（尺寸档位不可控）。这里显式
# 生成 16/24/32/48/64（Pillow 自动跳过大于源图尺寸的档位），并把 ico 放进 build/
# （gitignore），随 png 一起更新。
#
# 生成失败必须**中止构建**，不能回退默认图标：默认图标也是“打包成功”，现场只会
# 看到任务栏 / 资源管理器里还是个 Python 默认图标，很难联想到是构建期 Pillow 缺失。
# 以前这里静默回退，结果就是“exe 图标没生效”。
# --------------------------------------------------------------------------- #
_APP_ICON_PNG = PROJECT_ROOT / "resource" / "png" / "app_icon.png"
_app_icon = None
if IS_WIN:
    if not _APP_ICON_PNG.is_file():
        raise SystemExit(f"[icon] 应用图标不存在：{_APP_ICON_PNG}")
    try:
        from PIL import Image as _Image

        _ico_path = PROJECT_ROOT / "build" / "app_icon.ico"
        _ico_path.parent.mkdir(parents=True, exist_ok=True)
        with _Image.open(_APP_ICON_PNG) as _im:
            # bitmap_format="bmp" 很关键：Pillow 11.3+ 默认把 ICO 各档位存成 PNG
            # 压缩条目，Windows 的 shell 图标渲染（资源管理器）对「纯 PNG、无 BMP
            # 兜底」的 ICO 可能不显示（虽然 ExtractIconEx 能读），表现为“exe 还是
            # 默认图标”。强制 DIB/BMP 条目兼容性最好。
            _im.convert("RGBA").save(
                _ico_path,
                format="ICO",
                bitmap_format="bmp",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
        with _Image.open(_ico_path) as _ico:
            _sizes = sorted(_ico.info.get("sizes", []))
        _app_icon = str(_ico_path)
        print(f"[icon] exe 图标 {_APP_ICON_PNG.name} -> {_ico_path.name} 尺寸 {_sizes}")
    except Exception as _iph:
        raise SystemExit(
            f"[icon] 从 {_APP_ICON_PNG} 生成 .ico 失败：{_iph}\n"
            "       构建已中止——继续打包只会得到 PyInstaller 默认图标的 exe。\n"
            "       请确认构建环境已安装 Pillow（pip install pillow）。"
        ) from _iph


exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=(os.environ.get("QGIS_BUILD_CONSOLE") == "1"),
    icon=_app_icon,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # PyInstaller 6 默认把 collected 文件放进 `_internal/`。Windows 保留这个默认，
    # 根目录只留 app.exe / start_runtime.bat / config / resource / logs / runtime；
    # Linux 构建脚本已按 lib/ share/qgis/ 等自建布局，改动风险大，继续用扁平。
    contents_directory=("_internal" if IS_WIN else "."),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="app",
)
