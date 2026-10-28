"""算法描述工具：给工具树 / 参数对话框右侧展示算法说明。

描述来源（按优先级）：
1. QGIS processing 注册表动态取真实算法帮助（``displayName`` + ``shortHelpString``，
   即 QGIS 官方中文说明）；
2. 注册表不可用（如 processing 未初始化）时回退到仓库内兜底文案。

提供 :func:`algorithm_description_html` 返回 HTML 描述串，
:func:`make_description_browser` 返回配置好的 :class:`QTextBrowser`
（配色由全局 ``app_qss`` 统一负责），:func:`make_description_panel` 在浏览器
外套一层可折叠的「帮助说明」面板；工具树与对话框共用同一套展示。
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.infra.logging import get_logger
from app.ui.common.processing_zh import algorithm_title
from app.ui.common.theme import THEMES, ThemeManager

logger = get_logger(__name__)

#: GDAL 算法中文兜底描述。QGIS 4.2 在本环境下 ``shortHelpString()`` 对所有
#: ``gdal:*`` 都返回空字符串（注册表能拿到 alg，displayName 对，但 help 文本
#: 没装上），所以这里手工补各算法的中文简介，避免对话框右侧展示「裸 code」。
#: 描述尽量与算法语义对齐，按 ``tools_dock`` 的分组顺序排版。
_GDAL_FALLBACK_DESC: dict[str, str] = {
    # ---- GDAL 栅格分析（18） ----
    "gdal:rastercalculator": (
        "<h2>栅格计算器</h2>"
        "<p>对输入栅格波段做表达式运算（如 <code>A + B</code> / <code>A * 2</code>），"
        "支持 + / - / * / / / 逻辑 / 比较运算与常用数学函数（sin/cos/log/...）。</p>"
        "<p>可对多个波段组合运算，结果写出为新栅格（GeoTIFF 等）。</p>"
    ),
    "gdal:cliprasterbyextent": (
        "<h2>按范围裁剪栅格</h2>"
        "<p>按指定的矩形范围（xmin/xmax/ymin/ymax）裁剪栅格图层，输出只保留"
        "与范围相交的部分。</p>"
        "<p>适合按行政区 / 测区边界裁出目标区域。</p>"
    ),
    "gdal:cliprasterbymasklayer": (
        "<h2>按掩膜裁剪栅格</h2>"
        "<p>用矢量多边形图层作为掩膜裁剪栅格——输出栅格只保留掩膜覆盖的区域，"
        "外部置为 NoData。</p>"
        "<p>比「按范围裁剪」更精确，适合按不规则行政区 / 项目边界裁剪。</p>"
    ),
    "gdal:warpreproject": (
        "<h2>重投影（栅格）</h2>"
        "<p>把栅格从源 CRS 重投影到目标 CRS，使用指定的重采样方法（最近邻 /"
        "双线性 / 三次卷积等）。</p>"
        "<p>适合把影像统一到工程坐标系，或跨坐标系拼接前对齐。</p>"
    ),
    "gdal:translate": (
        "<h2>格式转换（栅格）</h2>"
        "<p>把栅格从一种格式（GeoTIFF / Erdas Imagine / JPEG2000 等）转换为"
        "另一种，可同时调整输出 SRS、子集范围、波段子集、缩放比例、NoData 等。</p>"
    ),
    "gdal:merge": (
        "<h2>拼接栅格</h2>"
        "<p>把多个相邻 / 重叠的栅格图层拼接为单个图层，可处理像素重叠"
        "（取最后输入 / 最前输入 / 最小值 / 最大值 / 求和）。</p>"
        "<p>适合把分幅影像合并为整幅。</p>"
    ),
    "gdal:rearrange_bands": (
        "<h2>重排波段</h2>"
        "<p>重排 / 选择栅格的波段顺序并写出，可用于把多波段影像重排为 RGB+α "
        "或提取指定波段。</p>"
    ),
    "gdal:pcttorgb": (
        "<h2>PCT 转 RGB</h2>"
        "<p>把带调色板（color table）的单波段栅格展开为 RGB 三波段栅格。</p>"
    ),
    "gdal:rgbtopct": (
        "<h2>RGB 转 PCT</h2>"
        "<p>把 RGB 多波段栅格压缩为单波段 + 调色板（color table），减小文件体积。</p>"
    ),
    "gdal:createcog": (
        "<h2>创建 COG</h2>"
        "<p>创建 Cloud-Optimized GeoTIFF（COG）：内嵌瓦片、金字塔、压缩，"
        "适合云端 / 网络流式读取。</p>"
    ),
    "gdal:buildvirtualraster": (
        "<h2>构建虚拟栅格</h2>"
        "<p>不实际拼接文件，生成一个 <code>.vrt</code> 虚拟栅格（XML 描述），"
        "把多个栅格当作一个图层来用——后续算法直接读取该虚拟源。</p>"
        "<p>输出轻量、无损、可作为流式数据源。</p>"
    ),
    "gdal:polygonize": (
        "<h2>栅格矢量化</h2>"
        "<p>把栅格像元按相同值聚合为多边形要素，结果矢量图层携带像素值字段。</p>"
        "<p>适合分类图（土地利用）转矢量面。</p>"
    ),
    "gdal:sieve": (
        "<h2>筛除小图斑</h2>"
        "<p>移除栅格中面积小于阈值的连通区域，用相邻最大面积像素值填充，"
        "常用于分类图去噪。</p>"
    ),
    "gdal:rasterize": (
        "<h2>矢量转栅格</h2>"
        "<p>把矢量要素（点 / 线 / 面）转成栅格，像素值取自要素属性字段。</p>"
        "<p>适合把行政边界 / 站点等矢量转成栅格参与空间分析。</p>"
    ),
    "gdal:rasterize_over": (
        "<h2>属性值栅格化（覆盖）</h2>"
        "<p>与「矢量转栅格」类似，但仅写入要素覆盖到的像素，其余像素保持不变。</p>"
    ),
    "gdal:rasterize_over_fixed_value": (
        "<h2>固定值栅格化（覆盖）</h2>"
        "<p>把矢量要素覆盖的像素都写为同一固定值（不读属性字段），适合临时打"
        "掩膜 / 标记区域。</p>"
    ),
    "gdal:fillnodata": (
        "<h2>填充 NoData</h2>"
        "<p>用周围像素插值（距离反比 / 最近邻）填充栅格中的 NoData 空洞，"
        "适合修补扫描图边缘 / 去黑边后的白边。</p>"
    ),
    "gdal:nearblack": (
        "<h2>去黑边</h2>"
        "<p>自动识别影像边缘接近全黑 / 全白的像素并替换为 NoData，"
        "裁出实际有数据的范围。</p>"
        "<p>对遥感影像特别有用，去除扫描黑边后再做拼接。</p>"
    ),
    # ---- GDAL 栅格地形（11） ----
    "gdal:aspect": (
        "<h2>坡向</h2>"
        "<p>基于 DEM 计算每个像素的坡向（0-360°，正北为 0，顺时针）。</p>"
        "<p>用于地形分析、太阳辐射建模、土壤水分等专题。</p>"
    ),
    "gdal:slope": (
        "<h2>坡度</h2>"
        "<p>基于 DEM 计算每个像素的坡度（0-90° 或百分比）。</p>"
        "<p>用于地形分类、滑坡易发性分析等。</p>"
    ),
    "gdal:hillshade": (
        "<h2>山体阴影</h2>"
        "<p>基于 DEM 与指定光源（默认 315° 西北、45° 高程角）生成山体阴影栅格，"
        "适合做地形底图叠加。</p>"
    ),
    "gdal:roughness": (
        "<h2>粗糙度</h2>"
        "<p>基于 DEM 计算地形粗糙度（像素邻域高程差异），用于地形复杂度分析。</p>"
    ),
    "gdal:tpitopographicpositionindex": (
        "<h2>TPI 地形位置指数</h2>"
        "<p>Topographic Position Index——像素高程与邻域平均高程之差，正值表示"
        "山脊 / 山顶，负值表示山谷 / 凹地。</p>"
    ),
    "gdal:triterrainruggednessindex": (
        "<h2>TRI 地表 ruggedness</h2>"
        "<p>Terrain Ruggedness Index——邻域高程均方差，表示地表崎岖程度，"
        "适合做地形复杂度分级。</p>"
    ),
    "gdal:proximity": (
        "<h2>距离栅格</h2>"
        "<p>对栅格中的目标像素（值非 0）计算每个像素到最近目标的距离，"
        "可按距离生成缓冲区 / 影响范围。</p>"
    ),
    "gdal:colorrelief": (
        "<h2>彩色渲染</h2>"
        "<p>把单波段 DEM / 数值栅格按指定色阶文件（文本格式：值 → R,G,B）"
        "渲染为 RGB 彩色影像，适合做高程 / 数值可视化。</p>"
    ),
    "gdal:contour": (
        "<h2>等高线</h2>"
        "<p>基于 DEM 按指定等高距生成等高线（线矢量），每条线带高程字段。</p>"
    ),
    "gdal:contour_polygon": (
        "<h2>等高线多边形</h2>"
        "<p>基于 DEM 按指定等高距生成等高线多边形（面矢量），可用于高程分级填色。</p>"
    ),
    "gdal:viewshed": (
        "<h2>可视域</h2>"
        "<p>基于 DEM 与观察点坐标（x, y, 高程）计算可视域栅格（每个像素是否可见），"
        "适合做通信塔 / 瞭望台选址。</p>"
    ),
    # ---- GDAL 栅格数据源（8） ----
    "gdal:overviews": (
        "<h2>构建金字塔</h2>"
        "<p>为栅格生成金字塔（overviews / 概览），加快大影像的缩放浏览速度。</p>"
    ),
    "gdal:gdalinfo": (
        "<h2>栅格信息</h2>"
        "<p>输出栅格的格式 / 大小 / 坐标系 / 范围 / 波段 / NoData 等元信息，"
        "适合排错时查看数据细节。</p>"
    ),
    "gdal:gridaverage": (
        "<h2>Grid 移动平均</h2>"
        "<p>对散点（带值）做移动平均插值生成栅格——窗口内取均值，适合含噪散点。</p>"
    ),
    "gdal:griddatametrics": (
        "<h2>Grid 数据度量</h2>"
        "<p>对散点窗口内做统计度量（最大值 / 最小值 / 范围 / 总和 / 均值 / 中位数 / "
        "标准差），可生成多张度量栅格。</p>"
    ),
    "gdal:gridinversedistance": (
        "<h2>Grid IDW（幂）</h2>"
        "<p>对散点做反距离加权插值生成栅格，权重按距离的幂次衰减，幂值越大近点"
        "权重越高。</p>"
    ),
    "gdal:gridinversedistancenearestneighbor": (
        "<h2>Grid IDW（最近邻搜索）</h2>"
        "<p>反距离加权插值，仅使用每个插值点最近的 N 个散点做插值，比"
        "「Grid IDW（幂）」更快，适合大数据量。</p>"
    ),
    "gdal:gridlinear": (
        "<h2>Grid 线性插值</h2>"
        "<p>对散点做线性插值生成栅格，结果平滑，适合规则分布的散点。</p>"
    ),
    "gdal:gridnearestneighbor": (
        "<h2>Grid 最近邻</h2>"
        "<p>对散点做最近邻插值生成栅格，每个像素值取最近散点的值，"
        "适合分类变量或保留原始采样。</p>"
    ),
    "gdal:convertformat": (
        "<h2>格式转换</h2>"
        "<p>OGR / GDAL 通用格式转换——栅格或矢量都可转换为指定输出格式，"
        "选项按目标驱动调整。</p>"
    ),
    # ---- GDAL 栅格元数据（2） ----
    "gdal:assignprojection": (
        "<h2>指派投影</h2>"
        "<p>给栅格直接写入 / 指派坐标系（不改几何），适合已知正确 CRS 但文件没记录的"
        "情况。</p>"
        "<p><b>注意</b>：不会重投影几何，仅写 CRS 字段。</p>"
    ),
    "gdal:extractprojection": (
        "<h2>提取投影</h2>"
        "<p>把栅格内嵌的坐标系信息（CRS、Geotransform、GCP 等）提取出来，"
        "输出到控制台或单独文本。</p>"
    ),
    "gdal:dataset_identify": (
        "<h2>数据集识别</h2>"
        "<p>识别栅格的格式、驱动、像素深度、波段数等元信息，"
        "用于诊断未知栅格。</p>"
    ),
    # ---- GDAL 矢量分析（9） ----
    "gdal:dissolve": (
        "<h2>融合</h2>"
        "<p>按指定字段把多边形融合（dissolve）——同字段值合并为单个要素，"
        "可对几何相交的多边形按字段聚合。</p>"
    ),
    "gdal:offsetcurve": (
        "<h2>偏移线</h2>"
        "<p>对线要素按法线方向偏移指定距离，正值向左 / 负值向右（取决于几何方向），"
        "常用于道路边线 / 管线偏移。</p>"
    ),
    "gdal:onesidebuffer": (
        "<h2>单边缓冲区</h2>"
        "<p>对线要素只做一侧缓冲区（指定左 / 右），适合道路单侧分析、河流一侧提取等。</p>"
    ),
    "gdal:clipvectorbyextent": (
        "<h2>按范围裁剪矢量</h2>"
        "<p>按矩形范围裁剪矢量图层，仅保留与范围相交的部分。</p>"
    ),
    "gdal:clipvectorbypolygon": (
        "<h2>按多边形裁剪矢量</h2>"
        "<p>用多边形图层作为掩膜裁剪矢量——只保留掩膜覆盖内的要素。</p>"
    ),
    "gdal:executesql": (
        "<h2>OGR 执行 SQL</h2>"
        "<p>对矢量数据源执行 SQL 语句（SELECT / WHERE / ORDER BY 等），"
        "输出查询结果为新矢量图层。</p>"
        "<p>可使用 OGR SQL 方言（含几何函数 ST_Buffer / ST_Intersects 等）。</p>"
    ),
    "gdal:pointsalonglines": (
        "<h2>沿线生成点</h2>"
        "<p>沿线要素按固定距离（间隔）撒点，结果为点矢量，每个点带沿线距离字段。</p>"
        "<p>适合做沿线采样 / 标注 / 桩号。</p>"
    ),
    "gdal:tileindex": (
        "<h2>瓦片索引</h2>"
        "<p>给一组栅格 / 矢量文件生成空间索引（MBTiles 风格 / Shapefile 索引），"
        "用单个多边形图层描述每个文件的位置 / 范围 / 路径。</p>"
        "<p>适合把分幅数据快速定位。</p>"
    ),
    "gdal:ogrinfo": (
        "<h2>矢量信息</h2>"
        "<p>输出矢量数据源的元信息（层名 / 字段 / 范围 / 要素数 / 几何类型等），"
        "用于诊断未知矢量。</p>"
    ),
    # ---- GDAL 数据导出（4） ----
    "gdal:importvectorintopostgisdatabaseavailableconnections": (
        "<h2>PostgreSQL 导出（已有连接）</h2>"
        "<p>把矢量图层导入到已配置的 PostgreSQL/PostGIS 数据库连接，"
        "可指定目标 schema 与表名。</p>"
    ),
    "gdal:importvectorintopostgisdatabasenewconnection": (
        "<h2>PostgreSQL 导出（新连接）</h2>"
        "<p>同上，但允许一次性填写主机 / 端口 / 用户名 / 密码 / 数据库名 "
        "等连接参数，新建连接再导入。</p>"
    ),
    "gdal:buildvirtualvector": (
        "<h2>构建虚拟矢量</h2>"
        "<p>不实际合并文件，生成一个 <code>.vrt</code> 虚拟矢量（XML 描述），"
        "把多个同构矢量当作一个图层使用。</p>"
        "<p>输出轻量，适合作为流式数据源。</p>"
    ),
    "gdal:gdal2xyz": (
        "<h2>gdal2xyz</h2>"
        "<p>把栅格导出为 XYZ 文本格式（每行 <code>x y z</code> 或 "
        "<code>x y z1 z2 z3</code>），适合做高程散点 / 数据交换。</p>"
    ),
}

#: 把仓库内已有的「overlay / native:* / 工具类」兜底与 ``_GDAL_FALLBACK_DESC``
#: 合并到 ``_ALGO_FALLBACK_DESC`` —— ``algorithm_description_html`` 优先查这个
#: 合并字典，命中则不再走「裸 code 命令」的兜底分支。
_ALGO_FALLBACK_DESC: dict[str, str] = {
    **_GDAL_FALLBACK_DESC,  # gdal:* 中文描述
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
    "native:orientedminimumboundingbox": (
        "<h2>定向最小外包矩形</h2>"
        "<p>为输入图层的<b>每个要素</b>生成一个<b>方向最小化</b>的外包矩形面，"
        "用于描述要素的最佳朝向边界（如建筑物主轴、农田垄向等）。</p>"
        "<p>矩形由算法按面积最小化求解，结果字段由算法自动注入："
        "<b>angle</b>（旋转角度）、<b>area</b>（面积）、"
        "<b>perimeter</b>（周长）、<b>width</b> / <b>height</b>"
        "（矩形局部宽与高）。</p>"
        "<p>选择<b>输入图层</b>即可执行；结果自动加入工程。"
        "可填写<b>输出文件路径</b>（GeoPackage / Shapefile）保存到文件；"
        "留空则生成临时图层。</p>"
    ),
    "overlay:analysis": (
        "<h2>叠加分析</h2>"
        "<p>对两个矢量图层执行几何叠加运算，每个运算生成一个独立结果图层。"
        "结果图层的属性表同时携带输入与叠加图层的字段。</p>"
        "<p><b>交集</b>：仅保留两个图层重叠的部分。</p>"
        "<p><b>并集</b>：合并全部几何，重叠区域分割为独立要素。</p>"
        "<p><b>差集</b>（擦除）：只保留输入图层中不与叠加图层重叠的部分；"
        "叠加图层自身的部分不会出现在结果中。</p>"
        "<p><b>对称差</b>：保留输入图层与叠加图层各自不重叠的部分，"
        "两侧几何都会输出；要素缺失的一侧字段为 NULL。</p>"
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
        "<h2>重投影（矢量）</h2>"
        "<p>把矢量图层转换到另一个<b>坐标系（CRS）</b>，几何坐标按目标坐标系"
        "重新计算，属性保持不变。用于把数据统一到工程坐标系，或在不同坐标系"
        "间转换（如 WGS84 经纬度 → Web Mercator 投影）。</p>"
        "<p>选择<b>输入图层</b>与<b>目标坐标系</b>即可；结果自动加入工程。"
        "可填写<b>输出文件路径</b>（GeoPackage / Shapefile）保存到文件；"
        "留空则生成临时图层。</p>"
    ),
    "vector:convert": (
        "<h2>格式转换（矢量）</h2>"
        "<p>把矢量图层转换为 GDAL/OGR 常见的矢量格式，保留几何与属性，"
        "并尽量保留原坐标系。</p>"
        "<p>支持：GeoPackage（.gpkg）、ESRI Shapefile（.shp）、GeoJSON、"
        "ESRI File Geodatabase（.gdb）、KML、GML、FlatGeobuf（.fgb）、"
        "SQLite、MapInfo TAB、AutoCAD DXF、CSV。</p>"
        "<p>选择输入图层、输出格式与输出文件即可；可只导出选中要素，"
        "并可把结果自动加载到工程。</p>"
        "<p><b>注意</b>：GeoJSON / KML / GML 是文本格式，默认不自动加载到工程；"
        "大文件加载、改字段和属性表定位都很慢，KML / GML 还可能因整文件解析而内存不足。"
        "需要编辑请使用 GeoPackage。"
        "Shapefile 字段名最长 10 字符；DXF 不支持任意属性字段，仅导出几何；"
        "CSV 的几何以 WKT 文本保存且不保存坐标系；KML 会自动转换为 WGS84 经纬度。</p>"
    ),
    "native:tilesxyzdirectory": (
        "<h2>影像切片（XYZ）</h2>"
        "<p>把<b>当前地图画布内容</b>渲染为 XYZ 瓦片目录，输出按 "
        "<b>{z}/{x}/{y}.png</b> 组织，可直接作为地图服务的瓦片数据源。</p>"
        "<p><b>输出范围</b>：可选「图层范围」（所选影像的完整范围）或"
        "「当前画布范围」（只切地图当前显示的区域，适合全球底图或局部区域）。"
        "算法按画布渲染。默认勾选"
        "「仅渲染所选图层」，只把所选影像写进瓦片（底图、矢量叠加等其他图层"
        "只在切片用的工程副本里隐藏，界面上的地图不受影响）——否则这些图层会被"
        "逐元瓦片重复渲染，速度可能慢几十倍，底图也会混进结果。取消勾选则按画布"
        "当前内容切片。</p>"
        "<p><b>缩放等级</b>：设置最小 / 最大缩放级别，级别越大切片越细、数量越多。</p>"
        "<p><b>瓦片格式</b>：PNG（无损、适合栅格影像）或 JPG（有损、体积更小）。"
        "可设置瓦片边长与抗锯齿。</p>"
        "<p><b>网页查看器</b>：在输出目录生成一个 Leaflet HTML 页面，可直接打开"
        "浏览瓦片结果。</p>"
        "<p>大范围高缩放的切片耗时较长：切片在<b>后台进程</b>里运行，期间可以继续"
        "使用系统的其他功能，也可随时取消；完成时提示输出目录。</p>"
    ),
    "native:tilesxyzmbtiles": (
        "<h2>影像切片（MBTiles）</h2>"
        "<p>把<b>当前地图画布内容</b>渲染为单个 <b>MBTiles</b> 文件（SQLite "
        "封装的 XYZ 瓦片金字塔），便于<b>分发</b>与离线使用——一个文件即全部"
        "瓦片。</p>"
        "<p><b>输出范围</b>：可选「图层范围」（所选影像的完整范围）或"
        "「当前画布范围」（只切地图当前显示的区域，适合全球底图或局部区域）。"
        "算法按画布渲染。默认勾选"
        "「仅渲染所选图层」，只把所选影像写进瓦片（底图、矢量叠加等其他图层"
        "只在切片用的工程副本里隐藏，界面上的地图不受影响）——否则这些图层会被"
        "逐元瓦片重复渲染，速度可能慢几十倍，底图也会混进结果。取消勾选则按画布"
        "当前内容切片。</p>"
        "<p><b>缩放等级</b>：设置最小 / 最大缩放级别，级别越大切片越细、数量越多。</p>"
        "<p><b>瓦片格式</b>：PNG（无损、适合栅格影像）或 JPG（有损、体积更小）。"
        "MBTiles 内部固定 256×256 瓦片、按 TMS 规范存储。</p>"
        "<p><b>网页查看器</b>：在输出文件旁生成一个 Leaflet HTML 页面"
        "（``{文件名}_preview/`` 目录，浏览器读不了 SQLite 格式的 mbtiles，"
        "故把瓦片解成目录后引用），双击即可预览切片结果。</p>"
        "<p>大范围高缩放的切片耗时较长：切片在<b>后台进程</b>里运行，期间可以继续"
        "使用系统的其他功能，也可随时取消；完成时提示输出文件路径。</p>"
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
    """取算法的中文描述（HTML）：processing 注册表优先，兜底文案其次。

    用户反馈：「对话框内的算法描述为什么都是 gdal:cliprasterbymasklayer 这种
    命令」——探针确认 QGIS 4.2 在这套环境下所有 ``gdal:*`` 算法的
    ``shortHelpString()`` 都返回空字符串（注册表能拿到 alg，displayName 也
    对，但 help 文本本身没装上）。如果不处理，``if help_html`` 分支失败，
    最终落到 ``<p>{code}</p>`` 兜底，把裸命令名直接吐给用户。

    处理顺序：
    1. 注册表能拿到 help → 正常用 title + help
    2. 注册表能拿到 alg 但 help 空 → 至少展示 displayName，避免裸 code
    3. 注册表拿不到 / 抛错 → ``_ALGO_FALLBACK_DESC`` 兜底；都没有再 ``<p>{code}</p>``
    """
    if not code:
        return _hint_html()
    # GDAL 算法统一用仓库内中文说明：本环境注册表的 help 为空，其他环境即使
    # 有 help 也可能是英文；优先保证界面是完整中文，不再拼接英文原名。
    if code.startswith("gdal:"):
        fallback = _ALGO_FALLBACK_DESC.get(code)
        if fallback is not None:
            return fallback
    name = ""
    try:
        from qgis.core import QgsApplication

        registry = QgsApplication.processingRegistry()
        alg = registry.algorithmById(code)
        if alg is not None:
            name = alg.displayName()
            help_html = alg.shortHelpString()
            if help_html:
                title = f"<h2>{algorithm_title(code, name)}</h2>" if name else ""
                return title + help_html
    except Exception:  # noqa: BLE001
        logger.warning("读取算法描述失败 code=%s，使用兜底文案", code, exc_info=True)

    fallback = _ALGO_FALLBACK_DESC.get(code)
    if fallback is not None:
        return fallback

    # 走到这里说明：(a) alg 拿到了但 shortHelpString 空，或 (b) 抛错。
    # 至少把 displayName 渲染出来，避免「裸 code 命令」这种无意义展示。
    if name:
        return (
            f"<h2>{algorithm_title(code, name)}</h2>"
            "<p style='color:gray'>"
            "（该算法的详细说明未随处理注册表一同装载，"
            "请在对话框中按字段名 / tooltip 查看参数意义。）</p>"
            f"<p style='color:gray;font-size:small'>内部 code："
            f"<code>{code}</code></p>"
        )
    return f"<p>{code}</p>"


def make_description_browser(code: str) -> QTextBrowser:
    """构造展示指定算法描述的 :class:`QTextBrowser`（只读；配色走 app_qss）。"""
    browser = QTextBrowser()
    browser.setOpenExternalLinks(True)
    browser.setHtml(algorithm_description_html(code))
    return browser


class DescriptionPanel(QWidget):
    """可折叠的算法说明面板。

    顶部是「帮助说明」小节标题 + 右侧小箭头按钮，正文为
    :func:`make_description_browser`。收起时隐藏标题与正文，只留一个贴边的
    小箭头，把横向空间让回左侧参数区；展开时恢复标题与正常宽度。默认展开，
    宽度占比由外部布局的 stretch 决定（分析工具对话框统一用参数 3 : 说明 1）。
    """

    #: 展开时取消宽度限制（Qt 的 QWIDGETSIZE_MAX）
    _NO_MAX_WIDTH = 16_777_215

    def __init__(self, code: str, parent=None, *, expanded: bool = True) -> None:
        super().__init__(parent)
        self._code = code
        self._expanded = bool(expanded)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self.toggle = QToolButton()
        self.toggle.setAutoRaise(True)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setFixedSize(22, 22)
        self.toggle.setStyleSheet("QToolButton { padding: 0px; }")
        self.toggle.clicked.connect(self._toggle_expanded)
        header.addWidget(
            self.toggle,
            0,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        self.title_label = QLabel("帮助说明")
        self.title_label.setObjectName("SectionLabel")
        header.addWidget(self.title_label, 1)
        layout.addLayout(header)

        self.browser = make_description_browser(code)
        # 说明正文本身可滚动：纵向用 Ignored，避免它的 sizeHint 撑高整个对话框。
        self.browser.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.browser, 1)
        # 收起后正文隐藏，底部 stretch 顶住标题行，避免小箭头跑到垂直中间。
        layout.addStretch(0)
        self._apply_state(self._expanded)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self._apply_state(self._expanded)

    def _toggle_expanded(self, *_args) -> None:
        self.set_expanded(not self._expanded)

    def _apply_state(self, expanded: bool) -> None:
        self.title_label.setVisible(expanded)
        self.browser.setVisible(expanded)
        self.toggle.setArrowType(
            Qt.ArrowType.RightArrow if expanded else Qt.ArrowType.LeftArrow
        )
        self.toggle.setToolTip("收起帮助说明" if expanded else "展开帮助说明")
        if expanded:
            self.setMaximumWidth(self._NO_MAX_WIDTH)
            self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        else:
            self.setMaximumWidth(self.toggle.width() + 8)
            self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.updateGeometry()


def make_description_panel(code: str, *, expanded: bool = True) -> DescriptionPanel:
    """构造可折叠的算法说明面板（分析工具对话框右侧统一使用）。"""
    return DescriptionPanel(code, expanded=expanded)


__all__ = [
    "HINT_HTML",
    "DescriptionPanel",
    "algorithm_description_html",
    "make_description_browser",
    "make_description_panel",
]
