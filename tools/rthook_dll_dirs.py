"""PyInstaller 运行时钩子：隔离随包运行时与宿主机的同名 DLL。

背景
----
这是便携包，**必须只用随包运行时**。但两个因素会让宿主机的 DLL 抢进来：

1. ``qgis/__init__.py``（Windows）会把 ``PATH`` 里的**每一个**目录都执行
   ``os.add_dll_directory(p)``；
2. ``PATH`` 上常见的 OSGeo4W / conda / 开发安装会提供 ``gdal313.dll``、
   ``proj_9.dll``、``pdalcpp.dll``、``geos_c.dll``、``MSVCP140.dll`` 等与随包
   **同名但不同版本**的 DLL。

于是 ``qgis_core.dll`` 的依赖可能解析到宿主机版本，与随包的 ``qgis/_core.pyd``
不匹配，启动即报::

    ImportError: DLL load failed while importing _core:
    The specified procedure could not be found.      # ERROR_PROC_NOT_FOUND

本钩子在 ``qgis`` 被 import 之前运行：

1. 把 PyInstaller 的 ``_MEIPASS``（``_internal/``）加进 DLL 搜索目录并前置到
   ``PATH``；
2. 从 ``PATH`` 里剔除**提供了随包同名 DLL**的外部目录（系统目录除外），
   这样 ``qgis/__init__.py`` 也不会再把它们 ``add_dll_directory`` 进来。

结果：随包运行时始终优先，不受宿主机装没装 QGIS/GDAL/Qt 影响。
"""

from __future__ import annotations

import os
import sys


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except OSError:
        return path


def _collect_bundled_dll_names(meipass: str) -> set[str]:
    """收集随包目录下所有 DLL 的文件名（小写）。"""
    names: set[str] = set()
    if not meipass or not os.path.isdir(meipass):
        return names
    for _root, _dirs, files in os.walk(meipass):
        for name in files:
            if name.lower().endswith(".dll"):
                names.add(name.lower())
    return names


def _main() -> None:
    if not getattr(sys, "frozen", False):
        return

    meipass = getattr(sys, "_MEIPASS", "")
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    bundle_roots = {_norm(d) for d in (meipass, exe_dir) if d}
    system_root = _norm(os.environ.get("SystemRoot", r"C:\Windows"))

    # 1) 随包目录优先
    if meipass and os.path.isdir(meipass):
        os.environ["PATH"] = meipass + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(meipass)
            except OSError:
                pass

    # 2) 剔除提供了“随包同名 DLL”的外部目录
    bundled = _collect_bundled_dll_names(meipass)
    if not bundled:
        return

    kept: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        norm = _norm(entry)
        if any(norm == root or norm.startswith(root + os.sep) for root in bundle_roots):
            kept.append(entry)
            continue
        if norm == system_root or norm.startswith(system_root + os.sep):
            kept.append(entry)  # 系统目录不动
            continue
        try:
            provided = {n.lower() for n in os.listdir(entry) if n.lower().endswith(".dll")}
        except OSError:
            kept.append(entry)
            continue
        if provided & bundled:
            continue  # 与随包 DLL 冲突，丢弃
        kept.append(entry)

    os.environ["PATH"] = os.pathsep.join(kept)


_main()
