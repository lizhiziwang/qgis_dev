"""Meta SAM 子进程客户端（QProcess + 每行一个 JSON 的协议）。

设计要点：

- 常驻进程只启动一次，权重变更时重新发 ``load`` 即可，不重建进程；
- 同一时刻只允许一个推理请求（``set_image`` / ``predict`` / ``auto``），
  其它请求立即失败，避免队列越积越长；
- 请求带自增 id 与超时，超时视为进程卡死：直接 kill 并把状态置为错误；
- 进程 stdout **只**解析协议，stderr 保留尾部用于错误提示；
- 会话临时目录（候选 mask）在 stop/析构时清理，启动时清理 7 天前的残留。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from qgis.PyQt.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

from app.core.ai.sam_environment import clean_process_environment
from app.core.ai.sam_models import (
    infer_family,
    model_for_checkpoint,
    sam2_config_for_filename,
)
from app.infra.deep_learning import SamConfig
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger

logger = get_logger(__name__)

_STALE_SESSION_SECONDS = 7 * 24 * 3600
_STDERR_TAIL = 8192
_SHUTDOWN_TIMEOUT_MS = 2000

#: 回调签名：``(响应 dict 或 None, 错误信息或 None)``
Callback = Callable[[dict | None, str | None], None]


def default_sidecar_path() -> Path:
    """开发态取工程目录，冻结态取 PyInstaller 解包目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Paths.default().project_root
    return base / "app" / "core" / "ai" / "sam_sidecar.py"


def _cleanup_stale_dirs(root: Path) -> None:
    now = time.time()
    try:
        entries = list(root.glob("qgis_sam_*"))
    except OSError:
        return
    for item in entries:
        try:
            if item.is_dir() and now - item.stat().st_mtime > _STALE_SESSION_SECONDS:
                shutil.rmtree(item, ignore_errors=True)
        except OSError:
            continue


