"""当前工程快照：出问题时"他打开的到底是什么"。

诊断包里除了日志，还需要一份现场记录。用户描述往往是"我就是切了个图斑就报错了"，
而**切图的候选图层**得在清单里对得上：源路径 / 坐标系 / 几何类型 / 字段能直接指出
脏数据（GDB 导出的字段长度不足、图层只读、要素数为 0 的过滤结果……），这些都是在
日志里看不出来的。

几条刻意的取舍：

- **不抛异常**：任何一项取不到都退化成 ``"?"`` 或整层降级成 ``{"error": …}``。这是
  诊断工具，它自己崩掉比少一项更糟。
- **要素数只取本地文件型 provider**：``featureCount()`` 在 WFS / PostGIS 上可能扫全表，
  用户点"导出诊断包"不该卡在那儿。网络图层报 ``None``，并注明原因。
- **叫 ``snapshot`` 不叫 ``report``**：它只描述现状，不下结论。
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

#: 这些 provider 是本地文件型，``featureCount()`` 读一下就有的；其余（WFS / PostGIS /
#: 各种在线服务）跳过——见模块头部说明。
_LOCAL_PROVIDERS = frozenset({"memory", "ogr", "delimitedtext", "spatialite", "gpx", "geojson"})

#: 字段列出来的上限：一张 300 字段的表会把 project.json 撑得没法看
_MAX_FIELDS = 60


def snapshot() -> dict[str, Any]:
    """当前工程 + 图层树 + 各图层概要（**只读**，不改动任何 QGIS 状态）。"""
    try:
        from qgis.core import QgsProject
    except ImportError as exc:  # 无 QGIS 运行时（单元测试 / 服务端）
        return {"status": f"无 GIS 运行时：{exc}"}

    try:
        project = QgsProject.instance()
    except Exception as exc:  # noqa: BLE001 —— QGIS 正在退出时 instance() 也可能炸
        return {"status": f"取工程失败：{exc}"}
    if project is None:
        return {"status": "未连接工程"}

    layers = list(project.mapLayers().values())
    return {
        "file": _call(project, "fileName") or "（未保存）",
        "title": _call(project, "title") or "",
        "crs": _authid(_call(project, "crs")),
        "layer_count": len(layers),
        "layers": [_layer(layer) for layer in layers],
        "tree": _tree_lines(project),
    }


def snapshot_text() -> str:
    """快照 → JSON 文本（诊断包里的 ``project.json``）。"""
    return json.dumps(snapshot(), ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------- 内部


def _layer(layer: Any) -> dict[str, Any]:
    """单个图层的概要；任何一项取不到都退化成 ``"?"``。"""
    info: dict[str, Any] = {
        "name": _call(layer, "name") or "?",
        "type": _kind(layer),
        "provider": _call(layer, "providerType") or "?",
        "source": _call(layer, "source") or "?",
        "crs": _authid(_call(layer, "crs")),
        "valid": _call(layer, "isValid"),
        "visible": _visible(layer),
    }
    if info["type"] == "vector":
        info["geometry"] = _enum_name(_call(layer, "wkbType"))
        info["feature_count"] = _feature_count(layer, info["provider"])
        info["read_only"] = _call(layer, "readOnly")
        # 子集过滤是"明明有数据却跑出 0 要素"的头号原因，必须记
        info["subset"] = _call(layer, "subsetString") or ""
        info["fields"] = _fields(layer)
    elif info["type"] == "raster":
        info["size"] = _raster_size(layer)
    return info


def _kind(layer: Any) -> str:
    """``vector`` / ``raster`` / 其它（用 ``type()`` 的枚举名，不 import Qt）。"""
    name = _enum_name(_call(layer, "type"))
    return {"VectorLayer": "vector", "RasterLayer": "raster"}.get(name, name or "?")


def _feature_count(layer: Any, provider: str) -> Any:
    """要素数：只对本地文件型 provider 取（网络图层可能扫全表，见模块头部）。"""
    if str(provider).lower() not in _LOCAL_PROVIDERS:
        return "未统计（网络数据源）"
    try:
        return layer.featureCount()
    except Exception:  # noqa: BLE001
        return "?"


def _fields(layer: Any) -> Any:
    """字段清单 ``名称:类型``（矢量图层）。取不到返回 ``[]``。"""
    try:
        fields = layer.fields()
        names = [f"{field.name()}:{field.typeName()}" for field in fields]
    except Exception:  # noqa: BLE001
        return []
    if len(names) > _MAX_FIELDS:
        return [*names[:_MAX_FIELDS], f"…其余 {len(names) - _MAX_FIELDS} 个字段省略"]
    return names


def _raster_size(layer: Any) -> Any:
    """栅格宽高（``宽x高``）。"""
    try:
        return f"{layer.width()}x{layer.height()}"
    except Exception:  # noqa: BLE001
        return "?"


def _visible(layer: Any) -> Any:
    """图层在树里是否可见（``QgsProject`` 的 ``layerTreeRoot`` 才是可见性的权威）。"""
    try:
        from qgis.core import QgsProject

        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        return bool(node.isVisible()) if node is not None else None
    except Exception:  # noqa: BLE001
        return None


def _tree_lines(project: Any) -> list[str]:
    """图层树的层级文本（缩进表示分组），顺序即绘制顺序。"""
    lines: list[str] = []

    def walk(node: Any, depth: int) -> None:
        try:
            children = node.children()
        except Exception:  # noqa: BLE001
            return
        for child in children:
            name = _call(child, "name") or "?"
            try:
                is_group = child.nodeType() == child.NodeType.Group
            except Exception:  # noqa: BLE001
                is_group = False
            lines.append(f"{'  ' * depth}{'▸' if is_group else '-'} {name}")
            walk(child, depth + 1)

    with contextlib.suppress(Exception):  # 树坏了不该让整份快照作废
        walk(project.layerTreeRoot(), 0)
    return lines


def _authid(crs: Any) -> str:
    """坐标系 ``EPSG:4490``；只有描述时退回描述。"""
    if crs is None:
        return "?"
    return _call(crs, "authid") or _call(crs, "description") or "?"


def _enum_name(value: Any) -> str:
    """PyQt6 枚举取 ``.name``（``WkbType.Polygon`` → ``Polygon``）。"""
    if value is None:
        return "?"
    return getattr(value, "name", None) or str(value)


def _call(obj: Any, name: str) -> Any:
    """调 ``obj.name()``；属性不存在 / 抛异常都返回 ``None``（快照不能出意外）。"""
    try:
        return getattr(obj, name)()
    except Exception:  # noqa: BLE001
        return None


__all__ = ["snapshot", "snapshot_text"]
