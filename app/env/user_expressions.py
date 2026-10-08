"""加载 / 编辑用户自定义表达式函数。

背景：本应用没有走 QGIS 启动器，而是自己 ``QgsApplication.initQgis()``，
少了 QGIS 原生启动时 import ``qgis.user`` 那一步。于是 QGIS「函数编辑器」
（表达式构建器 → Function Editor）里写的自定义函数不会被注册，
:class:`QgsExpression` 的函数表里没有它们，表达式编辑器里既看不到也用不了。

另一个坑：``QgsApplication`` 的 applicationName 默认取可执行文件名
（``python``），所以 ``qgisSettingsDirPath()`` 指向
``%APPDATA%/python/profiles/default``——而不是标准 QGIS 的
``%APPDATA%/QGIS/QGIS4/profiles/default``。用户通常是在标准 QGIS 里写的
自定义函数，因此这里除了当前 profile，还会扫标准 QGIS profile（同一大版本）。

原生「函数编辑器」在本应用里其实不可用：它靠 ``QgsPythonRunner`` 取保存目录
（``qgis.user.expressionspath``）并执行注册代码，而 runner 实例在
``qgis_app.dll`` 里、只有原生 ``QgisApp`` 启动时才会 ``setInstance``。本应用
没走那条路，runner 无效 → 保存会写到 ``<当前盘符>:\\<名>.py`` 且不注册。所以
本模块同时对外提供 :func:`expressions_dir` / :func:`list_function_files` /
:func:`save_function_file` / :func:`delete_function_file`，供自建的
``FunctionEditorDialog`` 使用，绕开原生函数编辑器。

加载方式与 QGIS 一致：逐个执行 ``expressions/*.py``，靠文件里的
``@qgsfunction`` 自注册。文件名相同的函数会互相覆盖（QGIS 也如此，后加载的
赢），这是 QGIS 语义，不在这里做特殊处理。
"""

from __future__ import annotations

import glob
import os
import traceback
from typing import Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsExpression,
    QgsExpressionFunction,
)

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 执行用户函数文件时预置的命名空间（对应 QGIS 函数编辑器模板里的两行 import）
_PRELUDE = "from qgis.core import *\nfrom qgis.gui import *\n"

#: 新建函数文件时的模板（与 QGIS 函数编辑器一致）
FUNCTION_TEMPLATE = '''from qgis.core import *
from qgis.gui import *

@qgsfunction(group='Custom', referenced_columns=[])
def my_sum(value1, value2):
    """
    计算参数 value1 与 value2 之和。
    <h2>示例：</h2>
    <ul>
      <li>my_sum(5, 8) -> 13</li>
      <li>my_sum("field1", "field2") -> 42</li>
    </ul>
    """
    return value1 + value2
'''

#: 已加载标记：重复调用直接返回（函数表是全局的，重跑没有意义）
_loaded_modules: set[str] = set()

#: 每个文件注册的函数对象所在的命名空间，**必须常驻**。
#:
#: ``@qgsfunction`` 的返回值是 :class:`QgsPyExpressionFunction`，并被绑到模块全局名
#: 上；而 ``QgsExpression.registerFunction`` 在 C++ 侧接管了指针。若命名空间被回收，
#: 这些 Python 包装对象会被 GC，注册表就留下**悬垂指针**——之后任何
#: ``QgsExpression.Functions()`` / 求值 / 表达式构建器都会直接段错误。
#: QGIS 走 ``qgis.user`` 时是 ``import`` 真模块，天然常驻；这里手动保住引用。
_file_namespaces: dict[str, dict[str, object]] = {}

#: 每个文件当前注册的函数名（重载 / 删除时用于注销被删除或改名的函数）
_file_function_names: dict[str, set[str]] = {}


def _key(path: str) -> str:
    """归一化路径，用作缓存键（Windows 大小写不敏感）。"""
    return os.path.normcase(os.path.normpath(path))


