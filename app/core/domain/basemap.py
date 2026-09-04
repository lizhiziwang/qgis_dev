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

import json
import re
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote

from app.env.config import (
    BaseTileConfig,
    MapConfig,
    MapKeysConfig,
    OnlineBasemapDef,
)
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsMapLayer, QgsProject, QgsRasterLayer, QgsVectorLayer

logger = get_logger(__name__)

# 需要 Referer 白名单的瓦片服务 host（当前只有天地图）
_REFERER_HOST = "tianditu.gov.cn"

#: URL 判等用的 key 归一占位符：老配置内嵌的真实 tk 与 ``{tk}`` 模板判等时归一
_KEY_PLACEHOLDER = "{tk}"
_KEY_PARAM = re.compile(r"tk=[^&\s]+")


def apply_key_placeholders(url: str, keys: Optional[MapKeysConfig]) -> str:
    """把 url 里的 key 占位符替换成 ``map.keys`` 里的真实值。

    ``keys`` 为 ``None`` → 原样返回（单测 / 未接 key 的旧数据不变）。非
    ``None`` → 无条件替换 ``{tk}`` / ``{gaode_key}``；key 被清空 → 替换成
    ``tk=``（"key 已清"的诚实状态，瓦片会 403 而非静默用旧 key）。**不在**
    config 解析期替换，保证模板/配置 url 判等自洽（见 build_online_basemap_layer
    docstring 与主窗口去重逻辑）。
    """
    if keys is None:
        return url
    return (
        url.replace("{tk}", keys.tianditu_tk).replace("{gaode_key}", keys.gaode_key)
    )


def canonical_basemap_url(url: str) -> str:
    """把 URL 归一成“与 key 无关”的判等键：``tk=<任意值>`` → ``tk={tk}``。

    老配置里内嵌真实 tk（``tk=cb75…``）的 URL，与新模板的 ``tk={tk}`` 形态
    不同导致去重/置灰失配——归一后再比较两边一致。只用于判等，不改存储 url。
    """
    return _KEY_PARAM.sub("tk=" + _KEY_PLACEHOLDER, url)


def build_xyz_uri(tile: BaseTileConfig, keys: Optional[MapKeysConfig] = None) -> str:
    """构造 QGIS XYZ 连接串 ``type=xyz&url=...&zmin=..&zmax=..``。

    关键：URL 里**只转义 ``&``**（``safe=":/?={}"``），其余 ``:`` ``/`` ``?``
    ``=`` ``{x}`` 保持原样。原因见模块 docstring 的"坑 1"——把整条 URL 编码会让
    QGIS 把 host 发成 ``https%3A%2F%2F...`` 假域名，所有瓦片请求失败。
    ``referer`` 非空时拼 ``referer=``（连接串参数 + 模块级请求预处理双重保障）。
    url 里的 key 占位符（如 ``{tk}``）由 ``keys`` 替换成真值（见
    :func:`apply_key_placeholders`；``keys=None`` 时保留占位符原样）。
    """
    url = apply_key_placeholders(tile.url, keys)
    parts = [
        "type=xyz",
        "url=" + quote(url, safe=":/?={}"),
        f"zmin={tile.zmin}",
        f"zmax={tile.zmax}",
    ]
    if tile.referer:
        # referer 保持原样（不含 & 或 =，不会拆断连接串），QGIS 直接用作请求头
        parts.append("referer=" + tile.referer)
    return "&".join(parts)


def build_base_tile_layer(
    tile: Optional[BaseTileConfig], keys: Optional[MapKeysConfig] = None
) -> Optional["QgsRasterLayer"]:
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

    uri = build_xyz_uri(tile, keys)
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


def build_wms_uri(
    d: "OnlineBasemapDef", keys: Optional[MapKeysConfig] = None
) -> str:
    """构造 QGIS WMS 连接串 ``url=...&layers=..&styles=..&format=..&crs=..``。

    QGIS 的 wms provider（与 xyz 同一个）按 ``&`` 拆连接串，URL 内部的 ``&``
    必须转义成 ``%26``——沿用 :func:`build_xyz_uri` 的坑 1 处理（只编码 ``&``，
    保留 ``: / ? =``）。layers/styles/format/crs 等是简单标识，原样拼接
    （弹窗层已禁止这些字段含 ``&`` / ``=``）。
    """
    url = apply_key_placeholders(d.url, keys)
    parts = ["url=" + quote(url, safe=":/?={}")]
    if d.layers:
        parts.append("layers=" + d.layers)
    if d.styles:
        parts.append("styles=" + d.styles)
    if d.format:
        parts.append("format=" + d.format)
    if d.crs:
        parts.append("crs=" + d.crs)
    return "&".join(parts)


