"""加载用户自定义表达式函数。

背景：本应用没有走 QGIS 启动器，而是自己 ``QgsApplication.initQgis()``，
少了 QGIS 原生启动时 import ``qgis.user`` 那一步。于是 QGIS「函数编辑器」
（表达式构建器 → Function Editor）里写的自定义函数不会被注册，
:class:`QgsExpression` 的函数表里没有它们，表达式编辑器里既看不到也用不了。

另一个坑：``QgsApplication`` 的 applicationName 默认取可执行文件名
（``python``），所以 ``qgisSettingsDirPath()`` 指向
``%APPDATA%/python/profiles/default``——而不是标准 QGIS 的
``%APPDATA%/QGIS/QGIS4/profiles/default``。用户通常是在标准 QGIS 里写的
自定义函数，因此这里除了当前 profile，还会扫标准 QGIS profile（同一大版本）。

加载方式与 QGIS 一致：逐个执行 ``expressions/*.py``，靠文件里的
``@qgsfunction`` 自注册。文件名相同的 ``updateLabel`` 会互相覆盖（QGIS 也
如此，后加载的赢），这是 QGIS 语义，不在这里做特殊处理。
"""

from __future__ import annotations

import glob
import os
from typing import Dict, List

from qgis.core import Qgis, QgsApplication

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 执行用户函数文件时预置的命名空间（对应 QGIS 函数编辑器模板里的两行 import）
_PRELUDE = "from qgis.core import *\nfrom qgis.gui import *\n"

#: 已加载标记：重复调用直接返回（函数表是全局的，重跑没有意义）
_loaded_modules: set[str] = set()

#: 执行用户文件用的命名空间，**必须常驻**。
#:
#: ``@qgsfunction`` 的返回值是 :class:`QgsPyExpressionFunction`，并被绑到模块全局名
#: 上；而 ``QgsExpression.registerFunction`` 在 C++ 侧接管了指针。若命名空间被回收，
#: 这些 Python 包装对象会被 GC，注册表就留下**悬垂指针**——之后任何
#: ``QgsExpression.Functions()`` / 求值 / 表达式构建器都会直接段错误。
#: QGIS 走 ``qgis.user`` 时是 ``import`` 真模块，天然常驻；这里手动保住引用。
_loaded_namespaces: List[Dict[str, object]] = []


def _expressions_dirs() -> List[str]:
    """待扫描的 ``expressions`` 目录，标准 QGIS profile 在前、当前 profile 在后。

    后者最后加载，同名函数以当前 profile 为准。
    """
    candidates: List[str] = []

    for path in sorted(_standard_profile_dirs()):
        candidates.append(path)
    try:
        candidates.append(
            os.path.join(QgsApplication.qgisSettingsDirPath(), "python", "expressions")
        )
    except Exception:  # noqa: BLE001
        logger.debug("取 qgisSettingsDirPath 失败", exc_info=True)

    # 去重（大小写不敏感，避免 Windows 下同一目录被扫两遍）
    seen: set[str] = set()
    unique: List[str] = []
    for path in candidates:
        key = os.path.normcase(os.path.normpath(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _standard_profile_dirs() -> List[str]:
    """标准 QGIS profile 的 ``expressions`` 目录（只扫与当前 QGIS 同大版本的）。"""
    try:
        major = Qgis.QGIS_VERSION.split(".")[0]
    except Exception:  # noqa: BLE001
        major = "4"
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    pattern = os.path.join(base, "QGIS", f"QGIS{major}", "profiles", "*", "python", "expressions")
    return glob.glob(pattern)


def _exec_namespace() -> Dict[str, object]:
    """构造执行用户函数文件的命名空间，保证裸用 ``@qgsfunction`` 也能跑。"""
    ns: Dict[str, object] = {"__name__": "user_expression"}
    try:
        exec(_PRELUDE, ns)  # noqa: S102 - 与 QGIS 函数编辑器一致的预置导入
    except Exception:  # noqa: BLE001
        logger.debug("表达式预置导入失败", exc_info=True)
    # 用户文件常常没有 import（QGIS 函数编辑器模板会补，手写的不一定），
    # 显式塞进去，保证 @qgsfunction 一定可用。
    try:
        from qgis.core import qgsfunction, register_function

        ns["qgsfunction"] = qgsfunction
        ns["register_function"] = register_function
    except Exception:  # noqa: BLE001
        logger.debug("取 qgsfunction 失败", exc_info=True)
    try:
        from qgis.PyQt.QtCore import QVariant

        ns["QVariant"] = QVariant
    except Exception:  # noqa: BLE001
        logger.debug("取 QVariant 失败", exc_info=True)
    return ns


def load_user_expression_functions(force: bool = False) -> int:
    """扫描并执行用户自定义表达式函数文件，返回成功加载的文件数。

    :param force: 为 True 时忽略"已加载"标记重新扫描（QGIS 的函数编辑器
        保存后即自注册，正常不需要；给测试与热重载用）。
    """
    loaded = 0
    for directory in _expressions_dirs():
        if not os.path.isdir(directory):
            continue
        for path in sorted(glob.glob(os.path.join(directory, "*.py"))):
            if os.path.basename(path) == "__init__.py":
                continue
            key = os.path.normcase(os.path.normpath(path))
            if not force and key in _loaded_modules:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    source = fh.read()
                ns = _exec_namespace()
                ns["__file__"] = path
                exec(compile(source, path, "exec"), ns)  # noqa: S102
            except Exception:  # noqa: BLE001
                logger.warning("加载自定义表达式函数失败：%s", path, exc_info=True)
                continue
            # 保住命名空间引用，否则 @qgsfunction 注册的 QgsPyExpressionFunction
            # 会被 GC，注册表留下悬垂指针 → 后续使用直接崩溃（见模块注释）。
            _loaded_namespaces.append(ns)
            _loaded_modules.add(key)
            loaded += 1
            logger.debug("已加载自定义表达式函数：%s", path)

    if loaded:
        logger.info("已加载 %d 个自定义表达式函数文件", loaded)
    return loaded
