"""图层下拉的安全刷新工具。

``QComboBox.addItem(name, layer)`` 只把 QObject 指针放进 itemData，
**不持有 Python 包装对象的引用**。图层从工程删除后 C++ 对象随之销毁，
``currentData()`` 再取出来只剩一个裸 ``QObject``（见
:mod:`app.ui.dialogs.import_layer_dialog` 的同名说明）——此时调
``.source()`` / ``.name()`` 会抛 ``AttributeError``；若拿到的是已删除的
sip 包装则抛 ``RuntimeError``。对话框监听 ``layersRemoved`` 刷新下拉框时
最容易踩到这一脚（QA BUG-02）。

这里把「取当前选中图层的 source → 重建 → 按 source 落位」收敛成一个函数，
调用方不再直接对 ``currentData()`` 调图层方法。
"""

from __future__ import annotations

import contextlib
from typing import Iterable, Optional

from qgis.core import QgsMapLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtWidgets import QComboBox


def live_layer(layer):
    """返回仍可安全调用的图层对象；wrapper 已销毁 / 类型不对时返回 ``None``。"""
    if layer is None:
        return None
    try:
        # 裸 QObject（wrapper 已被回收）不是 sip 包装，直接判 False
        if sip.isdeleted(layer):
            return None
    except TypeError:
        return None
    if not isinstance(layer, QgsMapLayer):
        return None
    return layer


def layer_source(layer) -> Optional[str]:
    """取图层 source（比 name 稳定，重名也安全）；图层不可用时返回 ``None``。"""
    layer = live_layer(layer)
    if layer is None:
        return None
    try:
        return layer.source()
    except (AttributeError, RuntimeError):
        return None


def combo_current_layer(combo: QComboBox):
    """取下拉框当前图层；itemData 里的 wrapper 已失效时返回 ``None``。"""
    return live_layer(combo.currentData())


def populate_layer_combo(
    combo: QComboBox,
    layers: Iterable,
    *,
    layer_type: Optional[int] = None,
) -> None:
    """按 ``layers`` 重建下拉框（itemData 存图层对象），并尽量保持当前选中项。

    Args:
        combo: 目标下拉框。
        layers: 候选图层（工程图层表 / 图层树顺序均可）。
        layer_type: 只保留该类型（如 ``QgsMapLayer.RasterLayer``）；``None`` 不过滤。
    """
    current_src = layer_source(combo.currentData())
    combo.clear()
    for layer in layers:
        layer = live_layer(layer)
        if layer is None:
            continue
        if layer_type is not None and layer.type() != layer_type:
            continue
        combo.addItem(layer.name(), layer)
        if current_src is not None and layer_source(layer) == current_src:
            combo.setCurrentIndex(combo.count() - 1)


def retitle_layer_combo(combo: QComboBox) -> None:
    """图层改名后刷新下拉框条目文字（itemData 里存的是图层对象）。

    只改文字、不动顺序与当前选择，比重建下拉框安全；已失效的图层条目保留
    原文字（通常马上会被 ``layersRemoved`` 触发的重建清掉）。
    """
    for index in range(combo.count()):
        layer = live_layer(combo.itemData(index))
        if layer is not None:
            combo.setItemText(index, layer.name())


class LayerNameWatcher(QObject):
    """工程图层改名时调用 ``callback``；随图层增删自动维护 ``nameChanged`` 连接。

    对话框实例常被工具树缓存复用，图层在面板里改名后下拉框文字不会自己变。
    用法：``self._name_watcher = LayerNameWatcher(project, self._on_layer_renamed, self)``，
    回调里调 :func:`retitle_layer_combo` 或重新填充下拉框即可。
    """

    def __init__(self, project, callback, parent=None) -> None:
        super().__init__(parent)
        self._project = project
        self._callback = callback
        self._hooked: set[str] = set()
        project.layersAdded.connect(self._sync_hooks)
        project.layersRemoved.connect(self._sync_hooks)
        self._sync_hooks()

    def _sync_hooks(self, *_args) -> None:
        live: set[str] = set()
        try:
            layers = list(self._project.mapLayers().values())
        except RuntimeError:
            layers = []
        for layer in layers:
            layer_id = layer.id()
            live.add(layer_id)
            if layer_id in self._hooked:
                continue
            try:
                layer.nameChanged.connect(self._on_name_changed)
                self._hooked.add(layer_id)
            except (AttributeError, RuntimeError):
                continue
        # 已移除的图层不再跟踪（C++ 对象销毁时连接自动断开）
        self._hooked.intersection_update(live)

    def _on_name_changed(self, *_args) -> None:
        with contextlib.suppress(RuntimeError):
            self._callback()


__all__ = [
    "LayerNameWatcher",
    "combo_current_layer",
    "layer_source",
    "live_layer",
    "populate_layer_combo",
    "retitle_layer_combo",
]
