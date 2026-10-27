"""影像切片子进程执行器（父进程侧）。

背景：``native:tilesxyzdirectory`` / ``native:tilesxyzmbtiles`` 的
``processAlgorithm`` 在 C++ 里整段跑完，调用期间一直持有 Python GIL。放在普通
``threading.Thread`` 里虽然不会像模态窗口那样卡死，但只要一次切片跑几秒，
主线程就拿不到 GIL——实测 6.7 s 的切片里主线程最长停顿 5.3 s，界面上的其他
功能基本没法用。

所以切片改为**独立子进程**执行：

1. 主进程（本模块）：把当前工程另存为临时工程（含未保存的样式/图层）
   → 启动子进程 → 轮询进度文件 → 收尾清理；
2. 子进程（:mod:`app.core.domain.imagery_tiles.worker`）：读临时工程、按
   「仅渲染所选图层」隐藏其他图层、跑算法，把进度 / 日志按 JSON 行写进
   进度文件。

子进程与主进程彻底隔离，主线程不再被 GIL 拖住；两边用**同一套**参数
（``job.to_native_params()``）和同一个算法，输出瓦片与原来的进程内切片逐字节
一致（已用 579 张瓦片抽样比对）。

子进程进度不直接走 Qt 信号 / 管道：打包成窗口程序后 ``sys.stdout`` 为
``None``，管道不可靠；统一写 UTF-8 进度文件，父进程 tail 它即可，开发态与
打包态行为一致。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.infra.fs.paths import Paths
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 子进程临时目录保留时长；超过即视为上次异常退出的残留，启动新任务时清掉
_STALE_SECONDS = 24 * 3600

#: 正在跑的子进程。任务线程是守护线程，应用直接退出时不会等它收尾，
#: 这里在解释器退出时统一杀掉，避免留下孤儿进程继续写瓦片目录。
_ACTIVE: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def _register(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)


def _unregister(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(proc)


@atexit.register
def _kill_active_workers() -> None:
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
        _ACTIVE.clear()
    for proc in procs:
        kill_worker(proc)


@dataclass
class WorkerSpec:
    """一次子进程切片所需的路径集合。"""

    work_dir: Path
    project_file: Path
    job_file: Path
    progress_file: Path
    output: str

    def cleanup(self) -> None:
        """删除本次任务的临时工程 / 进度文件。"""
        shutil.rmtree(self.work_dir, ignore_errors=True)


@dataclass
class WorkerCallbacks:
    """子进程事件 → 任务信号的回调集合（都在任务线程里调用）。

    :param progress: ``int`` 百分比
    :param text: 算法阶段性文字
    :param log: ``(level, message)``
    """

    progress: Callable[[int], None] | None = None
    text: Callable[[str], None] | None = None
    log: Callable[[str, str], None] | None = None


@dataclass
class WorkerResult:
    """子进程执行结果。"""

    canceled: bool = False
    exit_code: int | None = None
    error: str = ""
    events: int = field(default=0, repr=False)

    @property
    def ok(self) -> bool:
        return not self.canceled and self.exit_code == 0 and not self.error


def run_worker_process(
    spec: WorkerSpec,
    cancel_event: threading.Event,
    callbacks: WorkerCallbacks | None = None,
    poll_interval: float = 0.1,
) -> WorkerResult:
    """启动子进程并等它结束，期间把进度 / 日志转给回调。

    在任务的普通线程里调用（不碰界面）；``cancel_event`` 置位后立即终止
    子进程，结果标记为已取消。
    """
    callbacks = callbacks or WorkerCallbacks()
    result = WorkerResult()
    tail = ProgressTail(spec.progress_file)
    proc = spawn_worker(spec)
    _register(proc)
    killed = False
    try:
        while True:
            _dispatch(tail.read(), callbacks, result)
            if proc.poll() is not None:
                break
            if cancel_event.is_set() and not killed:
                kill_worker(proc)
                killed = True
            time.sleep(poll_interval)
        # 进程退出后再读一次，别丢最后几行（完成 / 错误提示可能就在里面）
        _dispatch(tail.read(), callbacks, result)
        result.exit_code = proc.returncode
    finally:
        if proc.poll() is None:
            kill_worker(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("影像切片：子进程未在 5 秒内退出，强制终止")
            kill_worker(proc)
        _unregister(proc)

    if killed or cancel_event.is_set():
        result.canceled = True
    elif result.exit_code != 0:
        detail = result.error or read_worker_log(spec)
        result.error = f"切片进程异常退出（退出码 {result.exit_code}）"
        if detail:
            result.error += f"：{detail}"
    return result


def _dispatch(
    events: list[dict], callbacks: WorkerCallbacks, result: WorkerResult
) -> None:
    """把子进程事件转成回调 / 结果字段。"""
    for event in events:
        result.events += 1
        t = event.get("t")
        if t == "p":
            if callbacks.progress is not None:
                try:
                    callbacks.progress(int(float(event.get("v", 0))))
                except (TypeError, ValueError):
                    continue
        elif t == "text":
            if callbacks.text is not None:
                callbacks.text(str(event.get("msg", "")))
        elif t == "log":
            if callbacks.log is not None:
                callbacks.log(
                    str(event.get("level", "INFO")), str(event.get("msg", ""))
                )
        elif t == "err":
            result.error = str(event.get("msg", "")).strip() or result.error



def prepare_worker_job(job, kind: str, only_layer: bool = True) -> WorkerSpec | None:
    """另存临时工程并写出子进程作业文件。

    必须在**主线程**调用：``QgsProject.write()`` 会读写工程单例，不能在后台
    线程碰。

    :param job: :class:`~app.core.domain.imagery_tiles.service.ImageryTileJob`
        或 :class:`~app.core.domain.imagery_mbtiles.service.ImageryMbtilesJob`。
    :param kind: ``"xyz"``（目录输出）或 ``"mbtiles"``（单文件输出）。
    :param only_layer: 是否让子进程只渲染所选图层（对应界面「仅渲染所选图层」）。
    :return: 成功返回 :class:`WorkerSpec`；另存工程失败返回 ``None``，调用方
        退回进程内切片。
    """
    from qgis.core import Qgis, QgsProject  # noqa: PLC0415 - 模块顶层不依赖 qgis

    root = _worker_root()
    work_dir = root / uuid.uuid4().hex
    work_dir.mkdir(parents=True)
    project_file = work_dir / "project.qgs"
    project = QgsProject.instance()

    # 另存到别的目录时必须用绝对路径，否则相对路径会按新位置解析，子进程里
    # 图层全部打不开。
    original_file = project.fileName()
    was_dirty = project.isDirty()
    storage = project.filePathStorage()
    project.blockSignals(True)
    try:
        project.setFilePathStorage(Qgis.FilePathType.Absolute)
        saved = project.write(str(project_file))
    except Exception:  # noqa: BLE001 - 另存失败交给调用方退回进程内执行
        logger.warning("影像切片：另存临时工程失败，退回进程内执行", exc_info=True)
        saved = False
    finally:
        # write() 会把工程的文件名改成临时文件、并清掉「已修改」标记，全部还原：
        # 否则用户点保存会存到临时工程里，标题栏也会变成临时路径。
        try:
            project.setFilePathStorage(storage)
            project.setFileName(original_file)
            project.setDirty(was_dirty)
        finally:
            project.blockSignals(False)

    if not saved:
        shutil.rmtree(work_dir, ignore_errors=True)
        return None

    output = job.output_dir if kind == "xyz" else job.output_file
    output = os.path.abspath(str(output or ""))
    extent = job.slice_extent_wgs84()
    payload = {
        "kind": kind,
        "project": str(project_file),
        "layer_id": job.layer.id(),
        "output": output,
        "min_zoom": int(job.min_zoom),
        "max_zoom": int(job.max_zoom),
        "tile_format": int(job.tile_format),
        "tile_size": int(getattr(job, "tile_size", 256)),
        "antialias": bool(job.antialias),
        "html": bool(job.html),
        "extent": [
            extent.xMinimum(),
            extent.yMinimum(),
            extent.xMaximum(),
            extent.yMaximum(),
        ],
        "only_layer": bool(only_layer),
        "progress": str(work_dir / "progress.jsonl"),
    }
    job_file = work_dir / "job.json"
    job_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return WorkerSpec(
        work_dir=work_dir,
        project_file=project_file,
        job_file=job_file,
        progress_file=Path(payload["progress"]),
        output=output,
    )


def spawn_worker(spec: WorkerSpec) -> subprocess.Popen:
    """按 :class:`WorkerSpec` 启动子进程。

    子进程的输出（QGIS / GDAL 自己的告警等，不是本模块的进度协议）重定向到
    ``worker.log``，出问题时随错误信息一并回显。
    """
    env = os.environ.copy()
    # 子进程没有界面：用 offscreen 平台插件，避免在任务栏闪一个窗口 / 抢焦点
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    paths = Paths.default()
    extra = [str(paths.project_root)]
    old = env.get("PYTHONPATH")
    if old:
        extra.append(old)
    env["PYTHONPATH"] = os.pathsep.join(extra)

    creationflags = 0
    if os.name == "nt":
        # 打包成窗口程序时也别让子进程弹控制台窗口
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_file = spec.work_dir / "worker.log"
    with log_file.open("wb") as out:
        return subprocess.Popen(
            worker_command(spec.job_file),
            cwd=str(paths.project_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )


def worker_command(job_file: Path) -> list[str]:
    """构造启动子进程的命令行（开发态 / PyInstaller 打包态通用）。"""
    if getattr(sys, "frozen", False):
        # 打包后 sys.executable 是应用自己的 exe；由 bootstrap 早期分支
        # （app/bootstrap/__init__.py 的 --tile-worker）转进 worker.main。
        return [sys.executable, "--tile-worker", str(job_file)]
    worker_py = Path(__file__).with_name("worker.py")
    return [sys.executable, "-u", str(worker_py), str(job_file)]


def kill_worker(proc: subprocess.Popen | None) -> None:
    """终止子进程（取消失败/关闭窗口时用）。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.kill()
    except OSError:
        logger.warning("影像切片：终止子进程失败", exc_info=True)


