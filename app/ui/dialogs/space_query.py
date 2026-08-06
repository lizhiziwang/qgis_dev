"""空间查询 Dialog。

迁自 :class:`component.SpaceQuery.SpaceQuery`。
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from qgis.core import Qgis, QgsGeometry, QgsProject
from qgis.gui import QgsMapCanvas

from app.core.domain.query import QueryType, SpaceQueryService
from app.core.domain.drawing import DrawingService
from app.core.domain.drawing.tools import get_tool_class
from app.infra.logging import get_logger
from app.ui.common.base_dialog import BaseDialog
from app.ui.common.icons import Icons


logger = get_logger(__name__)


_QTYPES = {
    QueryType.POINT: "点查询",
    QueryType.LINE: "线查询",
    QueryType.POLYGON: "区域查询",
    QueryType.CIRCLE: "圆形查询",
    QueryType.RECTANGLE: "框线查询",
    QueryType.ALONG_LINE: "沿线查询",
    QueryType.BUFFER: "缓冲区查询",
}


class SpaceQueryDialog(BaseDialog):
    """空间查询 Dialog。"""

    def __init__(self, parent, q_type: str = "point", is_buffer: bool = False) -> None:
        label = _QTYPES.get(QueryType(q_type), "空间查询")
        super().__init__(parent, label)
        self.parent_window = parent
        self.layers = parent.project.mapLayers().values() if parent and hasattr(parent, "project") else []
        self.q_type = q_type
        self.search_geom: Optional[QgsGeometry] = None
        self.is_buffer = is_buffer
        self.buff_value: Optional[QLineEdit] = None
        self.space_query = SpaceQueryService()
        self.drawing = DrawingService()
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        try:
            from app.ui.common.widgets import open_file_dir
            from app.ui.dialogs.query_builder import QueryBuilderDialog  # noqa
        except Exception:  # noqa: BLE001
            pass

        # 图层选择
        from app.ui.docks.attribute_dock import PagedAttributeTableWidget  # noqa
        from PyQt5.QtWidgets import QComboBox, QHBoxLayout
        layer_layout = QHBoxLayout()
        layer_layout.addWidget(QLabel("查询图层:"))
        self.layer_input = QComboBox()
        self.layer_input.setMinimumWidth(180)
        for l in self.layers:
            self.layer_input.addItem(l.name())
        layer_layout.addWidget(self.layer_input)
        layout.addLayout(layer_layout)
        self.setMinimumSize(400, 300)

        # 绘制按钮
        op = QHBoxLayout()
        op.addWidget(QLabel("操作:"))
        draw_btn = QPushButton("绘制")
        draw_btn.clicked.connect(self._start_draw)
        op.addWidget(draw_btn)

        self.geom_txt = QTextEdit()
        self.geom_txt.setPlaceholderText("绘制几何的位置")
        self.geom_txt.setReadOnly(True)
        op.addWidget(self.geom_txt)
        layout.addLayout(op)

        # 缓冲距离（可选）
        if self.is_buffer:
            buf = QHBoxLayout()
            buf.addWidget(QLabel("缓冲距离:"))
            self.buff_value = QLineEdit("0.0")
            buf.addWidget(self.buff_value)
            buf.addWidget(QLabel("米(m)"))
            layout.addLayout(buf)

        layout.addWidget(self.geom_txt)

        # 按钮
        bb = QDialogButtonBox()
        bb.addButton("确认", QDialogButtonBox.AcceptRole)
        bb.addButton("取消", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self._on_query)
        bb.rejected.connect(self.close)
        layout.addWidget(bb)

    def _start_draw(self) -> None:
        self.hide()
        try:
            canvas = self.parent_window.canvas
            cls = get_tool_class(self.q_type)
            distance = float(self.buff_value.text()) if self.buff_value else 0.0
            tool = cls(canvas, draw_manager=None, is_buffer=self.is_buffer, distance=distance)
            canvas.setMapTool(tool)
            tool.finished.connect(self._on_geom_ready)
        except Exception as e:  # noqa: BLE001
            logger.error(e)

    def _on_geom_ready(self, geom: QgsGeometry) -> None:
        self.search_geom = geom
        self.geom_txt.setPlainText(geom.asWkt())
        self.show()

    def _on_query(self) -> None:
        try:
            if not self.layer_input.currentText():
                from app.infra.messagebar import send
                send("warning", "请选择图层进行查询", duration=3)
                return
            if self.search_geom is None:
                self.hide()
                if self.parent_window and hasattr(self.parent_window, "messageBar"):
                    self.parent_window.messageBar.pushMessage("请绘制几何图形用于查询", level=Qgis.Warning, duration=3)
                return
            layer = self.parent_window.project.mapLayersByName(self.layer_input.currentText())[0]
            fids = self.space_query.query_by_geometry(
                layer, self.search_geom, source_crs=self.parent_window.canvas.mapSettings().destinationCrs()
            )
            if self.parent_window and hasattr(self.parent_window, "_show_attribute_dock"):
                self.parent_window._show_attribute_dock(layer, show_ids=fids)
            if self.parent_window and hasattr(self.parent_window, "current_attribute_dock"):
                self.parent_window.current_attribute_dock.zoom_to_map("selectFeature", {"id": fids})
        except Exception as e:  # noqa: BLE001
            logger.error(e)
        finally:
            self.close()

    def closeEvent(self, event) -> None:
        try:
            if hasattr(self, "parent_window") and self.parent_window and hasattr(self.parent_window, "canvas"):
                self.parent_window.canvas.unsetMapTool(self.parent_window.canvas.mapTool())
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)
