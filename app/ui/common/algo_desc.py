"""算法描述工具：给工具树 / 参数对话框右侧展示算法说明。

描述来源（按优先级）：
1. QGIS processing 注册表动态取真实算法帮助（``displayName`` + ``shortHelpString``，
   即 QGIS 官方中文说明）；
2. 注册表不可用（如 processing 未初始化）时回退到仓库内兜底文案。

提供 :func:`algorithm_description_html` 返回 HTML 描述串，以及
:func:`make_description_browser` 返回配置好的 :class:`QTextBrowser`
（配色由全局 ``app_qss`` 统一负责），工具树与对话框共用同一套展示。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QTextBrowser

from app.infra.logging import get_logger
from app.ui.common.theme import THEMES, ThemeManager

logger = get_logger(__name__)

#: processing 注册表不可用时的兜底算法描述（中文）
_ALGO_FALLBACK_DESC = {
    "native:writevectortiles_xyz": (
        "<h2>生成矢量瓦片（XYZ）</h2>"
        "<p>导出一个或多个矢量图层到矢量瓦片格式，为快速地图渲染和小数据量"
        "传输而优化。</p>"
        "<p>输出为 <b>.pbf</b>（Protocol Buffer）瓦片，按 <b>{z}/{x}/{y}</b> "
        "金字塔目录组织，可直接作为矢量瓦片服务的数据源。</p>"
        "<p>可设置最小/最大缩放等级、输出范围与文件模版。</p>"
    ),
    "native:buffer": (
        "<h2>矢量缓冲区</h2>"
        "<p>在矢量要素周围创建指定距离的缓冲区多边形。</p>"
        "<p>距离可按所选单位输入（米、千米、英尺、度等），执行时会自动换算为"
        "<b>图层坐标系单位</b>（投影坐标系为米，地理坐标系为度），距离必须大于 0。</p>"
        "<p>可设置圆弧分段数、端点样式（圆 / 平 / 方）、连接样式"
        "（圆 / 斜接 / 平切），并可选择融合重叠的缓冲结果。</p>"
        "<p>可填写<b>输出文件路径</b>（GeoPackage / Shapefile）把结果保存到文件；"
        "留空则生成临时图层。</p>"
    ),
    "overlay:analysis": (
        "<h2>叠加分析</h2>"
        "<p>对两个矢量图层执行几何叠加运算，每个运算生成一个独立结果图层。"
        "结果图层的属性表同时携带输入与叠加图层的字段。</p>"
        "<p><b>交集</b>：仅保留两个图层重叠的部分。</p>"
        "<p><b>并集</b>：合并全部几何，重叠区域分割为独立要素。</p>"
        "<p><b>差集</b>：输入图层去掉与叠加图层重叠的部分。</p>"
        "<p><b>对称差</b>：两个图层不重叠的部分。</p>"
        "<p>可同时勾选多种运算；输出图层名为前缀加运算名，自动加入工程。"
        "可填写<b>输出目录</b>把每个结果保存为 GeoPackage 文件；留空则生成临时图层。</p>"
    ),
    "native:shortestpathpointtopoint": (
        "<h2>最短路径（点到点）</h2>"
        "<p>在<b>线图层</b>构成的网络（道路、管网等）上，计算从起点到终点的"
        "最短/最快路径，并输出一条路径线图层。</p>"
        "<p><b>路径类型</b>：最短距离按网络边长累加；最快时间按速度折算耗时"
        "（有速度字段用字段值，否则用默认速度）。</p>"
        "<p><b>方向字段</b>用于表达单行线：字段值匹配「顺向值/逆向值/双向值」时"
        "该线段只能单向/双向通行；未设方向字段时按「默认方向」处理。</p>"
        "<p>起点/终点可从画布点击选择，也可直接输入 <b>x, y</b> 坐标；"
        "结果自动加入工程，并提示总代价。可填写<b>输出文件路径</b>"
        "（GeoPackage / Shapefile）保存到文件；留空则生成临时图层。</p>"
    ),
    "native:reprojectlayer": (
        "<h2>重投影图层</h2>"
        "<p>把矢量图层转换到另一个<b>坐标系（CRS）</b>，几何坐标按目标坐标系"
        "重新计算，属性保持不变。用于把数据统一到工程坐标系，或在不同坐标系"
        "间转换（如 WGS84 经纬度 → Web Mercator 投影）。</p>"
        "<p>选择<b>输入图层</b>与<b>目标坐标系</b>即可；结果自动加入工程。"
        "可填写<b>输出文件路径</b>（GeoPackage / Shapefile）保存到文件；"
        "留空则生成临时图层。</p>"
    ),
    "native:tilesxyzdirectory": (
        "<h2>影像切片（XYZ）</h2>"
        "<p>把<b>当前地图画布内容</b>渲染为 XYZ 瓦片目录，输出按 "
        "<b>{z}/{x}/{y}.png</b> 组织，可直接作为地图服务的瓦片数据源。</p>"
        "<p>切片范围取<b>所选影像图层的范围</b>；切出的内容即画布上可见的"
        "所有图层（含底图、矢量叠加等）。</p>"
        "<p><b>缩放等级</b>：设置最小 / 最大缩放级别，级别越大切片越细、数量越多。</p>"
        "<p><b>瓦片格式</b>：PNG（无损、适合栅格影像）或 JPG（有损、体积更小）。"
        "可设置瓦片边长与抗锯齿。</p>"
        "<p><b>网页查看器</b>：在输出目录生成一个 Leaflet HTML 页面，可直接打开"
        "浏览瓦片结果。</p>"
        "<p>大范围高缩放的切片耗时较长，任务在后台运行，可随时取消；"
        "完成时提示输出目录。</p>"
    ),
    "native:tilesxyzmbtiles": (
        "<h2>影像切片（MBTiles）</h2>"
        "<p>把<b>当前地图画布内容</b>渲染为单个 <b>MBTiles</b> 文件（SQLite "
        "封装的 XYZ 瓦片金字塔），便于<b>分发</b>与离线使用——一个文件即全部"
        "瓦片。</p>"
        "<p>切片范围取<b>所选影像图层的范围</b>；切出的内容即画布上可见的"
        "所有图层（含底图、矢量叠加等）。</p>"
        "<p><b>缩放等级</b>：设置最小 / 最大缩放级别，级别越大切片越细、数量越多。</p>"
        "<p><b>瓦片格式</b>：PNG（无损、适合栅格影像）或 JPG（有损、体积更小）。"
        "MBTiles 内部固定 256×256 瓦片、按 TMS 规范存储。</p>"
        "<p><b>网页查看器</b>：在输出文件旁生成一个 Leaflet HTML 页面"
        "（``{文件名}_preview/`` 目录，浏览器读不了 SQLite 格式的 mbtiles，"
        "故把瓦片解成目录后引用），双击即可预览切片结果。</p>"
        "<p>大范围高缩放的切片耗时较长，任务在后台运行，可随时取消；"
        "完成时提示输出文件路径。</p>"
    ),
}

#: 根/分组节点（无算法 code）时的提示文案。颜色走当前主题（次要文字色），
#: 不再硬编码；``HINT_HTML`` 常量保留浅色主题渲染结果，兼容历史 import。
def _hint_html() -> str:
    t = ThemeManager.instance().current()
    return (
        "<h2>分析工具</h2>"
        f"<p style='color:{t.text_secondary}'>"
        "在左侧选择工具查看算法说明，点击即可打开参数对话框。</p>"
    )


HINT_HTML = (
    "<h2>分析工具</h2>"
    f"<p style='color:{THEMES['light'].text_secondary}'>"
    "在左侧选择工具查看算法说明，点击即可打开参数对话框。</p>"
)


def algorithm_description_html(code: str) -> str:
    """取算法的中文描述（HTML）：processing 注册表优先，兜底文案其次。"""
    if not code:
        return _hint_html()
    try:
        from qgis.core import QgsApplication

        registry = QgsApplication.processingRegistry()
        alg = registry.algorithmById(code)
        if alg is not None:
            name = alg.displayName()
            help_html = alg.shortHelpString()
            if help_html:
                title = f"<h2>{name}</h2>" if name else ""
                return title + help_html
    except Exception:  # noqa: BLE001
        logger.warning("读取算法描述失败 code=%s，使用兜底文案", code, exc_info=True)
    return _ALGO_FALLBACK_DESC.get(code, f"<p>{code}</p>")


def make_description_browser(code: str) -> QTextBrowser:
    """构造展示指定算法描述的 :class:`QTextBrowser`（只读；配色走 app_qss）。"""
    browser = QTextBrowser()
    browser.setOpenExternalLinks(True)
    browser.setHtml(algorithm_description_html(code))
    return browser


__all__ = ["algorithm_description_html", "make_description_browser", "HINT_HTML"]
