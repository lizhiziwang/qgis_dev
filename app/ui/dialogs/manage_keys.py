"""「管理 Key」Dialog：在线底图 Dock 右键集中管理各底图服务商的 API key。

按 **provider 集中管理**（改一处全站生效）：
- 天地图 ``tk`` —— **真实生效**：所有天地图 URL（内置底图 + 模板）用 ``{tk}``
  占位符引用 ``map.keys.tianditu_tk``，构建 QGIS 图层时才替换（见 basemap 模块
  :func:`apply_key_placeholders`）。
- 高德 ``key`` —— **仅预留字段**：高德当前走社区公开地址、URL 未引用
  ``{gaode_key}``，本轮填了不影响加载（等正式 key / 鉴权地址到手再接入）。

只依赖 ``qgis.PyQt`` + :class:`MapKeysConfig`，不做 config I/O（读写由 MainWindow
经 ``set_map_keys`` 文本写回落盘，本模块只收集字段并组装 :class:`MapKeysConfig`）。
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app.env.config import MapKeysConfig
from app.ui.common.base_dialog import BaseDialog

_TIANDITU_HINT = (
    "换 key 后所有天地图 URL（含内置底图）引用新 tk；已加载底图需重载/重启生效"
)
_GAODE_HINT = "高德当前用公开地址、无需 key；此字段仅预留，本轮修改不影响加载"


def _hint_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color: #888;")
    return lab


class ManageKeysDialog(BaseDialog):
    """管理各底图服务商 API Key 的对话框。"""

    def __init__(
        self, parent: Optional[QWidget] = None, keys: Optional[MapKeysConfig] = None
    ) -> None:
        super().__init__(parent, title="管理地图服务 Key")
        # 缩小 BaseDialog 默认 800×600 的最小尺寸到紧凑表单
        self.setMinimumSize(0, 0)
        self.resize(520, 260)
        self.setModal(True)
        self._build_ui(keys or MapKeysConfig())

    # ============================================================== UI

    def _build_ui(self, keys: MapKeysConfig) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(6)

        head = QLabel("各底图服务商的 API Key（按服务商集中管理，改一处全站生效）")
        head.setWordWrap(True)
        layout.addWidget(head)

        # ---- 天地图 ----
        layout.addWidget(self._provider_label("天地图"))
        self.tianditu_edit = QLineEdit(keys.tianditu_tk)
        self.tianditu_edit.setPlaceholderText("天地图浏览器端 tk")
        layout.addWidget(self.tianditu_edit)
        layout.addWidget(_hint_label(_TIANDITU_HINT))

        layout.addSpacing(6)

        # ---- 高德 ----
        layout.addWidget(self._provider_label("高德"))
        self.gaode_edit = QLineEdit(keys.gaode_key)
        self.gaode_edit.setPlaceholderText("高德 Web 服务 key（预留）")
        layout.addWidget(self.gaode_edit)
        layout.addWidget(_hint_label(_GAODE_HINT))

        layout.addStretch(1)

        # ---- 按钮（Ok / Cancel）----
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.setLayout(layout)

    @staticmethod
    def _provider_label(text: str) -> QLabel:
        lab = QLabel(text)
        f = QFont(lab.font())
        f.setBold(True)
        lab.setFont(f)
        return lab

    # ============================================================== 取值

    def keys(self) -> MapKeysConfig:
        """按当前控件状态组装 :class:`MapKeysConfig`（strip 后；空串原样保留）。"""
        return MapKeysConfig(
            tianditu_tk=self.tianditu_edit.text().strip(),
            gaode_key=self.gaode_edit.text().strip(),
        )
