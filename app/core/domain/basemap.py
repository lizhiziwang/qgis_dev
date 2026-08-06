"""底图加载：按配置把天地图 XYZ 瓦片加进项目。

由启动回调调用（见 :mod:`app.bootstrap`），也可在运行时手动调用。
``build_xyz_uri`` 是纯 Python 逻辑，可脱离 QGIS 单测；真正创建图层时才
按需 import qgis.core（对齐"QGIS runtime 装配前不 import qgis"的约束）。

--------------------------------------------------------------------------
为什么之前瓦片一直加载不出来 —— 两个坑，缺一不可
--------------------------------------------------------------------------

坑 1：连接串里 URL 的编码粒度。
   QGIS 的 XYZ 连接串是 ``type=xyz&url=...&zmin=..&zmax=..``，用 ``&`` 分隔
   参数。天地图的瓦片 URL 本身带一堆 ``&``（WMTS 查询串），如果原样塞进
   ``url=``，QGIS 会按 ``&`` 拆连接串，把 URL 截断。

   → 所以 URL 内部的 ``&`` 必须转义成 ``%26``。

   但注意：**只能转义 ``&``，不能像 ``urllib.parse.quote(url, safe="")`` 那样
   把整条 URL 百分号编码。** 实测发现 QGIS 的 xyz provider 只会还原查询串里的
   ``%26``/``%3D``，**不会还原 scheme/主机/路径里的 ``%3A``/``%2F``/``%3F``**。
   一旦 ``https://`` 被编成 ``https%3A%2F%2F``，实际请求会发到一个名为
   ``https%3A%2F%2Ft0.tianditu.gov.cn`` 的假主机，DNS 解析失败 → 所有瓦片
   请求全挂 → 地图空白（图层本身是有效的，这就是"能加载但显示不出来"的根源）。

   → 所以用 ``quote(url, safe=":/?={}")``：只编码 ``&``，保留 ``:`` ``/`` ``?``
   ``=`` ``{x}/{y}/{z}`` 原样，让主机干净、占位符可替换、查询串能被正确还原。

坑 2：天地图"浏览器端" key 需要 Referer 头。
   配置里这个 ``tk`` 是**浏览器端**类型的 key，天地图校验规则是：请求必须带
   ``Mozilla`` 前缀的 User-Agent **并且** Referer 来自白名单域名
   （本 key 是 ``https://www.tianditu.gov.cn/``）。QGIS 自己的 User-Agent 已经
   是 ``Mozilla/5.0 (QGIS ...)`` 前缀，满足 UA 条件；但瓦片请求**没带 Referer**
   就会被 403 拒绝（错误码 301012 权限类型错误）。

   连接串支持 ``referer=`` 参数，但实测部分 QGIS 版本不会把它应用到 xyz 瓦片
   请求上。所以这里改用**全局请求预处理**：给 ``QgsNetworkAccessManager`` 装
   一个 ``setRequestPreprocessor`` 回调，凡是发往 ``tianditu.gov.cn`` 的请求
   强制塞 ``Referer`` 头 —— 不依赖 QGIS 是否读连接串参数，保证所有瓦片请求
   都带上。

   （验证方法：在预处理回调里打印被拦截的请求 URL，看到 host 是否被编码坏 /
   请求是否真正发出；用 ``QgsNetworkAccessManager.blockingGet`` 单独拉一张
   瓦片可以确认 UA+Referer 组合是否被服务端接受。）
--------------------------------------------------------------------------
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote

from app.env.config import BaseTileConfig, MapConfig
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsProject, QgsRasterLayer

logger = get_logger(__name__)

# 需要 Referer 白名单的瓦片服务 host（当前只有天地图）
_REFERER_HOST = "tianditu.gov.cn"


def build_xyz_uri(tile: BaseTileConfig) -> str:
    """构造 QGIS XYZ 连接串 ``type=xyz&url=...&zmin=..&zmax=..``。

    关键：URL 里**只转义 ``&``**（``safe=":/?={}"``），其余 ``:`` ``/`` ``?``
    ``=`` ``{x}`` 保持原样。原因见模块 docstring 的"坑 1"——把整条 URL 编码会让
    QGIS 把 host 发成 ``https%3A%2F%2F...`` 假域名，所有瓦片请求失败。
    ``referer`` 非空时拼 ``referer=``（连接串参数 + 模块级请求预处理双重保障）。
    """
    parts = [
        "type=xyz",
        "url=" + quote(tile.url, safe=":/?={}"),
        f"zmin={tile.zmin}",
        f"zmax={tile.zmax}",
    ]
    if tile.referer:
        # referer 保持原样（不含 & 或 =，不会拆断连接串），QGIS 直接用作请求头
        parts.append("referer=" + tile.referer)
    return "&".join(parts)


def build_base_tile_layer(tile: Optional[BaseTileConfig]) -> Optional["QgsRasterLayer"]:
    """按配置构造 XYZ 底图图层；失败返回 ``None`` 并记录具体原因。"""
    if tile is None:
        logger.warning("未配置 base_tile（config 里 map.base_tile 缺失）")
        return None
    if not tile.enabled:
        logger.info("base_tile 已禁用，跳过加载")
        return None
    if not tile.url:
        logger.warning("base_tile.url 为空")
        return None

    from qgis.core import QgsRasterLayer

    uri = build_xyz_uri(tile)
    layer = QgsRasterLayer(uri, tile.name, "wms")
    if not layer.isValid():
        # QgsRasterLayer 没有 lastError()；用 error() 取错误信息
        logger.warning(
            "XYZ 底图图层无效 url=%s error=%s",
            _mask_url(tile.url),
            layer.error().message(),
        )
        return None
    return layer


def add_base_tile_layer(
    map_cfg: MapConfig, project: Optional["QgsProject"] = None
) -> Optional["QgsRasterLayer"]:
    """把配置里的底图加进项目。返回图层；未启用 / 失败返回 ``None``。"""
    if map_cfg.base_tile is not None and map_cfg.base_tile.referer:
        # 连接串的 referer= 在部分 QGIS 版本对 xyz 不生效，用全局请求预处理兜底：
        # 发往该瓦片服务的所有请求强制带 Referer 头（见模块 docstring 的"坑 2"）。
        _install_referer_preprocessor(map_cfg.base_tile.referer)
    layer = build_base_tile_layer(map_cfg.base_tile)
    if layer is not None:
        from qgis.core import QgsProject

        (project or QgsProject.instance()).addMapLayer(layer)
        logger.info("已加载 XYZ 底图：%s", layer.name())
    return layer


def _install_referer_preprocessor(referer: str) -> None:
    """给 QGIS 网络请求装一个预处理：发往该瓦片服务的请求强制带 Referer 头。

    QGIS 的 ``setRequestPreprocessor`` 会在每个请求发出前回调，传进来的是
    :class:`QNetworkRequest`（指针语义），直接 ``setRawHeader`` 即可生效。
    """
    from qgis.core import QgsNetworkAccessManager

    referer_bytes = referer.encode("utf-8")

    def _preprocessor(request) -> None:
        url = request.url().toString()
        if _REFERER_HOST in url:
            request.setRawHeader(b"Referer", referer_bytes)

    try:
        QgsNetworkAccessManager.instance().setRequestPreprocessor(_preprocessor)
        logger.info("已安装瓦片服务 Referer 预处理 host=%s", _REFERER_HOST)
    except Exception as exc:  # noqa: BLE001
        logger.warning("安装瓦片服务 Referer 预处理失败：%s", exc)


def _mask_url(url: str) -> str:
    """把 URL 里的 tk token 打码，避免敏感信息进日志。"""
    return re.sub(r"(tk=)[^&\s]+", r"\1***", url)


__all__ = ["add_base_tile_layer", "build_base_tile_layer", "build_xyz_uri"]
