"""推理子进程客户端与 checkpoint 探测。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from qgis.PyQt.QtCore import QProcess, QProcessEnvironment

from app.core.ai.sam_environment import clean_process_environment
from app.core.training.client import TrainingClient
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 一次推理批次（大窗口）可能远超训练的 15s，给足优雅退出的时间
INFERENCE_STOP_GRACE_MS = 180_000
#: ``--inspect`` 需要导入 torch，冷启动可能较慢
INSPECT_TIMEOUT_MS = 60_000


def inference_sidecar_path() -> Path:
    """开发态取工程目录，冻结态取 PyInstaller 解包目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Paths.default().project_root
    return base / "app" / "core" / "inference" / "infer_sidecar.py"


class InferenceClient(TrainingClient):
    """推理子进程（复用训练客户端的 JSON 分帧 / stop.request）。"""

    def __init__(
        self,
        *,
        python: str = "",
        sidecar_path: str | Path | None = None,
        parent=None,
    ) -> None:
        super().__init__(
            python=python,
            sidecar_path=sidecar_path or inference_sidecar_path(),
            label="推理",
            stop_hint="（等待当前窗口完成）",
            stop_grace_ms=INFERENCE_STOP_GRACE_MS,
            parent=parent,
        )


def inspect_checkpoint(
    python: str,
    checkpoint: str | Path,
    *,
    sidecar_path: str | Path | None = None,
    timeout_ms: int = INSPECT_TIMEOUT_MS,
) -> dict:
    """同步读取 checkpoint 元数据（``--inspect``）。

    选中 run 之外的 ``.pt`` 时调用；失败抛 ``RuntimeError``，由页面提示，
    不阻止用户继续手动填写参数。
    """
    checkpoint_path = Path(checkpoint)
    if not checkpoint_path.is_file():
        raise RuntimeError(f"权重文件不存在：{checkpoint_path}")
    if not python or not Path(python).is_file():
        raise RuntimeError(f"推理 Python 不存在：{python or '（未配置）'}")
    sidecar = Path(sidecar_path) if sidecar_path else inference_sidecar_path()
    if not sidecar.is_file():
        raise RuntimeError(f"缺少推理子进程脚本：{sidecar}")

    process = QProcess()
    process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
    process.setProgram(python)
    process.setArguments(["-u", str(sidecar), "--inspect", str(checkpoint_path)])
    process.setProcessEnvironment(
        clean_process_environment(QProcessEnvironment.systemEnvironment())
    )
    process.setWorkingDirectory(str(checkpoint_path.parent))
    process.start()
    try:
        if not process.waitForStarted(10_000):
            raise RuntimeError(f"无法启动推理子进程：{process.errorString()}")
        if not process.waitForFinished(int(timeout_ms)):
            process.kill()
            process.waitForFinished(2000)
            raise RuntimeError("读取权重信息超时（模型可能过大或环境启动缓慢）")
        stdout = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        stderr = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        exit_code = int(process.exitCode())
    finally:
        process.deleteLater()

    info: dict | None = None
    error = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = str(message.get("event") or "")
        if event == "inspect" and isinstance(message.get("checkpoint"), dict):
            info = message["checkpoint"]
        elif event == "error":
            error = str(message.get("message") or "")
    if info is None:
        tail = stderr.strip()
        detail = error or (tail[-3000:] if tail else f"退出码 {exit_code}")
        logger.warning("读取权重信息失败 %s：%s", checkpoint_path, detail)
        raise RuntimeError(f"无法读取权重信息：{detail}")
    return info


__all__ = [
    "INFERENCE_STOP_GRACE_MS",
    "INSPECT_TIMEOUT_MS",
    "InferenceClient",
    "inference_sidecar_path",
    "inspect_checkpoint",
]
