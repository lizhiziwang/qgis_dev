"""在画布上高亮闪烁某个要素，用于对话框里「点一个要素 → 地图上定位到它」。

「补缝」「合并」这类对话框都要求用户从一组要素里指定一个（目标面 / 要并入的
图斑），光看列表里的编号在密集数据里找不到是哪一个，所以选中时把该要素画到
画布上闪几下定位。数据集再大，闪几下也能让人看到它是哪一块。

用法::

    hl = FeatureHighlighter(canvas, layer, parent=dialog)
    hl.flash(fid)   # 画到高亮带上闪几下，闪完保持可见
    hl.clear()      # 隐藏（实例保留，可继续 flash 别的要素）
    hl.close()      # 收尾：停定时器 + 清高亮带 + 重绘画布

默认「闪完保持可见」是给「补缝 / 合并」那类**要对照着挑要素**的对话框用的。
只想闪一下说明「是哪一个」的场景接 :attr:`FeatureHighlighter.finished`，在回调里
自己 ``clear()`` 收掉（默认行为不受影响）。

``canvas`` 为 None（无画布调用场景 / 测试）时全部方法退化为无操作。
"""

from __future__ import annotations

from typing import Optional

from qgis.core import QgsVectorLayer
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor

#: 闪烁间隔（毫秒）与总切换次数（偶数次 → 结束时高亮带可见）
_FLASH_INTERVAL_MS = 160
_FLASH_TOGGLES = 6

#: 高亮带颜色（洋红，与选中集的黄色区分开）
_FLASH_COLOR = (255, 0, 220)


class FeatureHighlighter(QObject):
    """把要素画到临时橡皮筋上并闪烁几下；闪完保持可见便于对照。"""

    #: 一次闪烁跑完（高亮带已按默认行为留在可见态）。调用方可以据此再收掉它——
    #: 例如「选择集」面板只要闪一下说明「是哪一个」，不需要它一直亮着对照。
    finished = pyqtSignal()

    def __init__(self, canvas, layer: QgsVectorLayer, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._canvas = canvas
        self._layer = layer
        self._band: Optional[QgsRubberBand] = None
        self._timer: Optional[QTimer] = None
        self._left = 0

    # ------------------------------------------------------------ 对外

    def flash(self, fid: int) -> None:
        """把 ``fid`` 画到高亮带上并闪烁几下，闪完保持可见。"""
        band = self._ensure_band()
        if band is None:
            return
        try:
            feature = self._layer.getFeature(int(fid))
            geometry = feature.geometry() if feature.isValid() else None
            if geometry is None or geometry.isNull() or geometry.isEmpty():
                # 几何失效 / 已删除：留着上一次的高亮会指错要素
                self.clear()
                return
            band.setToGeometry(geometry, self._layer)
            band.show()
        except RuntimeError:
            return  # 图层 / 画布已销毁

        self._left = _FLASH_TOGGLES
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._on_tick)
        self._timer.start(_FLASH_INTERVAL_MS)

    def clear(self) -> None:
        """停掉闪烁并隐藏高亮带（实例保留，可继续用）。"""
        self._stop_flash()
        if self._band is not None:
            try:
                self._band.hide()
            except RuntimeError:
                pass

    def close(self) -> None:
        """收尾：停定时器、清掉高亮带并重绘画布（对话框关闭时调）。"""
        self._stop_flash()
        if self._band is not None:
            try:
                self._band.reset()
            except RuntimeError:
                pass
            self._band = None
        if self._canvas is not None:
            try:
                # 高亮带已清，重绘一次把残留清掉
                self._canvas.refresh()
            except RuntimeError:
                pass

    # ------------------------------------------------------------ 内部

    def _ensure_band(self) -> Optional[QgsRubberBand]:
        if self._canvas is None:
            return None
        if self._band is None:
            try:
                band = QgsRubberBand(self._canvas, self._layer.geometryType())
                band.setColor(QColor(*_FLASH_COLOR))
                band.setWidth(3)
                band.setFillColor(QColor(*_FLASH_COLOR, 70))
                self._band = band
            except RuntimeError:  # 画布已销毁
                return None
        return self._band

    def _on_tick(self) -> None:
        self._left -= 1
        if self._band is not None:
            try:
                if self._band.isVisible():
                    self._band.hide()
                else:
                    self._band.show()
            except RuntimeError:
                pass
        if self._left <= 0:
            self._stop_flash()
            if self._band is not None:
                try:
                    self._band.show()  # 闪完保持高亮，便于对照
                except RuntimeError:
                    pass
            self.finished.emit()

    def _stop_flash(self) -> None:
        if self._timer is not None:
            self._timer.stop()


__all__ = ["FeatureHighlighter"]
