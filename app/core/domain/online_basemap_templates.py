"""内置「在线底图」模板：Dock 空白处右键「添加在线底图」的候选清单。

Dock 展示的图层来自 ``config/online_basemap_config.yaml``（独立清单文件）；本模块
是**内置候选集**：当某条官方图源没在清单里（用户删过 / 还没加高德）时，可在
Dock 空白处右键 → 从模板里选一条，把它加进 Dock 树并写回独立清单文件
（右键添加 → 持久化，重启仍在），而无须手工编辑 yaml。

模板即 :class:`OnlineBasemapDef`（带 ``group``）。天地图 URL 里的 key 用
``{tk}`` **占位符**引用（真实 key 统一在 ``config/app.yaml`` 的 ``map.keys``
管理，构建图层时才替换，见 basemap 模块）；高德为社区公开 endpoint 示例，
等拿到正式 key / 地址后替换对应 ``url``（或在写回后的 yaml 里直接改）。
"""

from __future__ import annotations

from app.env.config import OnlineBasemapDef

#: 天地图 referer —— browser-key 的请求 Referer 白名单域名（非 key，见 map.keys）
_TIANDITU_REFERER = "https://www.tianditu.gov.cn/"
#: 天地图节点（t0-t7 任选；wmts 按 layer 分路径）
_TIANDITU_HOST = "https://t0.tianditu.gov.cn"

#: 天地图 url 里的 {x}/{y}/{z} 占位来自 wmts 模板串（f-string 里写双花括号）
def _td_url(layer: str) -> str:
    """天地图 wmts 瓦片 URL（LAYER=img/vec/cva 对应影像/矢量/标注）。

    key 用 ``{tk}`` 占位符引用 ``map.keys.tianditu_tk``，模板本身不内嵌真值。
    """
    return (
        f"{_TIANDITU_HOST}/{layer}_w/wmts?SERVICE=WMTS&REQUEST=GetTile"
        f"&VERSION=1.0.0&LAYER={layer}&STYLE=default&TILEMATRIXSET=w"
        f"&FORMAT=tiles&TILECOL={{x}}&TILEROW={{y}}&TILEMATRIX={{z}}"
        f"&tk={{tk}}"
    )


#: 右键「添加在线底图」的内置模板（按配置书写顺序分组展示）。
#: 天地图三件套直接可用；高德为社区公开 endpoint 示例，正式源到手后替换 url。
ONLINE_BASEMAP_PRESETS: list[OnlineBasemapDef] = [
    OnlineBasemapDef.from_dict(
        {
            "group": "天地图",
            "name": "天地图·影像",
            "url": _td_url("img"),
            "referer": _TIANDITU_REFERER,
            "zmin": 0,
            "zmax": 18,
        }
    ),
    OnlineBasemapDef.from_dict(
        {
            "group": "天地图",
            "name": "天地图·矢量",
            "url": _td_url("vec"),
            "referer": _TIANDITU_REFERER,
            "zmin": 0,
            "zmax": 18,
        }
    ),
    OnlineBasemapDef.from_dict(
        {
            "group": "天地图",
            "name": "天地图·标注",
            "url": _td_url("cva"),
            "referer": _TIANDITU_REFERER,
            "zmin": 0,
            "zmax": 18,
        }
    ),
    # ---- 高德地图（示例 endpoint；正式 key/地址到手后替换 url）----
    OnlineBasemapDef.from_dict(
        {
            "group": "高德地图",
            "name": "高德·影像",
            "url": "https://webst01.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
            "zmin": 1,
            "zmax": 19,
        }
    ),
    OnlineBasemapDef.from_dict(
        {
            "group": "高德地图",
            "name": "高德·标注",
            "url": "https://webst01.is.autonavi.com/appmaptile?style=7&x={x}&y={y}&z={z}",
            "zmin": 1,
            "zmax": 19,
        }
    ),
    OnlineBasemapDef.from_dict(
        {
            "group": "高德地图",
            "name": "高德·矢量",
            "url": "https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
            "zmin": 3,
            "zmax": 19,
        }
    ),
    OnlineBasemapDef.from_dict(
        {
            "group": "高德地图",
            "name": "高德·路网",
            "url": "https://webst01.is.autonavi.com/appmaptile?style=8&x={x}&y={y}&z={z}",
            "zmin": 3,
            "zmax": 19,
        }
    ),
]


__all__ = ["ONLINE_BASEMAP_PRESETS"]