class SamClient(QObject):
    """SAM 子进程的 GUI 线程侧封装。"""

    status_changed = pyqtSignal(str)
    ready_changed = pyqtSignal(bool)
    error = pyqtSignal(str)
    progress = pyqtSignal(float)
    busy_changed = pyqtSignal(bool)

    def __init__(
        self,
        config: SamConfig,
        *,
        sidecar_path: str | Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._sidecar_path = Path(sidecar_path) if sidecar_path else default_sidecar_path()
        self._process: QProcess | None = None
        self._buffer = b""
        self._stderr_tail = ""
        self._pending: dict[int, dict] = {}
        self._next_id = 1
        self._exclusive: int | None = None
        self._status = "未启动"
        self._ready = False
        self._shutting_down = False
        #: 客户端主动 kill（超时 / 协议异常）后忽略随之而来的 crash/finished 信号，
        #: 否则用户会先看到「超时」再看到误导性的「子进程启动失败 / 已退出」。
        self._killing = False
        self._session_dir: Path | None = None
        self._load_callback: Callback | None = None

    # ------------------------------------------------------------------ 状态
    @property
    def config(self) -> SamConfig:
        return self._config

    @config.setter
    def config(self, value: SamConfig) -> None:
        self._config = value

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def is_busy(self) -> bool:
        return self._exclusive is not None

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    def _set_status(self, text: str) -> None:
        self._status = text
        self.status_changed.emit(text)

    def _set_ready(self, ready: bool) -> None:
        if self._ready != ready:
            self._ready = ready
            self.ready_changed.emit(ready)

    # ------------------------------------------------------------------ 校验
    def check_environment(self) -> str | None:
        """启动前检查 Python / 权重 / sidecar，返回错误文案。

        ``sam_src`` 可为空（改用 pip 安装的 ``segment_anything``），因此不再强制检查。
        """
        python = Path(self._config.python) if self._config.python else None
        if python is None or not python.is_file():
            return f"外部 Python 不存在：{self._config.python or '（未配置）'}"
        if not self._config.checkpoint or not Path(self._config.checkpoint).is_file():
            return "尚未选择 SAM 权重文件"
        if not self._sidecar_path.is_file():
            return f"缺少 SAM 子进程脚本：{self._sidecar_path}"
        return None

    # ------------------------------------------------------------------ 进程
    def start(self, callback: Callback | None = None) -> None:
        """启动子进程并加载模型；``callback`` 在 ready / 失败时调用。"""
        problem = self.check_environment()
        if problem:
            self._fail_all(problem)
            self._set_status(problem)
            self.error.emit(problem)
            if callback:
                callback(None, problem)
            return
        if self._process is not None and self._process.state() == QProcess.ProcessState.Running:
            self._send_load(callback)
            return
        self._process = None
        self._cleanup_session_dir()

        self._shutting_down = False
        self._killing = False
        self._stderr_tail = ""
        self._buffer = b""
        root = Path(tempfile.gettempdir())
        _cleanup_stale_dirs(root)
        try:
            self._session_dir = Path(tempfile.mkdtemp(prefix="qgis_sam_"))
        except OSError as exc:
            message = f"无法创建 SAM 临时目录：{exc}"
            self._set_status(message)
            self.error.emit(message)
            if callback:
                callback(None, message)
            return

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.setProgram(self._config.python)
        process.setArguments(
            [
                "-u",
                str(self._sidecar_path),
                "--sam-src",
                self._config.sam_src,
                "--out-dir",
                str(self._session_dir),
            ]
        )
        env = clean_process_environment(QProcessEnvironment.systemEnvironment())
        process.setProcessEnvironment(env)
        process.setWorkingDirectory(str(self._session_dir))
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_process_error)
        self._process = process
        self._load_callback = callback
        self._set_ready(False)
        self._set_status("正在启动 SAM 子进程…")
        process.start()

    def _send_load(self, callback: Callback | None = None) -> None:
        config = self._config
        entry = model_for_checkpoint(config.checkpoint, config.model_id)
        family = entry.family if entry else infer_family(config.checkpoint)
        sam2_config = config.sam2_config or (entry.config if entry else "")
        if not sam2_config:
            sam2_config = sam2_config_for_filename(config.checkpoint)
        model_type = entry.model_type if entry and entry.family == "sam1" else config.model_type
        payload = {
            "checkpoint": config.checkpoint,
            "model_type": model_type,
            "device": config.device,
            "family": family,
            "model_id": entry.id if entry else config.model_id,
            "sam2_config": sam2_config,
        }
        self._set_ready(False)
        self._set_status("正在加载 SAM 模型…")
        self._send(
            "load",
            payload,
            config.load_timeout_s * 1000,
            callback or (lambda _data, _err: None),
            busy=False,
            exclusive=False,
        )

    def stop(self) -> None:
        """关闭子进程并清理会话临时目录（窗口关闭时调用）。"""
        self._shutting_down = True
        process = self._process
        if process is not None:
            if process.state() != QProcess.ProcessState.NotRunning:
                with contextlib.suppress(Exception):  # 管道已断开时忽略
                    self._write({"cmd": "shutdown", "id": 0})
                if not process.waitForFinished(_SHUTDOWN_TIMEOUT_MS):
                    process.kill()
                    process.waitForFinished(1000)
            process.deleteLater()
        self._process = None
        self._fail_all("SAM 子进程已停止")
        self._set_ready(False)
        self._set_status("未启动")
        self._cleanup_session_dir()

    def _cleanup_session_dir(self) -> None:
        if self._session_dir is not None:
            shutil.rmtree(self._session_dir, ignore_errors=True)
            self._session_dir = None

    def _kill(self, message: str) -> None:
        process = self._process
        if process is not None:
            self._killing = True
            process.kill()
            process.waitForFinished(1000)
        self._fail_all(message)
        self._set_ready(False)
        self._set_status(message)

    # ------------------------------------------------------------------ 请求
    def _set_image_payload(self, path: str, token: str) -> dict:
        payload: dict = {"path": str(path), "token": token}
        config = self._config
        if config.cache_enabled and config.cache_dir:
            payload["cache_dir"] = config.cache_dir
            payload["cache_max_bytes"] = max(0, int(config.cache_max_mb)) * 1024 * 1024
        return payload

    def set_image(self, path: str, token: str, callback: Callback) -> None:
        self._send(
            "set_image",
            self._set_image_payload(path, token),
            self._config.set_image_timeout_s * 1000,
            callback,
        )

    def predict(
        self,
        *,
        points: list[list[float]],
        labels: list[int],
        box: list[float] | None,
        token: str,
        callback: Callback,
        multimask: bool = True,
    ) -> None:
        self._send(
            "predict",
            {
                "token": token,
                "points": points,
                "labels": labels,
                "box": box,
                "multimask": multimask,
            },
            self._config.predict_timeout_s * 1000,
            callback,
        )

    def auto(self, token: str, params: dict, callback: Callback) -> None:
        payload = dict(params)
        payload["token"] = token
        self._send("auto", payload, self._config.auto_timeout_s * 1000, callback)

    def text(self, token: str, text: str, threshold: float, callback: Callback) -> None:
        """SAM3 文字提示分割；结果形状与 ``predict`` / ``auto`` 一致。"""
        self._send(
            "text",
            {"token": token, "text": text, "threshold": float(threshold)},
            self._config.predict_timeout_s * 1000,
            callback,
        )

    def exemplar(
        self,
        token: str,
        box: list[float],
        threshold: float,
        callback: Callback,
    ) -> None:
        """SAM3 范例分割：``box`` 为原图像素 ``[x0, y0, x1, y1]``。"""
        self._send(
            "exemplar",
            {
                "token": token,
                "box": [float(value) for value in box],
                "threshold": float(threshold),
            },
            self._config.predict_timeout_s * 1000,
            callback,
        )

    def _send(
        self,
        command: str,
        payload: dict,
        timeout_ms: int,
        callback: Callback,
        *,
        busy: bool = True,
        exclusive: bool = True,
    ) -> None:
        process = self._process
        if process is None or process.state() != QProcess.ProcessState.Running:
            message = "SAM 子进程未启动"
            if callback:
                callback(None, message)
            return
        if exclusive and self._exclusive is not None:
            message = "SAM 正在处理上一个请求，请稍候"
            if callback:
                callback(None, message)
            return
        request_id = self._next_id
        self._next_id += 1
        entry = {
            "callback": callback,
            "command": command,
            "busy": busy,
            "exclusive": exclusive,
            "timer": QTimer(self),
        }
        entry["timer"].setSingleShot(True)
        entry["timer"].timeout.connect(lambda rid=request_id: self._on_timeout(rid))
        entry["timer"].start(max(1000, int(timeout_ms)))
        self._pending[request_id] = entry
        if busy:
            self._exclusive = request_id
            self.busy_changed.emit(True)
        try:
            self._write({"cmd": command, "id": request_id, **payload})
        except Exception as exc:  # noqa: BLE001
            self._resolve(request_id, None, f"写入 SAM 子进程失败：{exc}")
            self._kill(f"SAM 子进程通信失败：{exc}")
        else:
            logger.debug("SAM 请求 #%s %s", request_id, command)

    def _write(self, payload: dict) -> None:
        process = self._process
        if process is None:
            raise RuntimeError("SAM 子进程不存在")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        process.write(data)

    # ------------------------------------------------------------------ 响应
    def _resolve(self, request_id: int, data: dict | None, error: str | None) -> None:
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return
        entry["timer"].stop()
        entry["timer"].deleteLater()
        if entry["exclusive"] and self._exclusive == request_id:
            self._exclusive = None
            self.busy_changed.emit(False)
        callback = entry["callback"]
        if callback:
            try:
                callback(data, error)
            except Exception:
                logger.exception("SAM 回调异常 #%s", request_id)

    def _fail_all(self, message: str) -> None:
        for request_id in list(self._pending):
            self._resolve(request_id, None, message)
        self._exclusive = None
        self.busy_changed.emit(False)

    def _on_timeout(self, request_id: int) -> None:
        entry = self._pending.get(request_id)
        if entry is None:
            return
        command = entry["command"]
        message = f"SAM 请求超时（{command}），子进程已终止"
        self._resolve(request_id, None, message)
        self._fail_all(message)
        self._kill(message)
        self.error.emit(message)

    def _read_stdout(self) -> None:
        process = self._process
        if process is None:
            return
        chunk = bytes(process.readAllStandardOutput())
        if not chunk:
            return
        self._buffer += chunk
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            if not raw.strip():
                continue
            if len(raw) > self._config.max_line_bytes:
                message = "SAM 输出超长，已终止子进程"
                self._kill(message)
                self.error.emit(message)
                return
            try:
                message = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                logger.warning("忽略无法解析的 SAM 输出：%r", raw[:200])
                continue
            self._dispatch(message)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None:
            return
        text = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        if not text:
            return
        self._stderr_tail = (self._stderr_tail + text)[-_STDERR_TAIL:]
        logger.debug("SAM stderr: %s", text.rstrip())

    def _dispatch(self, message: dict) -> None:
        event = message.get("event")
        request_id = message.get("id")
        if event == "hello":
            self._set_status("SAM 子进程已就绪，正在加载模型…")
            self._send_load(getattr(self, "_load_callback", None))
            return
        if event == "status":
            text = message.get("message")
            if text:
                self._set_status(str(text))
            return
        if event == "progress":
            try:
                elapsed = float(message.get("elapsed") or 0.0)
            except (TypeError, ValueError):
                elapsed = 0.0
            self.progress.emit(elapsed)
            return
        if event == "ready":
            self._set_ready(True)
            device = message.get("device") or "?"
            family = str(message.get("family") or "").upper()
            model_label = message.get("model_id") or message.get("model_type") or "?"
            parts = [part for part in (family, str(model_label), str(device)) if part and part != "?"]
            detail = " / ".join(parts) if parts else "?"
            self._set_status(f"模型已就绪（{detail}）")
            self._resolve(int(request_id), message, None)
            return
        if event in ("image_ready", "result", "pong", "bye"):
            if request_id is not None:
                self._resolve(int(request_id), message, None)
            self._set_ready(True)
            return
        if event == "error":
            text = str(message.get("message") or "SAM 内部错误")
            error_type = message.get("error_type") or "internal"
            if error_type == "cuda_oom":
                text = f"显存不足，建议换更小的模型或 CPU：{text}"
            if request_id is None:
                self._fail_all(text)
                self._set_status(text)
                self.error.emit(text)
                return
            self._resolve(int(request_id), None, text)
            if message.get("fatal") or request_id == 0:
                self._fail_all(text)
            if event == "error" and error_type in ("checkpoint_missing", "import_error"):
                self._set_ready(False)
                self._set_status(text)
            self.error.emit(text)
            return
        logger.debug("未处理的 SAM 事件：%s", event)

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        if self._shutting_down or self._killing:
            return
        message = f"SAM 子进程启动失败：{self._process.errorString() if self._process else error}"
        self._fail_all(message)
        self._set_ready(False)
        self._set_status(message)
        self.error.emit(message)

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        if self._shutting_down or self._killing:
            return
        tail = self._stderr_tail.strip()
        message = f"SAM 子进程已退出（code={exit_code}）"
        if tail:
            message += f"\n{tail[-2000:]}"
        logger.warning("SAM 子进程退出：%s", message)
        self._process = None
        self._fail_all(message)
        self._set_ready(False)
        self._set_status("SAM 子进程已退出")
        self.error.emit(message)
