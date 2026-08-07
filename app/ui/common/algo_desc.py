"""算法描述工具：给工具树 / 参数对话框右侧展示算法说明。

描述来源（按优先级）：
1. QGIS processing 注册表动态取真实算法帮助（``displayName`` + ``shortHelpString``，
   即 QGIS 官方中文说明）；
2. 注册表不可用（如 processing 未初始化）时回退到仓库内兜底文案。

提供 :func:`algorithm_description_html` 返回 HTML 描述串，以及
:func:`make_description_browser` 返回配置好的 :class:`QTextBrowser`（复用
``TEXT_BROWSER_QSS`` 样式），工具树与对话框共用同一套展示。
"""

from __future__ import annotations

from PyQt5.QtWidgets import QTextBrowser

from app.infra.logging import get_logger
from app.ui.common.styles import TEXT_BROWSER_QSS

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
    ),
    "overlay:analysis": (
        "<h2>叠加分析</h2>"
        "<p>对两个矢量图层执行几何叠加运算，每个运算生成一个独立结果图层。"
        "结果图层的属性表同时携带输入与叠加图层的字段。</p>"
        "<p><b>交集</b>：仅保留两个图层重叠的部分。</p>"
        "<p><b>并集</b>：合并全部几何，重叠区域分割为独立要素。</p>"
        "<p><b>差集</b>：输入图层去掉与叠加图层重叠的部分。</p>"
        "<p><b>对称差</b>：两个图层不重叠的部分。</p>"
        "<p>可同时勾选多种运算；输出图层名为前缀加运算名，自动加入工程。</p>"
    ),
}

#: 根/分组节点（无算法 code）时的提示文案
HINT_HTML = (
    "<h2>分析工具</h2>"
    "<p style='color:#888'>在左侧选择工具查看算法说明，点击即可打开参数对话框。</p>"
)


def algorithm_description_html(code: str) -> str:
    """取算法的中文描述（HTML）：processing 注册表优先，兜底文案其次。"""
    if not code:
        return HINT_HTML
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
    """构造展示指定算法描述的 :class:`QTextBrowser`（只读 + 仓库样式）。"""
    browser = QTextBrowser()
    browser.setOpenExternalLinks(True)
    browser.setStyleSheet(TEXT_BROWSER_QSS)
    browser.setHtml(algorithm_description_html(code))
    return browser


__all__ = ["algorithm_description_html", "make_description_browser", "HINT_HTML"]
