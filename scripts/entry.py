"""PyInstaller 打包入口。

以**包外脚本**作为 PyInstaller 入口，避免直接以 ``app/main.py``（包内脚本）作为
入口时 ``from app.bootstrap import main`` 在 PyInstaller 模块图里解析失败、
打包后启动即 ``ModuleNotFoundError`` 的问题。

开发期仍用 ``python -m app.main`` 或 ``python scripts/run_dev.py``。

``--version`` / ``--version-out`` 在这里短路：只读构建元数据，不导入
``app.bootstrap``，因此不会初始化 Qt / QGIS，更新器可以在主程序运行时安全调用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 源码直跑 ``python scripts/entry.py`` 时 sys.path[0] 是 scripts/，补上项目根；
# PyInstaller 冻结态已把项目根放进 sys.path，这里是幂等操作。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_VERSION_FLAGS = {"--version", "-V"}
_VERSION_OUT_FLAGS = {"--version-out"}


def _version_payload() -> str:
    from app.build_meta import as_json, read_build_info

    return as_json(read_build_info(), indent=None)


def _version_out_path(argv: list[str]) -> str | None:
    """解析 ``--version-out PATH`` / ``--version-out=PATH``；未出现返回 ``None``。"""
    for i, arg in enumerate(argv):
        if arg in _VERSION_OUT_FLAGS:
            if i + 1 >= len(argv):
                raise SystemExit("--version-out 需要一个文件路径")
            return argv[i + 1]
        for flag in _VERSION_OUT_FLAGS:
            if arg.startswith(flag + "="):
                return arg.split("=", 1)[1]
    return None


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _emit(text: str) -> None:
    """尽力写 stdout；windowed 冻结态没有控制台，写不出去不算错误。"""
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass


def main() -> int:
    argv = sys.argv[1:]
    version_requested = any(arg in _VERSION_FLAGS or arg.startswith("--version=") for arg in argv)
    try:
        out_path = _version_out_path(argv)
    except SystemExit as exc:
        _emit(str(exc))
        return 2

    if version_requested or out_path is not None:
        try:
            payload = _version_payload()
        except Exception as exc:  # noqa: BLE001 —— 版本快路径失败也必须给出可诊断信息
            _emit(f'{{"error": "{type(exc).__name__}: {exc}"}}')
            return 1
        if out_path is not None:
            try:
                _write_atomic(Path(out_path), payload)
            except OSError as exc:
                _emit(f'{{"error": "write {out_path}: {exc}"}}')
                return 1
        _emit(payload)
        return 0

    from app.bootstrap import main as run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
