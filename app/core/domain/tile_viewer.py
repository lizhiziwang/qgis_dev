"""瓦片网页查看器：生成完全离线的 Leaflet 预览页。

此前查看器从 unpkg 加载 Leaflet，且矢量瓦片用 XHR 读 ``file://`` 下的 pbf，
浏览器会同时遇到「CDN 打不开」和「跨源请求被拦」两类错误，页面只剩空白。
现在：

* Leaflet / Leaflet.VectorGrid 随应用打包，生成查看器时复制到输出目录的
  ``_viewer_assets/`` 下，页面用相对路径引用，完全离线可用；
* 矢量瓦片由应用通过本机 HTTP 服务打开（见
  :mod:`app.infra.viewer_server`），避免 ``file://`` 的 CORS 限制；
* 矢量样式键使用 **pbf 里真实的图层名**（由
  :func:`app.core.domain.tiles.inspect.inspect_vector_tiles` 解析），不再靠
  猜测 ``layer.name()``。
"""

from __future__ import annotations

import json
import os
import shutil
from html import escape
from pathlib import Path
from typing import Iterable, Sequence

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 单个输出目录内放置静态资源的子目录
ASSET_DIR_NAME = "_viewer_assets"
_LEAFLET_DIR = "leaflet"
_LEAFLET_FILES = ("leaflet.js", "leaflet.css")
_VECTORGRID_FILE = "Leaflet.VectorGrid.bundled.js"

#: 找不到打包资源时的兜底（有网可用；离线场景应保证资源目录存在）
_CDN_LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
_CDN_LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
_CDN_VECTORGRID_JS = (
    "https://unpkg.com/leaflet.vectorgrid@1.3.0/dist/Leaflet.VectorGrid.bundled.js"
)


def _bundled_leaflet_dir() -> Path:
    """应用内 Leaflet 资源目录：``app/resources/web/leaflet``。"""
    return Path(__file__).resolve().parents[2] / "resources" / "web" / _LEAFLET_DIR


def copy_viewer_assets(output_dir: str, *, vector: bool = False) -> str:
    """把 Leaflet 资源复制到 ``output_dir/_viewer_assets/leaflet``。

    返回资源目录的**相对路径**（供 HTML 引用）；打包资源缺失时返回空串，
    调用方会退回 CDN 地址（页面仍有网可用）。
    """
    src = _bundled_leaflet_dir()
    if not src.is_dir():
        logger.warning("应用内 Leaflet 资源缺失：%s", src)
        return ""
    dest = Path(output_dir) / ASSET_DIR_NAME / _LEAFLET_DIR
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for name in _LEAFLET_FILES:
            shutil.copy2(src / name, dest / name)
        images = src / "images"
        if images.is_dir():
            shutil.copytree(images, dest / "images", dirs_exist_ok=True)
        if vector and (src / _VECTORGRID_FILE).is_file():
            shutil.copy2(src / _VECTORGRID_FILE, dest / _VECTORGRID_FILE)
    except OSError:
        logger.warning("复制查看器静态资源失败：%s → %s", src, dest, exc_info=True)
        return ""
    return f"{ASSET_DIR_NAME}/{_LEAFLET_DIR}"


def _asset_urls(output_dir: str, *, vector: bool) -> tuple[str, list[str]]:
    """返回 ``(css_href, [js_src...])``：优先本地资源，缺失时退回 CDN。"""
    prefix = copy_viewer_assets(output_dir, vector=vector)
    if prefix:
        css = f"{prefix}/leaflet.css"
        scripts = [f"{prefix}/leaflet.js"]
        if vector:
            scripts.append(f"{prefix}/{_VECTORGRID_FILE}")
        return css, scripts
    scripts = [_CDN_LEAFLET_JS]
    if vector:
        scripts.append(_CDN_VECTORGRID_JS)
    return _CDN_LEAFLET_CSS, scripts


def _json(value: object) -> str:
    """JSON 序列化并防止 ``</script>`` 提前闭合脚本。"""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def _normalise_template(template: str, default: str) -> str:
    text = str(template or default).strip().replace("\\", "/")
    return text.lstrip("./") or default


def _start_zoom(min_zoom: int, max_zoom: int) -> int:
    """无有效范围时的初始级别：取中间级别，至少 1 级。"""
    lo, hi = max(0, int(min_zoom)), max(0, int(max_zoom))
    zoom = (lo + hi) // 2 if hi > lo else hi
    return max(1, zoom)