def _expressions_dirs() -> list[str]:
    """待扫描的 ``expressions`` 目录，标准 QGIS profile 在前、当前 profile 在后。

    后者最后加载，同名函数以当前 profile 为准。
    """
    candidates: list[str] = []

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
    unique: list[str] = []
    for path in candidates:
        key = _key(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _standard_profile_dirs() -> list[str]:
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


def _current_major() -> str:
    try:
        return Qgis.QGIS_VERSION.split(".")[0]
    except Exception:  # noqa: BLE001
        return "4"


def expressions_dir() -> str:
    """用户自定义函数文件的读写目录（不存在则创建）。

    优先标准 QGIS profile（``%APPDATA%/QGIS/QGIS<major>/profiles/default/
    python/expressions``），这样独立 QGIS 与本应用共用同一份函数；拿不到时
    退回当前 profile 的 ``qgisSettingsDirPath()/python/expressions``。
    """
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    preferred = os.path.join(
        base, "QGIS", f"QGIS{_current_major()}", "profiles", "default",
        "python", "expressions",
    )
    candidates = [preferred]
    try:
        candidates.append(
            os.path.join(QgsApplication.qgisSettingsDirPath(), "python", "expressions")
        )
    except Exception:  # noqa: BLE001
        logger.debug("取 qgisSettingsDirPath 失败", exc_info=True)

    for directory in candidates:
        try:
            os.makedirs(directory, exist_ok=True)
            initfile = os.path.join(directory, "__init__.py")
            if not os.path.exists(initfile):
                with open(initfile, "a", encoding="utf-8"):
                    pass
            return directory
        except OSError:
            logger.debug("创建表达式函数目录失败：%s", directory, exc_info=True)
    return preferred


def list_function_files() -> list[str]:
    """函数文件绝对路径列表（升序，不含 ``__init__.py``）。"""
    directory = expressions_dir()
    paths: list[str] = []
    for path in sorted(glob.glob(os.path.join(directory, "*.py"))):
        if os.path.basename(path) == "__init__.py":
            continue
        paths.append(path)
    return paths


def _exec_namespace() -> dict[str, object]:
    """构造执行用户函数文件的命名空间，保证裸用 ``@qgsfunction`` 也能跑。"""
    ns: dict[str, object] = {"__name__": "user_expression"}
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


def _registered_names(ns: dict[str, object]) -> set[str]:
    """命名空间里由 ``@qgsfunction`` 注册的函数名（按注册表里的真实名）。"""
    names: set[str] = set()
    for obj in ns.values():
        if isinstance(obj, QgsExpressionFunction):
            names.add(obj.name())
    return names


def _unregister(name: str) -> None:
    try:
        QgsExpression.unregisterFunction(name)
    except Exception:  # noqa: BLE001
        logger.debug("注销自定义函数失败：%s", name, exc_info=True)


def _execute_file(path: str, source: str) -> tuple[set[str], Optional[str]]:
    """执行函数文件源码并注册，返回 ``(注册的函数名, 错误信息或 None)``。

    重载前会注销该文件上一次注册、但本次已不存在（删除 / 改名）的函数，
    避免注册表里留下幽灵函数。
    """
    key = _key(path)
    previous = _file_function_names.get(key, set())
    ns = _exec_namespace()
    ns["__file__"] = path
    try:
        exec(compile(source, path, "exec"), ns)  # noqa: S102
    except Exception:  # noqa: BLE001
        error = traceback.format_exc()
        logger.warning("加载自定义表达式函数失败：%s", path, exc_info=True)
        return set(), error

    names = _registered_names(ns)
    for stale in previous - names:
        _unregister(stale)
    # 保住命名空间引用，否则 @qgsfunction 注册的 QgsPyExpressionFunction
    # 会被 GC，注册表留下悬垂指针 → 后续使用直接崩溃（见模块注释）。
    _file_namespaces[key] = ns
    _file_function_names[key] = names
    _loaded_modules.add(key)
    return names, None


def register_function_file(path: str) -> tuple[int, Optional[str]]:
    """读取并执行一个函数文件，返回 ``(注册的函数数, 错误信息或 None)``。"""
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        logger.warning("读取自定义表达式函数失败：%s", path, exc_info=True)
        return 0, traceback.format_exc()
    names, error = _execute_file(path, source)
    if error is None:
        logger.info("已加载自定义表达式函数 %s（%d 个）", os.path.basename(path), len(names))
    return len(names), error


def save_function_file(path: str, source: str) -> tuple[int, Optional[str]]:
    """写入函数文件并立即执行注册，返回 ``(注册的函数数, 错误信息或 None)``。

    即使源码有语法 / 运行错误也会先落盘（方便用户接着改），此时错误信息非 None。
    """
    directory = os.path.dirname(path)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            logger.warning("创建函数目录失败：%s", directory, exc_info=True)
            return 0, traceback.format_exc()
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(source)
    except OSError:
        logger.warning("写入自定义表达式函数失败：%s", path, exc_info=True)
        return 0, traceback.format_exc()
    names, error = _execute_file(path, source)
    return len(names), error


def delete_function_file(path: str) -> bool:
    """注销该文件注册的函数并删除文件。"""
    key = _key(path)
    for name in _file_function_names.get(key, set()):
        _unregister(name)
    _file_function_names.pop(key, None)
    _file_namespaces.pop(key, None)
    _loaded_modules.discard(key)
    try:
        os.remove(path)
        return True
    except OSError:
        logger.warning("删除自定义表达式函数失败：%s", path, exc_info=True)
        return False


def load_user_expression_functions(force: bool = False) -> int:
    """扫描并执行用户自定义表达式函数文件，返回成功加载的文件数。

    :param force: 为 True 时忽略"已加载"标记重新扫描并执行（用于热重载；
        自建的函数编辑器保存本身就是即时注册，正常不需要）。
    """
    loaded = 0
    for directory in _expressions_dirs():
        if not os.path.isdir(directory):
            continue
        for path in sorted(glob.glob(os.path.join(directory, "*.py"))):
            if os.path.basename(path) == "__init__.py":
                continue
            key = _key(path)
            if not force and key in _loaded_modules:
                continue
            _names, error = register_function_file(path)
            if error is None:
                loaded += 1

    if loaded:
        logger.info("已加载 %d 个自定义表达式函数文件", loaded)
    return loaded
