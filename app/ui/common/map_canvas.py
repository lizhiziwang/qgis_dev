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
"""

from __future__ import annotations

from qgis.gui import QgsMapCanvas


class AppMapCanvas(QgsMapCanvas):
    """带图层渲染缓存的画布；Python 侧 ``refresh()`` 保持「整幅重画」旧语义。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 缓存按图层存渲染图：triggerRepaint 驱动的重绘只重画脏图层
        self.setCachingEnabled(True)

    def refresh(self) -> None:
        """清掉图层渲染缓存后再重画（等价于未开缓存时的 ``refresh()``）。

        仅覆盖 Python 调用点；C++ 内部（``triggerRepaint`` -> ``refresh``）走基类
        实现，仍复用缓存。
        """
        self.clearCache()
        super().refresh()


__all__ = ["AppMapCanvas"]
