"""点击识别工具。

迁自 :class:`component.map_tool.FindFeatureTool`，业务逻辑（识别 + 高亮）
走 :class:`app.core.domain.identify.FeatureIdentifyService`。
"""

from __future__ import annotations

from typing import Optional

from qgis.gui import QgsMapCanvas, QgsMapToolIdentify

from app.core.domain.identify import FeatureIdentifyService, IdentifyResult


class FindFeatureTool(QgsMapToolIdentify):
    """点击识别工具。"""

    def __init__(self, canvas: QgsMapCanvas, parent: Optional[object] = None) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.identify_service = FeatureIdentifyService()
        self.parent_window = parent
        self.highlight = None
        self.last_dialog = None

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 —— QGIS 命名
        # Qt6 下 QgsMapMouseEvent 不再有 x()/y()，用 pixelPoint() 取画布像素坐标
        point = event.pixelPoint()
        results = self.identify(point.x(), point.y(), self.IdentifyMode.TopDownAll)
        if not results:
            return
        from app.core.domain.identify.service import IdentifyResult as _Result

        r = results[0]
        result = IdentifyResult(
            layer=r.mLayer,
            feature=r.mFeature,
        )
        self._show_highlight(result)
        self._show_info(result)

    def _show_highlight(self, result: IdentifyResult) -> None:
        from PyQt6.QtGui import QColor
        if self.highlight is not None:
            self.canvas.scene().removeItem(self.highlight)
        self.highlight = self.identify_service.highlight(self.canvas, result.layer, result.feature)

    def _show_info(self, result: IdentifyResult) -> None:
        # 优先：把要素详情显示到主窗口的右侧 Dock（不再弹模态框）
        pw = self.parent_window
        if pw is not None and hasattr(pw, "show_feature_info"):
            pw.show_feature_info(result, highlight=self.highlight)
            return
        # 回退：没有主窗口时仍用旧弹窗（保持工具可独立使用）
        from app.ui.dialogs.feature_info import FeatureInfoDialog
        if self.last_dialog is not None:
            self.last_dialog.close()
        dlg = FeatureInfoDialog(self.parent_window, result, highlight=self.highlight)
        dlg.show()
        self.last_dialog = dlg

    def deactivate(self) -> None:
        if self.highlight is not None:
            self.canvas.scene().removeItem(self.highlight)
            self.highlight = None
        super().deactivate()
