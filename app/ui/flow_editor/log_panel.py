"""流程编排：底部运行日志 dock。

逐行记录节点执行时间 / 输入输出摘要 / 成败信息，按级别着色
（成功绿 / 失败红 / 跳过灰 / 提示蓝），并同步写应用日志。
"""

from __future__ import annotations

import html
import time

from qgis.PyQt.QtWidgets import QDockWidget, QPlainTextEdit

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 级别 → 日志行前景色
_LEVEL_COLORS = {
    "info": "#37474f",
    "success": "#2e7d32",
    "error": "#c62828",
    "warn": "#ef6c00",
    "skip": "#757575",
}


class LogDock(QDockWidget):
    """运行日志 dock。"""

    def __init__(self, parent=None) -> None:
        super().__init__("运行日志", parent)
        self.setObjectName("FlowLogDock")
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self._edit = QPlainTextEdit()
        self._edit.setReadOnly(True)
        self._edit.setMaximumBlockCount(2000)  # 防止长时间运行撑爆内存
        font = self._edit.font()
        font.setFamily("Consolas")
        font.setPointSize(9)
        self._edit.setFont(font)
        self.setWidget(self._edit)

    def log(self, text: str, level: str = "info") -> None:
        """追加一行日志。"""
        color = _LEVEL_COLORS.get(level, _LEVEL_COLORS["info"])
        stamp = time.strftime("%H:%M:%S")
        line = f'<span style="color:{color}">[{stamp}] {html.escape(text)}</span>'
        self._edit.appendHtml(line)
        if level == "error":
            logger.error("%s", text)
        elif level == "warn":
            logger.warning("%s", text)
        else:
            logger.info("%s", text)

    def clear(self) -> None:
        self._edit.clear()
