"""切片期间的图层隔离：临时隐藏目标图层以外的可见图层。

``native:tilesxyzdirectory`` / ``native:tilesxyzmbtiles`` 渲染的是**整个地图
画布**，而不是 ``EXTENT`` 对应的那一个图层：画布上所有可见图层（在线底图、
要素图层……）都会被烧进每一块瓦片。真实工程里这会让切片慢得离谱——实测
8249 个面的 yt.shp 单次渲染 23 秒，z0–2 三个元瓦片从 0.4 秒拖到 11 分钟以上，
输出里还会混进在线底图。

这里在任务开始前把其他可见图层临时隐藏，任务结束（成功 / 失败 / 取消）后
恢复原状；目标图层即使原本隐藏也会临时显示，否则算法会切出一片空白。
多个切片任务并发时按 token 共享状态：所有任务共用的目标图层保持可见，
等最后一个任务结束才恢复原始可见性。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsMapLayer, QgsProject

logger = get_logger(__name__)

_lock = threading.RLock()
#: token → 该任务要保留可见的图层 id
_owners: dict[str, str] = {}
#: 图层 id → 隔离前的可见性（只记录第一次隔离时的状态）
_original: dict[str, bool] = {}
#: 为显示目标图层而临时展开的祖先组：(节点, 原可见性)
_groups: list[tuple[object, bool]] = []


def _force_visible(node) -> None:
    """让图层节点及其祖先组可见（目标图层在折叠/未勾选的组里也要能渲染）。"""
    groups = []
    parent = node.parent()
    while parent is not None:
        groups.append(parent)
        parent = parent.parent()
    for group in reversed(groups):
        if not group.itemVisibilityChecked():
            _groups.append((group, False))
            group.setItemVisibilityChecked(True)


def _nodes_by_layer(root) -> dict[str, list]:
    """图层 id → 图例节点列表；同一图层的所有节点都要处理。"""
    mapping: dict[str, list] = {}
    for node in root.findLayers():
        mapping.setdefault(node.layerId(), []).append(node)
    return mapping


def isolate_other_layers(
    project: "QgsProject", keep_layer: "QgsMapLayer", token: str
) -> list[str]:
    """隐藏除 ``keep_layer`` 及其他并发任务目标外的可见图层。

    返回本次实际隐藏的图层名（供界面上日志提示）。同一 token 重复调用是
    幂等的；``keep_layer`` 原本隐藏时会临时显示，结束时恢复。
    """
    if not token:
        return []
    with _lock:
        protected = set(_owners.values())
        protected.add(keep_layer.id())
        _owners[token] = keep_layer.id()

        hidden: list[str] = []
        root = project.layerTreeRoot()
        nodes_by_layer = _nodes_by_layer(root)
        for layer in project.mapLayers().values():
            lid = layer.id()
            # 一个图层可能在图例里出现多次（重复添加/复制到组），全部处理，
            # 只要有一个节点可见就会被画进瓦片。
            nodes = nodes_by_layer.get(lid, [])
            if not nodes:
                continue
            if lid in protected:
                if not any(node.itemVisibilityChecked() for node in nodes):
                    _original.setdefault(lid, False)
                    for node in nodes:
                        node.setItemVisibilityChecked(True)
                for node in nodes:
                    _force_visible(node)
                continue
            checked = [node for node in nodes if node.itemVisibilityChecked()]
            # 本来就隐藏的图层不记录成「被我们隐藏」，恢复时保持隐藏即可；
            # 但仍记录原始状态，以备它稍后成为另一个任务的目标图层。
            _original.setdefault(lid, bool(checked))
            if not checked:
                continue
            for node in checked:
                node.setItemVisibilityChecked(False)
            hidden.append(layer.name())
        return hidden


def release_layer_isolation(project: "QgsProject", token: str) -> None:
    """任务结束（或启动失败）时释放隔离；最后一个 token 释放时恢复可见性。"""
    if not token:
        return
    with _lock:
        keep_id = _owners.pop(token, None)
        if keep_id is None:
            return
        root = project.layerTreeRoot()
        nodes_by_layer = _nodes_by_layer(root)
        if _owners:
            # 还有任务在跑：只处理本任务独有的目标图层，公共目标保持可见
            if keep_id not in _owners.values():
                for node in nodes_by_layer.get(keep_id, []):
                    node.setItemVisibilityChecked(_original.get(keep_id, True))
            return

        for lid, visible in list(_original.items()):
            for node in nodes_by_layer.get(lid, []):
                if node.itemVisibilityChecked() != visible:
                    node.setItemVisibilityChecked(visible)
        _original.clear()
        for group, visible in reversed(_groups):
            try:
                group.setItemVisibilityChecked(visible)
            except RuntimeError:  # 组已被删除（切换了工程）
                pass
        _groups.clear()


def active_isolation_count() -> int:
    """当前仍未释放的切片任务数（测试 / 诊断用）。"""
    with _lock:
        return len(_owners)


def reset_isolation_state() -> None:
    """清空内部状态（测试隔离用）。"""
    with _lock:
        _owners.clear()
        _original.clear()
        _groups.clear()
