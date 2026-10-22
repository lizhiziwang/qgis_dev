"""图层属性「条件」页：用属性筛选方式设置图层显示条件。

界面与「属性表 → 属性筛选」一致（字段选择 / 值预览 / 操作符 / 条件编辑框），
复用 :class:`~app.ui.dialogs.query_builder_widget.QueryBuilderWidget`。输入的
条件会写进图层的 ``subsetString``（QGIS 的「定义查询」），画布与属性表只呈现
满足条件的要素；留空即显示全部。写回推迟到 :meth:`ConditionTab.apply`
（点「确定 / 应用」），点「取消」不会改动图层。
"""

from __future__ import annotations

from qgis.core import QgsVectorLayer
from qgis.PyQt.QtWidgets import QMessageBox, QVBoxLayout

from app.infra.logging import get_logger
from app.ui.dialogs.layer_properties.base import LayerPropertyTab
from app.ui.dialogs.query_builder_widget import QueryBuilderWidget

logger = get_logger(__name__)


class ConditionTab(LayerPropertyTab):
    """图层属性「条件」页。"""

    def __init__(self, layer=None, parent=None) -> None:
        super().__init__(parent)
        self._layer = layer
        self._build()
        self.load(layer)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        self.widget = QueryBuilderWidget(self._layer, self)
        root.addWidget(self.widget, 1)

    def _sync_enabled(self) -> None:
        is_vector = isinstance(self._layer, QgsVectorLayer)
        self.widget.setEnabled(is_vector)

    def _refresh_status(self) -> None:
        if not isinstance(self._layer, QgsVectorLayer):
            self.widget.set_applied_text("仅矢量图层可以设置显示条件。")
            return
        try:
            applied = self._layer.subsetString()
            count = self._layer.featureCount()
        except Exception:  # noqa: BLE001
            applied = ""
            count = -1
        count_text = "—" if count < 0 else str(count)
        hint = "修改后点「确定 / 应用」生效。"
        if applied:
            text = f"图层当前可见要素数：{count_text}（已应用条件）。{hint}"
        else:
            text = f"图层当前显示全部要素：{count_text}。{hint}"
        self.widget.set_applied_text(text)

    def load(self, layer) -> None:
        self._layer = layer
        self._sync_enabled()
        target = layer if isinstance(layer, QgsVectorLayer) else None
        if self.widget.layer is not target:
            self.widget.set_layer(target)
        if not isinstance(layer, QgsVectorLayer):
            self.widget.set_condition("")
            self._refresh_status()
            return
        try:
            condition = layer.subsetString()
        except Exception:  # noqa: BLE001
            condition = ""
        self.widget.set_condition(condition or "")
        self._refresh_status()

    def apply(self, layer) -> bool:
        if not isinstance(layer, QgsVectorLayer):
            return True

        condition = self.widget.condition()
        if condition:
            error = self.widget.condition_error(condition)
            if error is not None:
                QMessageBox.warning(self, "图层属性", error)
                return False

        try:
            current = layer.subsetString()
        except Exception:  # noqa: BLE001
            current = None
        if current == condition:
            self._refresh_status()
            return True

        if layer.isEditable():
            QMessageBox.warning(
                self,
                "图层属性",
                "图层处于编辑状态，无法应用显示条件。请先保存 / 停止编辑后再试。",
            )
            return False

        try:
            if not layer.setSubsetString(condition):
                QMessageBox.warning(
                    self,
                    "图层属性",
                    "图层提供者拒绝了该显示条件，条件未生效！",
                )
                return False
        except Exception as exc:
            logger.error("应用显示条件失败: %s", exc, exc_info=True)
            QMessageBox.warning(self, "图层属性", f"应用显示条件失败：{exc}")
            return False

        self._refresh_status()
        return True
