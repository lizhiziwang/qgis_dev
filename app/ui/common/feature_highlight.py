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

from collections.abc import Callable
from typing import Optional

from qgis.core import QgsVectorLayer
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor

from app.ui.common.selection_style import (HighlightStyle, kind_of_geometry_type,
                                           style_band)

#: 闪烁间隔（毫秒）与总切换次数（偶数次 → 结束时高亮带可见）
_FLASH_INTERVAL_MS = 160
_FLASH_TOGGLES = 6

#: 默认高亮带颜色（洋红，与选中集的黄色区分开）。传了 ``style`` 时改用那套样式。
_FLASH_COLOR = (255, 0, 220)


class FeatureHighlighter(QObject):
    """把要素画到临时橡皮筋上并闪烁几下；闪完保持可见便于对照。"""

    #: 一次闪烁跑完（高亮带已按默认行为留在可见态）。调用方可以据此再收掉它——
    #: 例如「选择集」面板只要闪一下说明「是哪一个」，不需要它一直亮着对照。
    finished = pyqtSignal()

    def __init__(self, canvas, layer: QgsVectorLayer, parent: Optional[QObject] = None,
                 style_provider: Optional[Callable[[], HighlightStyle]] = None) -> None:
        """
        :param style_provider: 不给 = 用洋红闪烁色（默认，与选中黄区分、够醒目）；
            给了 = 每次画之前**问它要当前样式**（属性表单击行就是这种用法：样式是系统级
            配置，用户随时可能在「选择」页改，快照一份会过期）。
        """
        super().__init__(parent)
        self._canvas = canvas
        self._layer = layer
        self._style_provider = style_provider
        self._band: Optional[QgsRubberBand] = None
        self._timer: Optional[QTimer] = None
        self._left = 0

    # ------------------------------------------------------------ 对外

    def flash(self, fid: int) -> None:
        """把 ``fid`` 画到高亮带上并闪烁几下，闪完保持可见。"""
        if not self._draw(fid):
            return
        self._left = _FLASH_TOGGLES
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._on_tick)
        self._timer.start(_FLASH_INTERVAL_MS)

    def show(self, fid: int) -> None:
        """把 ``fid`` 画到高亮带上并**保持可见**（不闪烁）。"""
        self._stop_flash()
        self._draw(fid)

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

    def _draw(self, fid: int) -> bool:
        """把要素几何设进高亮带并显示；画上了返回 ``True``。

        几何失效 / 已删除时清掉并返回 ``False`` —— 留着上一次的高亮会指错要素。
        """
        band = self._ensure_band()
        if band is None:
            return False
        # 每次画之前重新上色：高亮样式是系统级配置，用户随时可能改（见「选择」页），
        # 带子只在创建时上色的话，改完得等下次重建才生效
        self._style_band(band)
        try:
            feature = self._layer.getFeature(int(fid))
            geometry = feature.geometry() if feature.isValid() else None
            if geometry is None or geometry.isNull() or geometry.isEmpty():
                self.clear()
                return False
            band.setToGeometry(geometry, self._layer)
            band.show()
        except RuntimeError:
            return False  # 图层 / 画布已销毁
        return True

    def _ensure_band(self) -> Optional[QgsRubberBand]:
        if self._canvas is None:
            return None
        if self._band is None:
            try:
                band = QgsRubberBand(self._canvas, self._layer.geometryType())
            except RuntimeError:  # 画布已销毁
                return None
            self._style_band(band)
            self._band = band
        return self._band

    def _style_band(self, band: QgsRubberBand) -> None:
        """上色：有 ``style_provider`` 就按它给的选中高亮样式，否则用洋红闪烁色。"""
        kind = kind_of_geometry_type(self._layer.geometryType()) if self._style_provider else None
        if kind is not None:
            style = self._style_provider()
            if style is not None:
                style_band(band, kind, style)
                return
        band.setColor(QColor(*_FLASH_COLOR))
        band.setWidth(3)
        band.setFillColor(QColor(*_FLASH_COLOR, 70))

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
