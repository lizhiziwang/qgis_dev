"""“发现新版本”对话框。

只负责展示与收集用户选择，网络 / 下载 / 安装都在
:class:`app.ui.update.controller.UpdateController` 的后台线程里执行。

按钮语义：

- 强制更新（``mandatory``）：没有“跳过此版本”，只有“稍后提醒”；关闭窗口也
  只是稍后，**不阻断程序使用**，下次启动 / 周期检查继续提示。
- 普通更新：可“跳过此版本”（写本地状态，不再自动弹）。
- 已下载待安装：直接“立即安装”（离线可用）。
- 自动安装不可用（源码运行 / Linux 无提权更新器）：退化为“打开下载页” /
  “打开所在文件夹”。
"""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from app import __version__
from app.ui.common.base_dialog import BaseDialog
from app.update.service import UpdateOutcome

_BUTTON_MIN_WIDTH = 96


class UpdateDialog(BaseDialog):
    """更新提示 / 下载进度 / 手工安装入口。"""

    install_requested = pyqtSignal()
    skip_requested = pyqtSignal()
    remind_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    open_download_requested = pyqtSignal()
    open_folder_requested = pyqtSignal()

    def __init__(self, parent=None, outcome: UpdateOutcome | None = None) -> None:
        super().__init__(parent, "检查更新", show_min_button=False, show_max_button=False)
        self.setMinimumSize(660, 460)
        self.resize(680, 500)
        self._outcome = outcome
        self._downloading = False
        self._manual: Path | None = None
        self._folder: Path | None = None
        self._init_ui()
        if outcome is not None:
            self.set_outcome(outcome)

    # ------------------------------------------------------------------ UI

    def _init_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)

        self._title_label = QLabel("发现新版本")
        self._title_label.setObjectName("updateTitle")
        self._title_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(self._title_label)

        self._version_label = QLabel("")
        self._version_label.setStyleSheet("color: #6b7280;")
        layout.addWidget(self._version_label)

        self._mandatory_label = QLabel("")
        self._mandatory_label.setWordWrap(True)
        self._mandatory_label.setStyleSheet(
            "color: #ad4e00; background: #fff7e6; border: 1px solid #ffd591;"
            "padding: 8px; border-radius: 4px;"
        )
        self._mandatory_label.hide()
        layout.addWidget(self._mandatory_label)

        notes_title = QLabel("更新内容")
        notes_title.setStyleSheet("font-weight: 600; color: #374151;")
        layout.addWidget(notes_title)
        self._notes = QTextBrowser()
        self._notes.setReadOnly(True)
        self._notes.setOpenExternalLinks(False)
        self._notes.setStyleSheet(
            "QTextBrowser { background: #fafbfc; border: 1px solid #e5e7eb;"
            "border-radius: 4px; padding: 6px; }"
        )
        self._notes.setMinimumHeight(150)
        layout.addWidget(self._notes, 1)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #4b5563;")
        self._status_label.hide()
        layout.addWidget(self._status_label)

        self._progress = QProgressBar()
        self._progress.setTextVisible(True)
        self._progress.hide()
        layout.addWidget(self._progress)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._skip_btn = _button("跳过此版本")
        self._remind_btn = _button("稍后提醒")
        self._open_download_btn = _button("打开下载页")
        self._open_folder_btn = _button("打开所在文件夹")
        self._cancel_btn = _button("取消下载")
        self._install_btn = _button("立即更新", primary=True)
        for btn in (
            self._skip_btn,
            self._remind_btn,
            self._open_download_btn,
            self._open_folder_btn,
            self._cancel_btn,
            self._install_btn,
        ):
            buttons.addWidget(btn)
        layout.addLayout(buttons)
        self.setLayout(layout)

        self._skip_btn.clicked.connect(self._on_skip)
        self._remind_btn.clicked.connect(self._on_remind)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._install_btn.clicked.connect(self.install_requested.emit)
        self._open_download_btn.clicked.connect(self.open_download_requested.emit)
        self._open_folder_btn.clicked.connect(self._on_open_folder)
        for btn in (self._open_download_btn, self._open_folder_btn, self._cancel_btn):
            btn.hide()

    # ------------------------------------------------------------------ 状态

    @property
    def outcome(self) -> UpdateOutcome | None:
        """当前展示的检查结论（控制器每次动作前读取，避免闭包捕获旧结果）。"""
        return self._outcome

    @property
    def folder_path(self) -> Path | None:
        """“打开所在文件夹”应打开的目录（安装包目录 / 日志目录）。"""
        return self._folder

    def set_outcome(self, outcome: UpdateOutcome) -> None:
        self._outcome = outcome
        self._downloading = False
        self._manual = None
        self._folder = None
        manifest = outcome.result.manifest
        title = "发现新版本" if not outcome.pending else "新版本已下载"
        self._title_label.setText(f"{title} {manifest.version}")
        self._version_label.setText(
            f"当前版本 {__version__}（{outcome.result.manifest.channel} 通道）"
            f"    →    {manifest.version}"
        )
        self._notes.setPlainText(manifest.release_notes.strip() or "（本次更新没有填写说明）")
        self._status_label.setStyleSheet("color: #4b5563;")
        self._progress.hide()
        self._open_download_btn.hide()
        self._open_folder_btn.hide()
        if outcome.mandatory:
            self._mandatory_label.setText(
                "此版本为强制提示更新：可以稍后处理，但程序会持续提醒。"
                "（不会限制当前版本的使用）"
            )
            self._mandatory_label.show()
            self._skip_btn.hide()
            self._remind_btn.setText("稍后提醒")
        else:
            self._mandatory_label.hide()
            self._skip_btn.setVisible(bool(outcome.update_available and not outcome.pending))
            self._remind_btn.setText("稍后提醒")
        self._install_btn.setText("立即安装" if outcome.pending else "立即更新")
        self._install_btn.setEnabled(True)
        self._install_btn.show()
        self._remind_btn.show()
        self._cancel_btn.hide()
        if outcome.pending:
            self._status_label.setText("安装包已下载并通过校验，可以立即安装。")
            self._status_label.show()
        elif outcome.package is not None:
            self._status_label.setText(
                f"安装包：{outcome.package.file_name}（{_human_size(outcome.package.size_bytes)}）"
                + ("" if outcome.update_available else "，当前已是最新版本")
            )
            self._status_label.show()
        else:
            self._status_label.setText("当前平台没有匹配的安装包。")
            self._status_label.show()

    def set_downloading(self) -> None:
        self._downloading = True
        self._progress.setRange(0, 1000)
        self._progress.setValue(0)
        self._progress.show()
        self._status_label.setStyleSheet("color: #4b5563;")
        self._status_label.setText("正在下载安装包…")
        self._status_label.show()
        self._install_btn.setEnabled(False)
        self._skip_btn.setEnabled(False)
        self._remind_btn.setEnabled(False)
        self._open_download_btn.hide()
        self._open_folder_btn.hide()
        self._cancel_btn.show()

    def set_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self._progress.setRange(0, total)
        self._progress.setValue(min(done, total))
        percent = int(done * 100 / total)
        self._status_label.setText(
            f"正在下载安装包… {_human_size(done)} / {_human_size(total)}（{percent}%）"
        )

    def set_download_error(self, message: str) -> None:
        self._downloading = False
        self._progress.hide()
        self._cancel_btn.hide()
        self._install_btn.setEnabled(True)
        self._skip_btn.setEnabled(True)
        self._remind_btn.setEnabled(True)
        self._status_label.setText(f"下载失败：{message}")
        self._status_label.setStyleSheet("color: #cf1322;")

    def set_download_cancelled(self) -> None:
        """取消下载后恢复按钮（已下载的分片保留，下次继续）。"""
        if self._outcome is None:
            return
        self.set_outcome(self._outcome)
        self._status_label.setText("已取消下载，可稍后继续（已下载部分会保留）。")

    def set_installing(self) -> None:
        self._downloading = False
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self._progress.show()
        self._status_label.setStyleSheet("color: #237804;")
        self._status_label.setText("正在启动更新程序，程序即将自动退出…")
        self._status_label.show()
        self._install_btn.hide()
        self._skip_btn.hide()
        self._remind_btn.hide()
        self._cancel_btn.hide()

    def set_starting_updater(self) -> None:
        """更新器进程已创建，正在等它写出启动心跳（还没到退出的时候）。"""
        self._downloading = False
        self._progress.setRange(0, 0)  # 忙碌动画，进度未知
        self._progress.show()
        self._status_label.setStyleSheet("color: #4b5563;")
        self._status_label.setText("正在启动更新程序…")
        self._status_label.show()
        self._install_btn.hide()
        self._skip_btn.hide()
        self._remind_btn.hide()
        self._cancel_btn.hide()

    def set_install_blocked(self, reason: str, log_path: Path | None) -> None:
        """更新器没能确认启动（例如被安全软件拦截）：程序不退出。

        此时更新器可能仍在后台等本进程退出，不提供“立即更新”按钮，避免
        启动第二个更新器并发安装。
        """
        self._downloading = False
        self._progress.hide()
        self._manual = None
        self._folder = log_path.parent if log_path is not None else None
        self._install_btn.hide()
        self._skip_btn.hide()
        self._remind_btn.setEnabled(True)
        self._remind_btn.show()
        self._cancel_btn.hide()
        self._open_download_btn.hide()
        self._open_folder_btn.setVisible(self._folder is not None)
        detail = f"（日志：{log_path}）" if log_path is not None else ""
        self._status_label.setStyleSheet("color: #cf1322;")
        self._status_label.setText(
            f"{reason}，本次不会自动更新，程序保持运行。"
            f"可能是安全软件拦截了更新程序{detail}。"
            "可关闭程序后重新打开再试，或手动运行安装包。"
        )
        self._status_label.show()

    def set_install_deferred(self, reason: str) -> None:
        """更新器已启动但用户取消了退出程序。

        不再显示“立即更新”：更新器仍在等旧进程退出，再启动一个会并发安装。
        用户可关闭对话框继续工作，下次启动程序会重新检查（更新器等待超时后
        自行放弃，不会修改任何文件）。
        """
        self._downloading = False
        self._progress.hide()
        self._install_btn.hide()
        self._skip_btn.hide()
        self._remind_btn.setEnabled(True)
        self._remind_btn.show()
        self._cancel_btn.hide()
        self._status_label.setStyleSheet("color: #ad4e00;")
        self._status_label.setText(reason)
        self._status_label.show()

    def set_manual(self, reason: str, package_path: Path | None) -> None:
        """自动安装不可用：展示原因，提供下载页 / 所在文件夹。"""
        self._downloading = False
        self._manual = package_path
        self._folder = package_path
        self._progress.hide()
        self._install_btn.hide()
        self._skip_btn.setEnabled(True)
        self._remind_btn.setEnabled(True)
        self._cancel_btn.hide()
        self._open_download_btn.show()
        if package_path is not None:
            self._open_folder_btn.show()
        self._status_label.setStyleSheet("color: #ad4e00;")
        self._status_label.setText(reason)
        self._status_label.show()

    # ------------------------------------------------------------------ 事件

    def _on_skip(self) -> None:
        self.skip_requested.emit()
        self.close()

    def _on_remind(self) -> None:
        self.remind_requested.emit()
        self.close()

    def _on_cancel(self) -> None:
        self.cancel_requested.emit()

    def _on_open_folder(self) -> None:
        if self._folder is not None:
            self.open_folder_requested.emit()

    def reject(self) -> None:
        # 关闭窗口 = 稍后提醒（不能把强制更新变成“跳过”）
        if self._downloading:
            # 下载中关窗：先取消，分片保留可续传
            self.cancel_requested.emit()
        super().reject()

    def closeEvent(self, event):
        if self._downloading:
            self.cancel_requested.emit()
        super().closeEvent(event)


def _button(text: str, *, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setMinimumWidth(_BUTTON_MIN_WIDTH)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        button.setStyleSheet(
            "QPushButton { background: #1677ff; color: white; border: none;"
            "padding: 6px 16px; border-radius: 3px; }"
            "QPushButton:hover { background: #4096ff; }"
            "QPushButton:disabled { background: #bfbfbf; }"
        )
    return button


def _human_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


__all__ = ["UpdateDialog"]