def build_wmts_uri(
    d: "OnlineBasemapDef", keys: Optional[MapKeysConfig] = None
) -> str:
    """WMTS 连接串 = WMS 串 + ``tileMatrixSet=``（QGIS 靠 capabilities 文档 +
    矩阵集标识识别 WMTS；服务地址应指向 WMTS GetCapabilities 端点）。"""
    parts = [build_wms_uri(d, keys)]
    if d.tile_matrix_set:
        parts.append("tileMatrixSet=" + d.tile_matrix_set)
    return "&".join(parts)


def _encode_non_ascii(url: str) -> str:
    """把 URL 里的非 ASCII 字节百分号编码（幂等，已编码的 ``%xx`` 不受影响）。

    ArcGIS REST 服务名常含中文（如 ``…/services/江西省县级行政区域/MapServer``）。
    QgsDataSourceUri 连接串内放裸中文虽常能过，但 HTTP 请求需要干净的 ASCII URL；
    这里只编码 ord>127 的字节，已有 ``%E6…`` 保留原样——已编码 / 未编码两种写法
    归一成同一条连接串（QGIS GUI 存连接时同样把中文路径 percent 编码）。
    """
    return re.sub(
        r"[^\x00-\x7F]",
        lambda m: "".join(f"%{b:02X}" for b in m.group(0).encode("utf-8")),
        url,
    )


def build_arcgis_uri(
    d: "OnlineBasemapDef", keys: Optional[MapKeysConfig] = None
) -> str:
    """构造 QGIS arcgismapserver 连接串 ``format='..' layer='..' url='..'``。

    **为什么不是裸服务 URL**：QGIS 4 的 ``arcgismapserver`` provider 收到一条
    ``QgsDataSourceUri`` 参数串（``format=``/``layer=``/``url=``）才会抓服务元数据
    并对 MapServer 走 ``/export`` 动态出图；只传裸 ``…/MapServer`` 时图层 isValid
    但 extent 恒 0、什么都画不出来（QGIS GUI 加 ArcGIS 层时生成的源正是
    ``format='PNG32' layer='0' url='…'`` 形态——矢量面也由服务端栅格化成图）。

    - ``format``：ArcGIS 出图格式（``PNG32`` 默认，QGIS GUI 同款）；
    - ``layer``：MapServer 子图层 id（默认 ``0``，与 QGIS GUI 默认一致；可用
      ``d.layers`` 显式指定）；ImageServer 整服务即影像，不传 ``layer``；
    - ``url``：服务地址（key 占位符已替换 + 非 ASCII 归一，见
      :func:`apply_key_placeholders` / :func:`_encode_non_ascii`）。
    """
    url = _encode_non_ascii(apply_key_placeholders(d.url, keys))
    parts = [f"format='{d.format or 'PNG32'}'"]
    if d.layers:
        parts.append(f"layer='{d.layers}'")
    elif url.rstrip("/").lower().endswith("mapserver"):
        parts.append("layer='0'")
    parts.append(f"url='{url}'")
    return " ".join(parts)


def _arcgis_feature_layer_url(
    d: "OnlineBasemapDef", keys: Optional[MapKeysConfig] = None
) -> str:
    """FeatureServer 服务地址 → 完整子图层地址（key 占位符替换 + 非 ASCII 归一）。

    ``arcgisfeatureserver`` provider 要求图层 id 在 **URL 路径** 里（裸
    ``…/FeatureServer`` 根无效，实测）；故地址已带 ``/FeatureServer/<id>`` 时原样
    使用，否则按 ``d.layers``（缺省 ``0``）追加到路径尾。
    """
    url = _encode_non_ascii(apply_key_placeholders(d.url, keys)).rstrip("/")
    if not re.search(r"/featureserver/\d+$", url, re.IGNORECASE):
        url = f"{url}/{d.layers or '0'}"
    return url


