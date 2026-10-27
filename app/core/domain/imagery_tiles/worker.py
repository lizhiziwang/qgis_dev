"""影像切片子进程 worker（由 :mod:`.worker_runner` 启动，不直接被界面调用）。

流程：读作业文件（JSON）→ 初始化 QGIS → 打开主进程另存的临时工程 →
按「仅渲染所选图层」隐藏其他图层 → 跑 ``native:tilesxyzdirectory`` /
``native:tilesxyzmbtiles`` → 收尾。

进度 / 日志写进作业文件里指定的 ``progress.jsonl``（一行一个 JSON），父进程
tail 该文件即可；不用 stdout 是因为打包成窗口程序后 ``sys.stdout`` 为
``None``，管道不可靠。

之所以单独开进程：算法的 ``processAlgorithm`` 在 C++ 里整段执行并持有 GIL，
放在主进程的任何线程里都会把界面卡住（详见 :mod:`.worker_runner` 模块说明）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path

#: QgsApplication 必须全程持有引用——被 GC 掉会让后续 QGIS 调用静默原生崩溃。
_APP = None


def main(job_file: str) -> int:
    """worker 入口；返回进程退出码（0 成功，非 0 失败）。"""
    try:
        payload = json.loads(Path(job_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 2
    writer = _ProgressWriter(Path(payload["progress"]))
    try:
        _run(payload, writer)
        return 0
    except Exception as exc:  # noqa: BLE001 - 子进程边界：任何异常都要回传父进程
        writer.error(f"{exc}")
        traceback.print_exc()
        return 1
    finally:
        writer.close()


def _run(payload: dict, writer: "_ProgressWriter") -> None:
    global _APP
    _APP = _init_qgis()

    from qgis.core import QgsProject, QgsRectangle  # noqa: PLC0415 - initQgis 之后才能 import

    from app.core.domain.imagery_tiles.isolation import isolate_other_layers  # noqa: PLC0415
    from app.core.domain.imagery_tiles.service import (  # noqa: PLC0415
        TILE_FORMAT_LABELS,
        ImageryTileJob,
    )

    kind = payload["kind"]
    project = QgsProject.instance()
    if not project.read(payload["project"]):
        raise RuntimeError("子进程打开临时工程失败")
    layer = project.mapLayer(payload["layer_id"])
    if layer is None:
        raise RuntimeError("临时工程里找不到要切片的图层")

    if payload.get("only_layer", True):
        try:
            hidden = isolate_other_layers(project, layer, "tile-worker")
        except Exception:  # noqa: BLE001 - 隔离失败不影响切片，只是可能很慢
            writer.log("WARNING", "临时隐藏其他图层失败，按工程当前可见图层切片")
            traceback.print_exc()
            hidden = []
        if hidden:
            writer.log(
                "INFO",
                f"仅渲染所选图层：已临时隐藏 {len(hidden)} 个其他可见图层"
                f"（{'、'.join(hidden)}）",
            )
        else:
            writer.log("INFO", "仅渲染所选图层：工程里没有其他可见图层")

    extent_values = payload.get("extent")
    extent = QgsRectangle(*extent_values) if extent_values else None

    title = "影像切片" if kind == "xyz" else "MBTiles 切片"
    writer.log("INFO", f"开始{title}：{layer.name()}")
    writer.log(
        "INFO",
        f"参数：缩放 {payload['min_zoom']}-{payload['max_zoom']}，"
        f"格式 {TILE_FORMAT_LABELS[int(payload['tile_format'])]}，"
        f"抗锯齿 {'开' if payload['antialias'] else '关'}",
    )
    writer.log("INFO", f"输出：{payload['output']}")

    if kind == "mbtiles":
        from app.core.domain.imagery_mbtiles.service import (  # noqa: PLC0415
            ImageryMbtilesJob,
        )

        job = ImageryMbtilesJob(
            layer=layer,
            output_file=payload["output"],
            min_zoom=payload["min_zoom"],
            max_zoom=payload["max_zoom"],
            tile_format=payload["tile_format"],
            antialias=payload["antialias"],
            extent_wgs84=extent,
        )
        algorithm = "native:tilesxyzmbtiles"
    else:
        job = ImageryTileJob(
            layer=layer,
            output_dir=payload["output"],
            min_zoom=payload["min_zoom"],
            max_zoom=payload["max_zoom"],
            tile_format=payload["tile_format"],
            tile_size=payload.get("tile_size", 256),
            antialias=payload["antialias"],
            extent_wgs84=extent,
        )
        algorithm = "native:tilesxyzdirectory"

    import processing  # noqa: PLC0415 - 依赖 initQgis

    feedback = _make_feedback(writer)
    processing.run(algorithm, job.to_native_params(), feedback=feedback)
    if feedback.failed:
        raise RuntimeError("瓦片生成命令执行失败（详见任务日志）")
    writer.log("INFO", f"{title}完成")
    writer.progress(100.0, force=True)
    writer.done()


# ------------------------------------------------------------------ QGIS 环境


def _init_qgis():
    """按应用同一套配置（dev 配置 / 打包运行时）初始化无界面 QGIS。"""
    # 允许直接 ``python worker.py`` 调试时也能 import app.*
    root = Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from app.bootstrap import _build_qgis_runtime, _resolve_config_path  # noqa: PLC0415
    from app.env.config import AppConfig  # noqa: PLC0415

    cfg = AppConfig.from_file(_resolve_config_path())
    runtime = _build_qgis_runtime(cfg)
    runtime.apply_env()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from qgis.core import QgsApplication  # noqa: PLC0415 - 必须在 apply_env 之后

    app = QgsApplication([b"tile-worker"], False)
    QgsApplication.setPrefixPath(str(runtime.qgis_prefix), True)
    QgsApplication.initQgis()

    # 注册 processing 提供者（native:tilesxyz* 就在其中）；主进程是在
    # _run_qgis_app 里做这一步，子进程没有那套启动流程，必须自己做。
    from processing.core.Processing import Processing  # noqa: PLC0415

    Processing.initialize()
    return app


# ------------------------------------------------------------------ 进度 / 日志


def _make_feedback(writer: "_ProgressWriter"):
    """构造把回调写成进度事件的 :class:`QgsProcessingFeedback`。

    进度信号用 ``DirectConnection``：算法在渲染线程里发 ``progressChanged``，
    排队投递要等父线程的事件循环，而 worker 主线程正卡在 ``processing.run``
    里，排队的进度会全部堆到结束才到——直接连接保证谁发谁写，进度实时。
    """
    from qgis.core import QgsProcessingFeedback  # noqa: PLC0415
    from qgis.PyQt.QtCore import Qt  # noqa: PLC0415

    class _Feedback(QgsProcessingFeedback):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False
            self.progressChanged.connect(
                self._on_progress, Qt.ConnectionType.DirectConnection
            )

        def _on_progress(self, value: float) -> None:
            writer.progress(float(value))

        def setProgressText(self, text: str) -> None:
            text = str(text).strip()
            if text:
                writer.text(text)

        def pushInfo(self, info: str) -> None:
            writer.log("INFO", str(info))

        def pushCommandInfo(self, info: str) -> None:
            writer.log("CMD", str(info))

        def pushConsoleInfo(self, info: str) -> None:
            writer.log("DEBUG", str(info))

        def pushWarning(self, info: str) -> None:
            writer.log("WARNING", str(info))

        def reportError(self, error: str, fatalError: bool = False) -> None:
            text = str(error)
            if "error code" in text.lower():
                self.failed = True
            writer.log("ERROR", text)

    return _Feedback()


class _ProgressWriter:
    """把事件按 JSON 行追加到进度文件（线程安全，逐行 flush）。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._last_progress = -1.0

    def log(self, level: str, message: str) -> None:
        self._write({"t": "log", "level": level, "msg": str(message)})

    def progress(self, value: float, force: bool = False) -> None:
        value = max(0.0, min(100.0, float(value)))
        with self._lock:
            if not force and value - self._last_progress < 0.9 and value < 100.0:
                return
            self._last_progress = value
            self._emit({"t": "p", "v": round(value, 1)})

    def text(self, value: str) -> None:
        self._write({"t": "text", "msg": str(value)})

    def error(self, message: str) -> None:
        self._write({"t": "err", "msg": str(message)})

    def done(self) -> None:
        self._write({"t": "done"})

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass

    def _write(self, event: dict) -> None:
        with self._lock:
            self._emit(event)

    def _emit(self, event: dict) -> None:
        try:
            self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._fh.flush()
        except (OSError, ValueError):
            # 进度文件写不进去不能影响切片本身（父进程仍靠退出码判断结果）
            pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
