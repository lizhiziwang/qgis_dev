"""矢量瓦片按缩放级并行：拆分层级 + 子进程启动。

QGIS 的 ``native:writevectortiles_xyz`` **每个缩放级都会重新遍历全部要素**，
大图层 0-14 级能跑十几分钟。层级之间互相独立（各自写 ``{z}/`` 子目录），可以
拆分并行；但实测在**同一进程内**并发跑多个 processing 算法，大图层会原生崩溃
（0xC0000409 / segfault），所以这里统一用**独立子进程**执行，避免线程安全问题。

子进程入口见 :mod:`app.core.domain.tiles.worker`；父进程侧由
:class:`app.core.domain.tiles.task.CreateTileGroupTask` 负责拉起、轮询进度、
收尾。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import suppress
from itertools import pairwise
from pathlib import Path

from qgis.PyQt.QtCore import QThread

#: 最多同时跑几个子进程（子进程各自初始化 QGIS，内存 / CPU 开销都不小）
MAX_PARALLEL_TASKS = 4
#: 层级权重：8 级及以上单级耗时约为低层级的 N 倍（实测约 13 倍）
LOW_ZOOM_MAX = 7
LOW_ZOOM_WEIGHT = 1
HIGH_ZOOM_WEIGHT = 13


def parallel_worker_count(level_count: int) -> int:
    """按 CPU 核数 / 上限决定并行组数（至少 1 组）。"""
    cores = max(1, QThread.idealThreadCount())
    return max(1, min(level_count, MAX_PARALLEL_TASKS, max(1, cores - 1)))


def split_zoom_ranges(min_zoom: int, max_zoom: int) -> list[tuple[int, int]]:
    """按预计耗时权重把连续缩放级切成若干段，尽量让各段工作量接近。

    低层级单级耗时小、8 级起明显变重，不能按级数平均分；按累计权重取分位点
    切分，避免“一组全是 0-3 级、另一组全是 12-14 级”。
    """
    levels = list(range(min_zoom, max_zoom + 1))
    workers = parallel_worker_count(len(levels))
    if workers <= 1 or len(levels) <= 1:
        return [(min_zoom, max_zoom)]
    weights = [
        LOW_ZOOM_WEIGHT if z <= LOW_ZOOM_MAX else HIGH_ZOOM_WEIGHT for z in levels
    ]
    total = sum(weights)
    cumulative: list[int] = []
    acc = 0
    for weight in weights:
        acc += weight
        cumulative.append(acc)
    cuts = {0, len(levels)}
    for k in range(1, workers):
        target = total * k / workers
        idx = min(
            range(len(cumulative)),
            key=lambda i: abs(cumulative[i] - target),
        )
        cuts.add(min(len(levels), idx + 1))
    bounds = sorted(cuts)
    ranges = [
        (levels[start], levels[end - 1])
        for start, end in pairwise(bounds)
        if start < end
    ]
    return ranges or [(min_zoom, max_zoom)]


def create_work_dir() -> Path:
    """每次并行导出一个独立临时目录（job / progress / 日志都放这里）。"""
    return Path(tempfile.mkdtemp(prefix="qgis_vtiles_"))


def worker_command(job_file: Path) -> list[str]:
    """构造启动子进程的命令行（开发态 / PyInstaller 打包态通用）。"""
    if getattr(sys, "frozen", False):
        # 打包后 sys.executable 是应用自己的 exe；bootstrap 的
        # ``--vector-tile-worker`` 分支会转进 worker.main。
        return [sys.executable, "--vector-tile-worker", str(job_file)]
    # 用 ``-m`` 而不是脚本路径：脚本方式会把 tiles/ 放到 sys.path[0]，里面的
    # inspect.py 会遮蔽标准库 inspect（dataclasses 导入直接崩）。cwd 已设为
    # 工程根目录，PYTHONPATH 也带了它。
    return [
        sys.executable,
        "-u",
        "-m",
        "app.core.domain.tiles.worker",
        str(job_file),
    ]


def spawn_worker(job_file: Path, work_dir: Path) -> subprocess.Popen:
    """启动一个缩放段子进程；stdout/stderr 写进同目录日志。"""
    from app.infra.fs.paths import Paths

    env = os.environ.copy()
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
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_file = work_dir / f"{job_file.stem}.log"
    with log_file.open("wb") as out:
        return subprocess.Popen(
            worker_command(job_file),
            cwd=str(paths.project_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )


def kill_worker(proc: subprocess.Popen | None) -> None:
    """终止子进程（取消 / 失败收尾时用）。"""
    if proc is None or proc.poll() is not None:
        return
    with suppress(OSError):
        proc.kill()


__all__ = [
    "MAX_PARALLEL_TASKS",
    "create_work_dir",
    "kill_worker",
    "parallel_worker_count",
    "spawn_worker",
    "split_zoom_ranges",
]
