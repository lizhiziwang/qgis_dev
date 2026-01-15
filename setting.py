import os
import sys


def set_config():
    # ===================== 核心：QGIS 3.40 专属路径配置（务必与你的安装目录一致） =====================
    QGIS_BASE_PATH = r'D:\zsh\app\qgis_3.40'  # QGIS 根目录
    QGIS_QGIS_LTR_PATH = os.path.join(QGIS_BASE_PATH, 'apps', 'qgis-ltr')  # QGIS 核心程序目录
    QGIS_GDAL_PATH = os.path.join(QGIS_BASE_PATH, 'apps', 'gdal')  # QGIS 3.40 GDAL 根目录
    QGIS_PROJ_PATH = os.path.join(QGIS_BASE_PATH, 'apps', 'proj')  # QGIS 3.40 Proj 根目录
    QGIS_BIN_PATH = os.path.join(QGIS_BASE_PATH, 'bin')  # QGIS 可执行文件/DLL 目录
    QGIS_PLUGIN_PATH = os.path.join(QGIS_QGIS_LTR_PATH, 'python', 'plugins')

    # ===================== 适配QGIS 3.40 的环境变量配置（关键：修正GDAL/Proj路径） =====================
    # 1. QGIS 核心前缀路径（必须指向 apps/qgis-ltr）
    os.environ['QGIS_PREFIX_PATH'] = QGIS_QGIS_LTR_PATH

    # 2. Proj 投影库路径（关键：QGIS 3.40 在 apps/proj/share 下）
    os.environ['PROJ_LIB'] = os.path.join(QGIS_PROJ_PATH, 'share')

    # 3. GDAL 核心数据路径（关键：QGIS 3.40 在 apps/gdal/data 下）
    os.environ['GDAL_DATA'] = os.path.join(QGIS_GDAL_PATH, 'data')

    # 4. GDAL 驱动插件路径（关键：QGIS 3.40 在 apps/gdal/gdalplugins 下）
    os.environ['GDAL_DRIVER_PATH'] = os.path.join(QGIS_GDAL_PATH, 'gdalplugins')

    # 5. Qt 插件路径（兼顾 QGIS 核心插件和 Qt 平台插件）
    os.environ['QT_PLUGIN_PATH'] = (
        f"{os.path.join(QGIS_QGIS_LTR_PATH, 'plugins')};"  # QGIS 内置 Qt 插件
        f"{QGIS_BIN_PATH}"  # QGIS 根目录 bin 下的 Qt 平台插件
    )
    sys.path.append(QGIS_QGIS_LTR_PATH)
    sys.path.append(os.path.join(QGIS_PROJ_PATH, 'share'))
    sys.path.append(os.path.join(QGIS_GDAL_PATH, 'data'))
    sys.path.append(os.path.join(QGIS_GDAL_PATH, 'gdalplugins'))
    sys.path.append(QGIS_PLUGIN_PATH)

    # 6. Qt 平台插件路径（兜底：避免GUI渲染崩溃，QGIS 3.40 专属）
    os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = os.path.join(QGIS_BIN_PATH, 'platforms')

    # 7. 系统 PATH 路径（优先加载 QGIS 3.40 底层 DLL 库，避免版本冲突）
    os.environ['PATH'] = (
        f"{QGIS_BIN_PATH};"
        f"{os.path.join(QGIS_QGIS_LTR_PATH, 'bin')};"
        f"{os.path.join(QGIS_GDAL_PATH, 'bin')};"
        f"{os.environ['PATH']}"
    )

    # ===================== 验证 QGIS 3.40 关键路径有效性（提前拦截无效路径） =====================
    required_paths = [
        QGIS_BASE_PATH,
        QGIS_QGIS_LTR_PATH,
        QGIS_GDAL_PATH,
        QGIS_PROJ_PATH,
        os.environ['PROJ_LIB'],
        os.environ['GDAL_DATA']
    ]

    return required_paths


def init_plugs():
    from processing.core.Processing import Processing
    Processing.initialize()