"""启动健康标记（更新器判断“新版本是否真的能起来”）。

更新器在安装前清空标记，安装并拉起新程序后等待：

- ``.process-started``：进程已进入 :func:`app.bootstrap.main`（最早写入）；
- ``.startup-ok``：主窗口已显示、启动回调开始执行（真正可用）。

只有 ``.startup-ok`` 且 ``versionCode`` 达到目标版本才算成功；否则更新器回滚。
两个标记都写在每用户更新目录的 ``markers/`` 下（更新器以 root / 独立进程运行，
不需要读应用日志目录，也避免便携包目录只读的问题）。

标记写入必须是**纯标准库**且不依赖 logging——:.func:`mark_process_started`
在日志装配之前调用。
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROCESS_STARTED = ".process-started"
STARTUP_OK = ".startup-ok"


def marker_dir() -> Path:
    from app.update import paths as update_paths

    return update_paths.markers_dir()


def mark_process_started(extra: dict[str, Any] | None = None) -> Path | None:
    """进程启动标记；写失败只返回 ``None``（绝不能阻断启动）。"""
    return _write(PROCESS_STARTED, extra)


def mark_startup_ok(extra: dict[str, Any] | None = None) -> Path | None:
    """主窗口显示后的健康标记。"""
    return _write(STARTUP_OK, extra)


def clear_markers() -> None:
    directory = marker_dir()
    for name in (PROCESS_STARTED, STARTUP_OK):
        with contextlib.suppress(OSError):
            (directory / name).unlink()


def read_marker(name: str) -> dict[str, Any] | None:
    try:
        raw = (marker_dir() / name).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(name: str, extra: dict[str, Any] | None) -> Path | None:
    payload: dict[str, Any] = _build_payload()
    if extra:
        payload.update({str(k): v for k, v in extra.items()})
    try:
        directory = marker_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        temp = directory / (name + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temp, path)
        return path
    except OSError:
        return None


def _build_payload() -> dict[str, Any]:
    from app import VERSION_CODE, __version__
    from app.build_meta import read_build_info

    info = read_build_info()
    return {
        "pid": os.getpid(),
        "time": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "version": __version__,
        "versionCode": VERSION_CODE,
        "buildId": info.get("buildId", "?"),
        "runtimeId": info.get("runtimeId", "?"),
    }


__all__ = [
    "PROCESS_STARTED",
    "STARTUP_OK",
    "clear_markers",
    "mark_process_started",
    "mark_startup_ok",
    "marker_dir",
    "read_marker",
]
