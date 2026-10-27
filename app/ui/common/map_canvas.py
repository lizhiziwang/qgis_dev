"""应用画布 ``AppMapCanvas``：打开图层渲染缓存，同时保住 Python 侧 ``refresh()`` 的整幅重画语义。

**为什么需要它（性能）**

``QgsMapCanvas`` 默认 ``isCachingEnabled() == False``：每次 ``refresh()`` 都把
**全部可见图层从零重绘**一遍。实测 2 个 2 万要素图层 + 1280x760 窗口，一次全量
重绘约 250 ~ 470 ms。

而点选 / 框选 / 取消选择改的只是某个图层的选中集，却要为此付整幅重绘的钱。

QGIS 自身在选中集变化时走的是：

``QgsVectorLayer.selectionChanged`` -> 内部 ``triggerRepaint()`` ->
``QgsVectorLayer.repaintRequested`` -> ``QgsMapCanvas.layerRepaintRequested``，
该槽只把**这一个图层**的缓存置脏，再 ``refresh()``；其它图层直接复用缓存图片。
所以只要打开画布缓存，「改选中 -> 重绘」从 ~260 ms 掉到 ~13 ms（实测约 20 倍，
见 ``.tmp/probe_cache.py``）。

**为什么还要重写 ``refresh()``**

打开缓存后，**不经过 ``triggerRepaint()`` 的数据改动**——例如直接
``layer.dataProvider().addFeatures()``、或瓦片预取完成后重画——不会让缓存失效，
于是 Python 侧原有的 ``canvas.refresh()`` 会拿到旧图片（实测：新增要素后
``refresh()`` 画面不变，只有清缓存才更新）。

而仓里有 40+ 处 Python 侧 ``canvas.refresh()``，逐处改成「先清缓存」既啰嗦又容易漏。
本类改为重写 ``refresh()``：Python 调用点一律「清缓存 + 重画」，与没开缓存时**完全
等价**；从而保证「今天能刷出来的东西，开了缓存依然刷得出来」。

``QgsMapCanvas::refresh()`` 不是虚函数，C++ 内部（``triggerRepaint`` 通路、
``mRefreshTimer``）调用的是基类实现，**不会**被这层重写拦到——缓存仍在
``triggerRepaint`` 驱动的重绘里生效。

**平移 / 缩放时影像“消失再出现”**

``QgsMapCanvas`` 默认每 250ms 把渲染中的半成品图贴到画布（``mapUpdateTimeout``）。
本地大影像重绘超过 250ms 时，半成品里尚未画完的区域是透明的，旧影像会被这张
透明图替换，等渲染完成才回来。这里把间隔拉长到 :data:`_MAP_UPDATE_INTERVAL_MS`：
正常渲染在计时器触发前就结束，画布一直保留旧帧；只有确实很慢的渲染才会显示
一次中间结果。
"""

from __future__ import annotations

from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtGui import QColor

#: 渲染中间结果贴到画布的间隔（毫秒）。QGIS 默认 250ms：平移 / 缩放触发重绘后，
#: 如果当前图层（尤其是本地大影像）还没画完，250ms 后就会把“只有部分内容、其余
#: 透明”的半成品贴上去 → 看起来像影像先消失、渲染完再出现。拉长到 1s：正常渲染
#: 在这之前就结束（``rendererJobFinished`` 会停掉计时器），画布一直保留旧帧；只有
#: 确实很慢的渲染才会在 2s 后显示一次中间结果。想更稳可继续调大。
_MAP_UPDATE_INTERVAL_MS = 2_000


class AppMapCanvas(QgsMapCanvas):
    """带图层渲染缓存的画布；Python 侧 ``refresh()`` 保持「整幅重画」旧语义。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 缓存按图层存渲染图：triggerRepaint 驱动的重绘只重画脏图层
        self.setCachingEnabled(True)
        # 降低“半成品渲染图”出现的频率：本地大影像重绘慢于 250ms 时，
        # QGIS 会把还没画完的透明图贴到画布上，表现为影像消失再出现。
        self.setMapUpdateInterval(_MAP_UPDATE_INTERVAL_MS)
        # 地图本身按**透明底**渲染，画布底色交给控件背景。
        # 这样切主题只改控件背景（一次重绘，微秒级），不必 setCanvasColor +
        # refresh() 触发全部图层重画 —— 百万要素图层一次全量重画要十几秒。
        self.setCanvasColor(QColor(0, 0, 0, 0))

    def apply_theme_background(self, color: str) -> None:
        """只改画布控件底色，不触发地图重绘（主题切换用）。"""
        self.setStyleSheet(f"QgsMapCanvas {{ background-color: {color}; }}")

    def refresh(self) -> None:
        """清掉图层渲染缓存后再重画（等价于未开缓存时的 ``refresh()``）。

        仅覆盖 Python 调用点；C++ 内部（``triggerRepaint`` -> ``refresh``）走基类
        实现，仍复用缓存。
        """
        self.clearCache()
        super().refresh()


__all__ = ["AppMapCanvas"]