def build_arcgis_feature_uri(
    d: "OnlineBasemapDef", crs: str, keys: Optional[MapKeysConfig] = None
) -> str:
    """构造 QGIS arcgisfeatureserver 连接串 ``crs='..' url='..'``。

    **真矢量**：与 :func:`build_arcgis_uri`（arcgismapserver 服务端栅格化成 PNG）
    不同，FeatureServer 走 ``arcgisfeatureserver`` provider，把要素拉进 QGIS 当
    普通矢量层——「属性」工具等按矢量路径即可识别要素属性。

    - ``crs``：要素层的目标坐标系（QgsDataSourceUri 传单引号包住）。实测**必填**
      ——缺省会让图层 isValid=False；provider 按服务元数据里的源 SR 把要素重投影到
      该 crs（声明值与服务实际 SR 不同也不会错位，只决定图层 CRS 标注），故取
      服务实际 SR（自动识别或手填）最干净；
    - ``url``：完整子图层地址（服务根 + 图层 id 拼进路径，见
      :func:`_arcgis_feature_layer_url`）。
    """
    return f"crs='{crs}' url='{_arcgis_feature_layer_url(d, keys)}'"


def _discover_arcgis_feature_crs(full_url: str) -> Optional[str]:
    """从 FeatureServer 子图层元数据识别服务坐标系（``EPSG:<wkid>``）；失败 ``None``。

    阻塞 GET ``{full_url}?f=json``（:class:`QgsBlockingNetworkRequest`，走 QGIS 网络
    栈、响应超时/连接失败快速返回错误码，不会挂死 GUI），从返回的 layer JSON 取
    ``spatialReference``（部分服务没写顶层、只写在 ``extent.spatialReference`` 里），
    优先 ``latestWkid``（ArcGIS 现行坐标系标识）。ArcGIS 的 Web Mercator 别名
    ``102100/102113`` 归一成 QGIS 认得的标准 ``3857``；无 wkid（本地自定义 SR 等）
    → ``None``（调用方回退手填 crs，不静默用错坐标系）。
    """
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    from qgis.core import QgsBlockingNetworkRequest

    try:
        req = QgsBlockingNetworkRequest()
        code = req.get(QNetworkRequest(QUrl(full_url + "?f=json")))
        if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
            return None
        doc = json.loads(bytes(req.reply().content()).decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("识别 FeatureServer 坐标系失败 url=%s：%s", full_url, exc)
        return None
    sr = doc.get("spatialReference") or (doc.get("extent") or {}).get("spatialReference") or {}
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if wkid is None:
        return None
    try:
        wkid = int(wkid)
    except (TypeError, ValueError):
        return None
    if wkid in (102100, 102113):
        wkid = 3857
    return f"EPSG:{wkid}"


def _wait_until_layer_valid(layer: "QgsVectorLayer", timeout_ms: int = 25000) -> bool:
    """轮询 QGIS 事件循环直到图层有效或超时。

    ``arcgisfeatureserver`` provider **异步加载**：构造后元数据/要素请求挂在网络栈
    上，要等事件循环转动才送达——实测冷网络下 isValid 初始 False、要数秒才变 True，
    热/缓存时构造完即有效。调用方（如图层添加回调）跑在主事件循环的同步槽里，
    期间外层不派发事件，故这里嵌套事件循环轮询（每 200ms）直到 isValid 或超时。
    """
    from qgis.PyQt.QtCore import QEventLoop, QTimer

    step = 200
    waited = 0
    while waited < timeout_ms:
        if layer.isValid():
            return True
        loop = QEventLoop()
        QTimer.singleShot(step, loop.quit)
        loop.exec()
        waited += step
    return layer.isValid()


def build_online_basemap_layer(
    d: "OnlineBasemapDef", keys: Optional[MapKeysConfig] = None
) -> Optional["QgsMapLayer"]:
    """按 ``type`` 构造在线底图图层；失败返回 ``None`` 并记录原因。

    - ``xyz`` → XYZ 瓦片（沿用 :func:`build_base_tile_layer`）；
    - ``arcgis`` → ArcGIS REST MapServer/ImageServer（provider
      ``arcgismapserver``，连接串见 :func:`build_arcgis_uri`——用参数串而不是
      裸 URL，否则 provider 不出图）；
    - ``arcgisfeature`` → ArcGIS REST FeatureServer（provider
      ``arcgisfeatureserver``，连接串见 :func:`build_arcgis_feature_uri`）——
      **真矢量层**，要素可被「属性」工具识别；该 provider 异步加载，构造后要
      轮询事件直到有效（见 :func:`_wait_until_layer_valid`）；坐标系取 ``d.crs``
      或自动从服务元数据识别，拿不到记日志返回 ``None``；
    - ``wms`` / ``wmts`` → OGC 服务（provider ``wms``）。

    非 xyz 图层首次打开会拉 capabilities（联网），此处与 xyz 一致判
    ``isValid()``：无效记日志返回 ``None``，由上层弹「加载失败」。整段包
    try/except（离网/服务异常时构造本身也可能抛）。

    url 里的 key 占位符（``{tk}``）在此用 ``keys`` 替换成真值——这是唯一把
    key 应用到图层的边界；config 模型里 url 恒为占位符形态（否则模板 vs
    配置的 url 判等会失配，见 :func:`apply_key_placeholders`）。
    """
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    try:
        if d.type == "arcgisfeature":
            full_url = _arcgis_feature_layer_url(d, keys)
            crs = d.crs or _discover_arcgis_feature_crs(full_url)
            if not crs:
                logger.warning(
                    "arcgisfeature 图层无法确定坐标系（自定义底图里手填坐标系即可）"
                    " url=%s",
                    _mask_url(full_url),
                )
                return None
            layer = QgsVectorLayer(
                build_arcgis_feature_uri(d, crs, keys), d.name, "arcgisfeatureserver"
            )
            if not _wait_until_layer_valid(layer):
                logger.warning(
                    "arcgisfeature 图层加载失败/超时 type=%s url=%s error=%s",
                    d.type,
                    _mask_url(full_url),
                    layer.error().message(),
                )
                return None
            return layer
        if d.type == "arcgis":
            layer = QgsRasterLayer(
                build_arcgis_uri(d, keys), d.name, "arcgismapserver"
            )
        elif d.type == "wms":
            layer = QgsRasterLayer(build_wms_uri(d, keys), d.name, "wms")
        elif d.type == "wmts":
            layer = QgsRasterLayer(build_wmts_uri(d, keys), d.name, "wms")
        elif d.type == "xyz":
            return build_base_tile_layer(d, keys)
        else:
            logger.warning(
                "未知在线底图类型 type=%s name=%s", d.type, getattr(d, "name", "?")
            )
            return None
        if not layer.isValid():
            logger.warning(
                "%s 底图图层无效 type=%s url=%s error=%s",
                d.type,
                d.type,
                _mask_url(d.url),
                layer.error().message(),
            )
            return None
        return layer
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "构建在线底图图层失败 type=%s name=%s：%s",
            getattr(d, "type", "?"),
            getattr(d, "name", "?"),
            exc,
        )
        return None