def write_tile_viewer(
    output_dir: str,
    *,
    title: str = "",
    tile_template: str,
    min_zoom: int = 0,
    max_zoom: int = 8,
    bounds: Sequence[float] | None = None,
    vector: bool = False,
    layer_names: Iterable[str] = (),
) -> str:
    """在 ``output_dir/index.html`` 写一个离线 Leaflet 查看器，返回路径。

    :param tile_template: 相对瓦片路径模板，如 ``{z}/{x}/{y}.png``。
    :param bounds: WGS84 ``(west, south, east, north)``；无效时按中心定位。
    :param vector: ``True`` 时加载 Leaflet.VectorGrid 并渲染 pbf。
    :param layer_names: vector 模式下 pbf 内的真实图层名（样式键）。
    """
    os.makedirs(output_dir, exist_ok=True)
    css_href, scripts = _asset_urls(output_dir, vector=vector)
    template = _normalise_template(
        tile_template, "{z}/{x}/{y}.pbf" if vector else "{z}/{x}/{y}.png"
    )
    west, south, east, north = [float(v) for v in (bounds or (0, 0, 0, 0))]
    valid_bounds = east > west and north > south
    bounds_list = [west, south, east, north] if valid_bounds else [0.0, 0.0, 0.0, 0.0]
    center_lat, center_lon = (south + north) / 2.0, (west + east) / 2.0
    zoom = _start_zoom(min_zoom, max_zoom)
    page_title = escape(
        str(title or os.path.basename(str(output_dir).rstrip("\\/")) or "瓦片预览")
    )
    names = [str(name) for name in layer_names if str(name).strip()]
    max_zoom = max(0, int(max_zoom))
    min_zoom = max(0, int(min_zoom))
    # 地图允许继续放大，瓦片层用 maxNativeZoom 做缩放；矢量瓦片不做原生缩放。
    map_max_zoom = max_zoom if vector else max(max_zoom, 18)

    script_srcs = "".join(f'<script src="{escape(src)}"></script>' for src in scripts)
    style_block = (
        "body{margin:0;background:#f4f5f7}"
        "html,body,#map{width:100%;height:100%}"
        "#err{position:absolute;z-index:9999;left:12px;top:12px;max-width:560px;"
        "padding:8px 10px;background:#fff3cd;border:1px solid #d6b656;border-radius:4px;"
        "font:13px/1.6 sans-serif;color:#664d03;display:none;white-space:pre-line}"
    )
    if vector:
        body_script = (
            f"var template={_json(template)};"
            f"var names={_json(names)};"
            f"var bounds={_json(bounds_list)};"
            "var map=L.map('map',{attributionControl:false});"
            f"map.setView([{center_lat:.6f},{center_lon:.6f}],{zoom});"
            "var styleFn=function(){return {weight:1,color:'#1f78b4',"
            "fillColor:'#1f78b4',fillOpacity:0.25,radius:3};};"
            "var styles={};names.forEach(function(n){styles[n]=styleFn;});"
            "function showErr(msg){var el=document.getElementById('err');"
            "el.textContent=msg;el.style.display='block';}"
            "if(location.protocol==='file:'){showErr('矢量瓦片通过 file:// 打开时，"
            "浏览器会拦截 pbf 请求。请从应用里点「打开网页查看器」，"
            "或在本目录运行 python -m http.server 后用 HTTP 访问。');}"
            "try{"
            "if(!names.length){showErr('切片完成，但瓦片中没有要素，无法预览。"
            "请提高「最大等级」后重新生成（面/线要素通常需要 14 级或更高）。');}"
            "else{"
            "L.vectorGrid.protobuf(template,{"
            f"minZoom:{min_zoom},maxZoom:{max_zoom},tms:false,tolerance:0,"
            "vectorTileLayerStyles:styles}).addTo(map);}"
            "if(bounds[2]>bounds[0]&&bounds[3]>bounds[1]){"
            f"map.fitBounds([[bounds[1],bounds[0]],[bounds[3],bounds[2]]],"
            f"{{maxZoom:{map_max_zoom}}});}}"
            "}"  # 关闭 try
            "catch(e){showErr('矢量瓦片加载失败：'+(e&&e.message?e.message:e));"
            "console.error(e);}"
        )
    else:
        body_script = (
            f"var template={_json(template)};"
            f"var bounds={_json(bounds_list)};"
            "var map=L.map('map',{attributionControl:false});"
            f"map.setView([{center_lat:.6f},{center_lon:.6f}],{zoom});"
            # noWrap：全球范围在低级别下 Leaflet 默认会把世界横向重复铺满视口，
            # 看起来像「重叠了多份地图」；禁止循环后只显示一份。
            # 不设 attribution：预览页不要「Created by QGIS」水印。
            "var tileOpts={"
            f"minZoom:{min_zoom},maxZoom:{map_max_zoom},"
            f"minNativeZoom:{min_zoom},maxNativeZoom:{max_zoom},"
            "tms:false,noWrap:true};"
            "if(bounds[2]>bounds[0]&&bounds[3]>bounds[1]){"
            "tileOpts.bounds=[[bounds[1],bounds[0]],[bounds[3],bounds[2]]];}"
            "L.tileLayer(template,tileOpts).addTo(map);"
            "if(bounds[2]>bounds[0]&&bounds[3]>bounds[1]){"
            f"map.fitBounds([[bounds[1],bounds[0]],[bounds[3],bounds[2]]],"
            f"{{maxZoom:{map_max_zoom}}});}}"
        )

    html_path = os.path.join(str(output_dir), "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"/>"
            f"<title>{page_title}</title>"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
            f'<link rel="stylesheet" href="{escape(css_href)}"/>'
            f"{script_srcs}"
            f"<style>{style_block}</style>"
            "</head><body><div id=\"map\"></div><div id=\"err\"></div>"
            f"<script>{body_script}</script></body></html>"
        )
    return html_path


__all__ = ["ASSET_DIR_NAME", "copy_viewer_assets", "write_tile_viewer"]
