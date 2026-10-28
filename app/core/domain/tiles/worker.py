"""矢量瓦片子进程 worker（由 :mod:`app.core.domain.tiles.parallel` 启动）。

读作业文件（JSON）→ 初始化无界面 QGIS → 跑一个缩放区间的
``native:writevectortiles_xyz`` → 把进度 / 日志写进指定的 JSONL 进度文件。

父进程按缩放级拆成多个这样的子进程并行执行；进度文件协议与影像切片 worker
相同，复用其 ``_ProgressWriter`` / ``_make_feedback``，避免两套实现漂移。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

# 直接 ``python worker.py`` 调试时，脚本目录（tiles/）会排在 sys.path 最前，
# 其中的 inspect.py 会遮蔽标准库 inspect，导致 dataclasses 导入失败。父进程
# 已改用 ``-m`` 启动；这里顺手把脚本目录踢掉，保证两种启动方式都能跑。
_SCRIPT_DIR = Path(__file__).resolve().parent
try:
    if sys.path and sys.path[0] and Path(sys.path[0]).resolve() == _SCRIPT_DIR:
        sys.path.pop(0)
except (OSError, ValueError):
    pass

#: QgsApplication 必须全程持有引用——被 GC 掉会让后续 QGIS 调用静默原生崩溃。
_APP = None


def main(job_file: str) -> int:
    """worker 入口；返回进程退出码（0 成功，非 0 失败）。"""
    try:
        payload = json.loads(Path(job_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 2
    # 复用影像切片 worker 的进度 / 反馈实现（纯 Python 顶层定义，不依赖 QGIS）
    from app.core.domain.imagery_tiles.worker import (
        _init_qgis,
        _make_feedback,
        _ProgressWriter,
    )

    writer = _ProgressWriter(Path(payload["progress"]))
    try:
        _run(payload, writer, _init_qgis, _make_feedback)
        return 0
    except Exception as exc:  # noqa: BLE001 - 子进程边界：任何异常都要回传父进程
        writer.error(f"{exc}")
        traceback.print_exc()
        return 1
    finally:
        writer.close()


def _run(payload: dict, writer, init_qgis, make_feedback) -> None:
    global _APP
    _APP = init_qgis()

    import processing  # noqa: PLC0415 - 依赖 initQgis 注册 provider

    params = {
        "OUTPUT_DIRECTORY": payload["output_dir"],
        "XYZ_TEMPLATE": payload["xyz_template"],
        "LAYERS": [{"layer": source} for source in payload["layers"]],
        "MIN_ZOOM": int(payload["min_zoom"]),
        "MAX_ZOOM": int(payload["max_zoom"]),
        "EXTENT": payload["extent"],
    }
    writer.log(
        "INFO",
        f"矢量瓦片：缩放 {payload['min_zoom']}-{payload['max_zoom']}，"
        f"图层 {len(payload['layers'])} 个 → {payload['output_dir']}",
    )
    feedback = make_feedback(writer)
    processing.run("native:writevectortiles_xyz", params, feedback=feedback)
    if feedback.failed:
        raise RuntimeError("矢量瓦片生成命令执行失败（详见任务日志）")
    writer.log("INFO", f"矢量瓦片：缩放 {payload['min_zoom']}-{payload['max_zoom']} 完成")
    writer.progress(100.0, force=True)
    writer.done()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