def add_base_tile_layer(
    map_cfg: MapConfig, project: Optional["QgsProject"] = None
) -> Optional["QgsRasterLayer"]:
    """把配置里的底图加进项目。返回图层；未启用 / 失败返回 ``None``。"""
    if map_cfg.base_tile is not None and map_cfg.base_tile.referer:
        # 连接串的 referer= 在部分 QGIS 版本对 xyz 不生效，用全局请求预处理兜底：
        # 发往该瓦片服务的所有请求强制带 Referer 头（见模块 docstring 的"坑 2"）。
        install_referer_preprocessor(map_cfg.base_tile.referer)
    layer = build_base_tile_layer(map_cfg.base_tile, map_cfg.keys)
    if layer is not None:
        from qgis.core import QgsProject

        (project or QgsProject.instance()).addMapLayer(layer)
        logger.info("已加载 XYZ 底图：%s", layer.name())
    return layer


def install_referer_preprocessor(referer: str) -> None:
    """公开入口：给发往该瓦片服务（当前仅天地图）的请求装全局 Referer 头。

    在线底图 Dock 手动添加带 referer 的图层时也需要它——连接串 ``referer=``
    对 xyz 瓦片在部分 QGIS 版本不生效（见模块 docstring 的"坑 2"）。
    """
    _install_referer_preprocessor(referer)


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


__all__ = [
    "add_base_tile_layer",
    "apply_key_placeholders",
    "build_arcgis_feature_uri",
    "build_arcgis_uri",
    "build_base_tile_layer",
    "build_online_basemap_layer",
    "build_wms_uri",
    "build_wmts_uri",
    "build_xyz_uri",
    "canonical_basemap_url",
    "install_referer_preprocessor",
]
