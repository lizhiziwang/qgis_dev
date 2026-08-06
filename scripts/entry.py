"""PyInstaller 打包入口。

以**包外脚本**作为 PyInstaller 入口，避免直接以 ``app/main.py``（包内脚本）作为
入口时 ``from app.bootstrap import main`` 在 PyInstaller 模块图里解析失败、
打包后启动即 ``ModuleNotFoundError`` 的问题。

开发期仍用 ``python -m app.main`` 或 ``python scripts/run_dev.py``。
"""

from __future__ import annotations

from app.bootstrap import main

if __name__ == "__main__":
    raise SystemExit(main())
