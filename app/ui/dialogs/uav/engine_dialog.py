"""拼接引擎（OpenSfM）设置与组件安装对话框。"""

from __future__ import annotations

import os
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from app.core.uav.engine import (
    ENV_ENGINE_MANIFEST,
    EngineInfo,
    download_and_install,
    fetch_manifest,
    install_from_zip,
    probe_engine,
)
from app.infra.logging import get_logger
from app.infra.uav import (
    load_engine_settings,
    resolve_engine,
    save_engine_settings,
    uav_engine_state_dir,
)
from app.ui.common.base_dialog import BaseDialog
from app.ui.dialogs.uav.worker import UavWorker

logger = get_logger(__name__)

_STYLE_OK = "color: #2e7d32;"
_STYLE_WARN = "color: #b8860b;"
_STYLE_ERROR = "color: #d9534f;"


class UavEngineDialog(BaseDialog):
    """查看/检测/安装拼接引擎组件。

    引擎是外部 Python 环境，不装进 QGIS。这里既可以指定已有环境，
    也可以从 manifest 下载官方打包组件，或从本地 zip 离线安装。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent, title="拼接引擎设置", show_max_button=False)
        self.setMinimumSize(820, 600)
        self.resize(900, 660)
        self.setModal(True)
        self._settings = load_engine_settings()
        self._worker: UavWorker | None = None
        self._engine: EngineInfo | None = None
        self._checked = False
        self._build_ui()
        self._load_values()
        self._auto_probe()

    # ------------------------------------------------------------ 构建

    def _build_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        intro = QLabel(
            "拼接引擎（OpenSfM）负责空三、密集点云、DSM 与正射影像计算。"
            "它运行在独立的 Python 环境中，不会修改 QGIS 自带的 Python。"
        )
        intro.setObjectName("PropHint")
        intro.setWordWrap(True)
        root.addWidget(intro)

        status_box = QGroupBox("引擎状态")
        status_layout = QVBoxLayout(status_box)
        self.status_label = QLabel("尚未检测")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        status_layout.addWidget(self.status_label)
        status_row = QHBoxLayout()
        self.detect_button = QPushButton("重新检测")
        self.detect_button.clicked.connect(self._auto_probe)
        self.open_state_button = QPushButton("打开组件目录")
        self.open_state_button.clicked.connect(self._open_state_dir)
        status_row.addWidget(self.detect_button)
        status_row.addWidget(self.open_state_button)
        status_row.addStretch(1)
        status_layout.addLayout(status_row)
        root.addWidget(status_box)

        env_box = QGroupBox("使用已有 OpenSfM 环境（高级）")
        env_form = QFormLayout(env_box)
        self.python_edit = QLineEdit()
        self.python_edit.setPlaceholderText("例如 D:/miniconda3/envs/opensfm/python.exe")
        browse_python = QPushButton("浏览…")
        browse_python.clicked.connect(self._choose_python)
        env_form.addRow("引擎 Python", _row(self.python_edit, browse_python))
        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText("留空则自动使用 Python 所在环境目录")
        browse_root = QPushButton("浏览…")
        browse_root.clicked.connect(self._choose_root)
        env_form.addRow("引擎根目录", _row(self.root_edit, browse_root))
        hint = QLabel(
            "已有环境里需要安装好 OpenSfM（能执行 python -c \"import opensfm\"）。"
            "普通用户建议直接下载下方组件。"
        )
        hint.setObjectName("PropHint")
        hint.setWordWrap(True)
        env_form.addRow("", hint)
        root.addWidget(env_box)

        component_box = QGroupBox("下载 / 离线安装组件")
        component_layout = QVBoxLayout(component_box)
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("组件信息地址"))
        self.manifest_edit = QLineEdit()
        self.manifest_edit.setPlaceholderText("https://…/engine.json，或本地 engine.json 路径")
        url_row.addWidget(self.manifest_edit, 1)
        component_layout.addLayout(url_row)
        buttons = QHBoxLayout()
        self.download_button = QPushButton("下载并安装")
        self.download_button.clicked.connect(self._download)
        self.local_button = QPushButton("从本地 zip 安装…")
        self.local_button.clicked.connect(self._install_local)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.local_button)
        buttons.addStretch(1)
        component_layout.addLayout(buttons)
        self.install_label = QLabel()
        self.install_label.setObjectName("PropHint")
        self.install_label.setWordWrap(True)
        self.install_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        component_layout.addWidget(self.install_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        component_layout.addWidget(self.progress)
        root.addWidget(component_box)

        notes = QLabel(
            "组件包含独立 Python 与 OpenSfM，首次下载约 1–3 GB。"
            "安装到本软件配置目录，可随时在「打开组件目录」中查看或删除。"
        )
        notes.setObjectName("PropHint")
        notes.setWordWrap(True)
        root.addWidget(notes)
        root.addStretch(1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.accept)
        bottom.addWidget(self.close_button)
        root.addLayout(bottom)
        self.setLayout(root)

    def _load_values(self) -> None:
        self.python_edit.setText(self._settings.get("python", ""))
        self.root_edit.setText(self._settings.get("root", ""))
        # 发布方可通过 QGIS_UAV_ENGINE_MANIFEST 预置组件地址；用户保存过设置则优先。
        default_manifest = os.environ.get(ENV_ENGINE_MANIFEST, "").strip()
        self.manifest_edit.setText(
            self._settings.get("manifest_url", "") or default_manifest
        )

    # ------------------------------------------------------------ 检测

    def _auto_probe(self) -> None:
        if self._worker is not None:
            return
        self._save_values()
        self._set_busy(True, "正在检测引擎…")
        self.status_label.setText("正在检测引擎…")
        self.status_label.setStyleSheet("")

        def task(progress, cancel):
            engine = resolve_engine(probe=False)
            if engine is None:
                return None
            return probe_engine(engine)

        self._run_worker(task, self._on_probe_finished, failure=self._on_probe_failed)

    def _on_probe_finished(self, engine: EngineInfo | None) -> None:
        self._set_busy(False)
        self._engine = engine
        self._checked = True
        if engine is None:
            self.status_label.setText(
                "✘ 未找到拼接引擎。请指定已有环境，或下载/导入引擎组件。"
            )
            self.status_label.setStyleSheet(_STYLE_ERROR)
            return
        if engine.available:
            self.status_label.setText(
                f"✔ {engine.label}可用\n"
                f"Python：{engine.python}\n"
                f"OpenSfM：{engine.opensfm_version or '未知版本'}"
            )
            self.status_label.setStyleSheet(_STYLE_OK)
        else:
            self.status_label.setText(f"✘ {engine.message}\nPython：{engine.python}")
            self.status_label.setStyleSheet(_STYLE_ERROR)

    def _on_probe_failed(self, message: str) -> None:
        self._set_busy(False)
        self.status_label.setText(f"✘ 检测失败：{message}")
        self.status_label.setStyleSheet(_STYLE_ERROR)

    # ------------------------------------------------------------ 安装

    def _download(self) -> None:
        self._save_values()
        url = self.manifest_edit.text().strip()
        if not url:
            QMessageBox.information(
                self,
                "下载组件",
                "请先填写组件信息地址（engine.json 的 URL），或使用「从本地 zip 安装」。",
            )
            return
        if self._worker is not None:
            return
        if not self._confirm_install():
            return
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._set_busy(True, "正在获取组件信息…")

        def task(progress, cancel):
            manifest = fetch_manifest(url)
            return download_and_install(
                manifest,
                state_dir=uav_engine_state_dir(),
                progress=progress,
                cancel=cancel,
            )

        self._run_worker(task, self._on_install_finished, failure=self._on_install_failed)

    def _install_local(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择引擎组件 zip", "", "Zip 压缩包 (*.zip)"
        )
        if not path:
            return
        if self._worker is not None:
            return
        if not self._confirm_install():
            return
        version = Path(path).stem
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._set_busy(True, "正在解压组件…")

        def task(progress, cancel):
            return install_from_zip(
                path,
                state_dir=uav_engine_state_dir(),
                version=version,
                progress=progress,
                cancel=cancel,
            )

        self._run_worker(task, self._on_install_finished, failure=self._on_install_failed)

    def _confirm_install(self) -> bool:
        answer = QMessageBox.question(
            self,
            "安装引擎组件",
            "安装会下载/解压较大的组件包，并替换当前组件版本。继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_install_finished(self, engine: EngineInfo) -> None:
        self._set_busy(False)
        self.progress.setValue(100)
        self.install_label.setText(f"✔ 组件已安装：{engine.root}")
        self.install_label.setStyleSheet(_STYLE_OK)
        # 安装完成后立即校验，避免 zip 不完整却显示成功
        self._settings["python"] = engine.python
        self._settings["root"] = engine.root
        self.python_edit.setText(engine.python)
        self.root_edit.setText(engine.root)
        self._save_values()
        self._auto_probe()

    def _on_install_failed(self, message: str) -> None:
        self._set_busy(False)
        self.progress.setVisible(False)
        if "取消" in message:
            self.install_label.setText("已取消安装")
            self.install_label.setStyleSheet(_STYLE_WARN)
            return
        self.install_label.setText(f"✘ 安装失败：{message}")
        self.install_label.setStyleSheet(_STYLE_ERROR)
        QMessageBox.warning(self, "安装引擎组件", message)

    # ------------------------------------------------------------ 后台

    def _run_worker(self, function, on_finished, *, failure=None) -> None:
        worker = UavWorker(function, self)
        self._worker = worker
        worker.progress.connect(self._on_progress)
        worker.finished.connect(
            lambda result: self._on_worker_finished(result, on_finished)
        )
        worker.failed.connect(failure or self._on_worker_failed)
        worker.start()

    def _on_progress(self, percent: int, message: str) -> None:
        self.progress.setValue(max(0, min(100, percent)))
        if message:
            self.install_label.setText(message)

    def _on_worker_finished(self, result, callback) -> None:
        self._worker = None
        callback(result)

    def _on_worker_failed(self, message: str) -> None:
        self._worker = None
        self._set_busy(False)
        self.install_label.setText(f"✘ {message}")
        self.install_label.setStyleSheet(_STYLE_ERROR)
        QMessageBox.warning(self, "拼接引擎", message)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        for widget in (
            self.detect_button,
            self.download_button,
            self.local_button,
            self.python_edit,
            self.root_edit,
            self.manifest_edit,
        ):
            widget.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        if message:
            self.install_label.setText(message)
            self.install_label.setStyleSheet("")

    # ------------------------------------------------------------ 文件

    def _choose_python(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择引擎 Python", "", "Python (python.exe python)"
        )
        if path:
            self.python_edit.setText(path)
            self.root_edit.clear()

    def _choose_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择引擎根目录")
        if path:
            self.root_edit.setText(path)

    def _open_state_dir(self) -> None:
        state = uav_engine_state_dir()
        state.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(state)))

    # ------------------------------------------------------------ 设置

    def _save_values(self) -> None:
        values = {
            "python": self.python_edit.text().strip(),
            "root": self.root_edit.text().strip(),
            "manifest_url": self.manifest_edit.text().strip(),
        }
        self._settings.update(values)
        try:
            save_engine_settings(values)
        except Exception:
            logger.exception("保存拼接引擎设置失败")


def _row(edit, *buttons):
    layout = QHBoxLayout()
    layout.addWidget(edit, 1)
    for button in buttons:
        layout.addWidget(button)
    return layout


__all__ = ["UavEngineDialog"]