def read_worker_log(spec: WorkerSpec, limit: int = 1200) -> str:
    """读取子进程日志尾部，用于错误提示。"""
    try:
        text = (spec.work_dir / "worker.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.strip()
    return text[-limit:]


class ProgressTail:
    """增量读取子进程进度文件（JSON 行协议）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._offset = 0
        self._pending = ""

    def read(self) -> list[dict]:
        """返回上次读取以来新增的完整事件；半行留到下次。"""
        try:
            with self._path.open("rb") as fh:
                fh.seek(self._offset)
                data = fh.read()
        except FileNotFoundError:
            return []
        except OSError:
            logger.debug("影像切片：读取进度文件失败", exc_info=True)
            return []
        if not data:
            return []
        self._offset += len(data)
        text = self._pending + data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._pending = lines.pop()
        events: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events


def _worker_root() -> Path:
    """子进程工作目录：``<tmp>/tile_worker``，顺手清掉过期残留。"""
    root = Paths.default().tmp / "tile_worker"
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        for child in root.iterdir():
            try:
                if child.is_dir() and now - child.stat().st_mtime > _STALE_SECONDS:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass
    return root


__all__ = [
    "ProgressTail",
    "WorkerCallbacks",
    "WorkerResult",
    "WorkerSpec",
    "kill_worker",
    "prepare_worker_job",
    "read_worker_log",
    "run_worker_process",
    "spawn_worker",
    "worker_command",
]
