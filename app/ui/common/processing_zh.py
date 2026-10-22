"""GDAL / Processing 算法标题与参数中文映射。

QGIS 自带的 GDAL provider 在本环境没有中文翻译：算法标题、参数标签、
枚举选项都是英文（例如 ``Polygonize (raster to vector)``、``Input layer``、
``Use 8-connectedness``）。这里集中维护中文映射，供
:class:`app.ui.dialogs.generic_alg.GenericAlgDialog` 在生成表单时使用。

只翻译展示层：参数内部名称（``INPUT`` / ``EIGHT_CONNECTEDNESS`` 等）
保持不变，``wrapper.parameterValue()`` 仍按原参数名收集值，不影响执行。
"""

from __future__ import annotations

from qgis.core import QgsProcessingParameterDefinition, QgsProcessingParameterNumber

#: GDAL 算法 id → 中文标题（与分析工具树上的显示名保持一致）
ALGO_TITLES: dict[str, str] = {
    "gdal:aspect": "坡向",
    "gdal:assignprojection": "指派投影",
    "gdal:buffervectors": "矢量缓冲区",
    "gdal:buildvirtualraster": "构建虚拟栅格",
    "gdal:buildvirtualvector": "构建虚拟矢量",
    "gdal:cliprasterbyextent": "按范围裁剪栅格",
    "gdal:cliprasterbymasklayer": "按掩膜裁剪栅格",
    "gdal:clipvectorbyextent": "按范围裁剪矢量",
    "gdal:clipvectorbypolygon": "按多边形裁剪矢量",
    "gdal:colorrelief": "彩色渲染",
    "gdal:contour": "等高线",
    "gdal:contour_polygon": "等高线多边形",
    "gdal:convertformat": "格式转换",
    "gdal:createcog": "创建 COG",
    "gdal:dataset_identify": "数据集识别",
    "gdal:dissolve": "融合",
    "gdal:executesql": "OGR 执行 SQL",
    "gdal:extractprojection": "提取投影",
    "gdal:fillnodata": "填充 NoData",
    "gdal:gdal2tiles": "gdal2tiles 切片",
    "gdal:gdal2xyz": "gdal2xyz",
    "gdal:gdalinfo": "栅格信息",
    "gdal:gridaverage": "Grid 移动平均",
    "gdal:griddatametrics": "Grid 数据度量",
    "gdal:gridinversedistance": "Grid IDW（幂）",
    "gdal:gridinversedistancenearestneighbor": "Grid IDW（最近邻搜索）",
    "gdal:gridlinear": "Grid 线性插值",
    "gdal:gridnearestneighbor": "Grid 最近邻",
    "gdal:hillshade": "山体阴影",
    "gdal:importvectorintopostgisdatabaseavailableconnections": "PostgreSQL 导出（已有连接）",
    "gdal:importvectorintopostgisdatabasenewconnection": "PostgreSQL 导出（新连接）",
    "gdal:merge": "拼接栅格",
    "gdal:nearblack": "去黑边",
    "gdal:offsetcurve": "偏移线",
    "gdal:ogrinfo": "矢量信息",
    "gdal:ogrinfojson": "矢量信息（JSON）",
    "gdal:onesidebuffer": "单边缓冲区",
    "gdal:overviews": "构建金字塔",
    "gdal:pansharp": "全色融合",
    "gdal:pcttorgb": "PCT 转 RGB",
    "gdal:pointsalonglines": "沿线生成点",
    "gdal:polygonize": "栅格矢量化",
    "gdal:proximity": "距离栅格",
    "gdal:rastercalculator": "栅格计算器",
    "gdal:rasterize": "矢栅转换",
    "gdal:rasterize_over": "属性值栅格化（覆盖）",
    "gdal:rasterize_over_fixed_value": "固定值栅格化（覆盖）",
    "gdal:rearrange_bands": "重排波段",
    "gdal:retile": "重切片",
    "gdal:rgbtopct": "RGB 转 PCT",
    "gdal:roughness": "粗糙度",
    "gdal:sieve": "筛除小图斑",
    "gdal:slope": "坡度",
    "gdal:tileindex": "瓦片索引",
    "gdal:tpitopographicpositionindex": "TPI 地形位置指数",
    "gdal:translate": "格式转换（栅格）",
    "gdal:triterrainruggednessindex": "TRI 地表 ruggedness",
    "gdal:viewshed": "可视域",
    "gdal:warpreproject": "重投影（栅格）",
}

#: GDAL 参数英文描述 → 中文标签
PARAM_LABELS: dict[str, str] = {
    "A fixed value to burn": "固定燃烧值",
    "Add alpha mask band to VRT when source raster has none": "源栅格无 Alpha 时向 VRT 添加 Alpha 掩膜波段",
    "Add burn in values to existing raster values": "将燃烧值叠加到现有栅格值",
    "Add details about identified datasets in the output": "在输出中附带数据集识别详情",
    "Additional command-line parameters": "附加命令行参数",
    "Additional creation options": "附加创建选项",
    "Allow projection difference": "允许投影差异",
    "Altitude of the light": "光源高度角",
    "Angle of search ellipse rotation in degrees (counter clockwise)": "搜索椭圆旋转角度（度，逆时针）",
    "Append and add new fields to existing table": "追加并添加新字段到现有表",
    "Append to existing table": "追加到现有表",
    "Aspect": "坡向",
    "Assign a specified NoData value to output bands": "为输出波段指定 NoData 值",
    "Assign an output CRS": "指定输出坐标系",
    "Assign specified NoData value to output": "为输出指定 NoData 值",
    "Attribute name (if not set, no elevation attribute is attached)": "属性字段名（留空则不附加高程属性）",
    "Attribute name for maximum elevation of contour polygon": "等高线多边形最高高程字段名",
    "Attribute name for minimum elevation of contour polygon": "等高线多边形最低高程字段名",
    "Avoid automatic generation of KML files for EPSG:4326": "EPSG:4326 不自动生成 KML 文件",
    "Azimuth of the light": "光源方位角",
    "Band number": "波段号",
    "Bing Maps API key (https://www.bingmapsportal.com/)": "Bing 地图 API 密钥",
    "Buffer": "缓冲区",
    "Buffer distance": "缓冲距离",
    "Buffer side": "缓冲侧",
    "Build only the pyramids": "仅构建金字塔",
    'Burn value extracted from the "Z" values of the feature': "从要素 Z 值提取燃烧值",
    "CRS of the target raster extent": "目标栅格范围坐标系",
    "CSV file containing the tile(s) georeferencing information": "包含瓦片地理参考信息的 CSV 文件",
    "Calculated": "计算结果",
    "Calculation in gdalnumeric syntax using +-/* or any numpy array functions (i.e. logical_and())": "使用 gdalnumeric 语法计算（支持 +-/* 及 numpy 函数，如 logical_and()）",
    "Clip the input layer using the above (rectangle) extent": "按上述矩形范围裁剪输入图层",
    "Clipped (extent)": "裁剪结果（范围）",
    "Clipped (mask)": "裁剪结果（掩膜）",
    "Clipping extent": "裁剪范围",
    "Color configuration file": "颜色配置文件",
    "Color relief": "彩色晕渲",
    "Column delimiter used in the CSV file": "CSV 文件列分隔符",
    "Combined shading": "组合晕渲",
    "Compute area and perimeter of dissolved features": "计算融合要素的面积与周长",
    "Compute edges": "计算边缘",
    "Compute min/max/sum/mean for attribute": "计算属性最小值/最大值/总和/平均值",
    "Continue after a failure, skipping the failed feature": "失败后继续并跳过失败要素",
    "Contours": "等高线结果",
    "Convert all layers from dataset": "转换数据集中的所有图层",
    "Converted": "转换结果",
    "Copy all subdatasets of this file to individual output files": "将文件的所有子数据集复制为单独输出文件",
    "Copyright of the map": "地图版权信息",
    "Count dissolved features": "统计融合要素数量",
    'Create "unioned" VRT': "创建合并 VRT",
    "Create also .prj file": "同时创建 .prj 文件",
    "Create an output alpha band": "创建输出 Alpha 波段",
    "Data metric to use": "数据度量指标",
    "Database (connection name)": "数据库（连接名）",
    "Database name": "数据库名",
    "Desired CRS": "目标坐标系",
    "Dissolve all results": "融合所有结果",
    "Dissolve by attribute": "按属性融合",
    "Dissolve field": "融合字段",
    "Dissolved": "融合结果",
    "Distance from line start represented as fraction of line length": "距线起点的距离（占线长比例）",
    "Distance tolerance for simplification": "简化距离容差",
    "Distance units": "距离单位",
    "Do not create spatial index": "不创建空间索引",
    "Do not launder columns/table names": "不转换列名/表名",
    "Do not output NoData values": "不输出 NoData 值",
    "Do not use the default validity mask for the input band": "不使用输入波段的默认有效值掩膜",
    "Enable listing of all layers in the dataset": "列出数据集中的所有图层",
    "Enable listing of features": "列出要素信息",
    "Express slope as percent instead of degrees": "坡度以百分比表示（而非度）",
    "Field name to hold the file path to the indexed rasters": "保存栅格文件路径的字段名",
    "Field to use for a burn-in value": "用于燃烧值的字段",
    "Field to use for burn in value": "用于燃烧值的字段",
    "Fields to include (leave empty to use all fields)": "包含的字段（留空表示全部）",
    "Filled": "填充结果",
    "Force computation of the actual min/max values for each band": "强制计算各波段实际最小/最大值",
    "Generate KML for Google Earth": "生成供 Google Earth 使用的 KML",
    "Generate a RGBA file": "生成 RGBA 文件",
    "Generate only missing files": "仅生成缺失的文件",
    "Geometry column name": "几何列名",
    "Georeferenced extents of output file to be created": "输出文件的地理参考范围",
    "Google Maps API key (http://code.google.com/apis/maps/signup.html)": "Google 地图 API 密钥",
    "Grab pseudocolor table from first layer": "使用第一层的伪彩色表",
    "Group N features per transaction (Default: 20000)": "每事务处理的要素数（默认 20000）",
    "Handling of extent differences": "范围差异处理方式",
    "Height/Vertical resolution": "高度/垂直分辨率",
    "Hillshade": "山体阴影",
    "Host": "主机",
    "How far from black (white)": "距黑/白值的容差",
    "Input datasources": "输入数据源",
    "Input file": "输入文件",
    "Input files": "输入文件",
    "Input folder": "输入文件夹",
    "Input layer": "输入图层",
    "Input layer A": "输入图层 A",
    "Input layer B": "输入图层 B",
    "Input layer C": "输入图层 C",
    "Input layer D": "输入图层 D",
    "Input layer E": "输入图层 E",
    "Input layer F": "输入图层 F",
    "Input layers": "输入图层",
    "Input pixel value to treat as NoData": "视为 NoData 的输入像元值",
    "Input raster layer": "输入栅格图层",
    "Input vector layer": "输入矢量图层",
    "Interpolated (IDW with NN search)": "插值结果（IDW 最近邻搜索）",
    "Interpolated (IDW)": "插值结果（IDW）",
    "Interpolated (Linear)": "插值结果（线性）",
    "Interpolated (Nearest neighbor)": "插值结果（最近邻）",
    "Interpolated (data metrics)": "插值结果（数据度量）",
    "Interpolated (moving average)": "插值结果（移动平均）",
    "Interval between contour lines": "等高线间距",
    "Invert rasterization": "反转栅格化",
    "Keep input attributes": "保留输入属性",
    "Keep resolution of input raster": "保持输入栅格分辨率",
    "Keep width and precision of input attributes": "保留输入属性的宽度与精度",
    "Layer information": "图层信息",
    "List of target pixels": "目标像元列表",
    "Mask layer": "掩膜图层",
    "Match the extent of the clipped raster to the extent of the mask layer": "裁剪栅格范围与掩膜图层范围一致",
    "Matching mode": "匹配方式",
    "Maximum distance (in pixels) to search out for values to interpolate": "搜索插值值的最大距离（像元）",
    "Maximum distance between 2 nodes (densification)": "两节点间最大距离（加密）",
    "Maximum distance from observer to compute visibility": "计算可见性的最远观察距离",
    "Maximum number of data points to use": "最多使用的数据点数",
    "Merged": "合并结果",
    "Minimum number of data points to use": "最少使用的数据点数",
    "Multidirectional shading": "多方向晕渲",
    "NODATA marker to fill empty points": "填充空点的 NoData 值",
    "Name of the field to create": "输出字段名",
    "Nearblack": "去黑边结果",
    "Nodata value for output bands": "输出波段 NoData 值",
    "Nodata value to use for the destination proximity raster": "输出距离栅格的 NoData 值",
    "Nodata value(s) for input bands (space separated)": "输入波段 NoData 值（空格分隔）",
    "Number of colors": "颜色数量",
    "Number of pyramids levels to build": "构建金字塔的层级数",
    "Number of raster band for A": "A 的栅格波段号",
    "Number of raster band for B": "B 的栅格波段号",
    "Number of raster band for C": "C 的栅格波段号",
    "Number of raster band for D": "D 的栅格波段号",
    "Number of raster band for E": "E 的栅格波段号",
    "Number of raster band for F": "F 的栅格波段号",
    "Number of smoothing iterations to run after the interpolation": "插值后平滑迭代次数",
    "Numeric attribute to calculate statistics on": "用于统计计算的数值字段",
    "Observer height, DEM units": "观察点高度（DEM 单位）",
    "Observer location": "观察点位置",
    "Offset curve": "偏移线结果",
    "Offset distance (left-sided: positive, right-sided: negative)": "偏移距离（左侧为正，右侧为负）",
    "Offset from zero relative to which to interpret intervals": "等高线相对零值的偏移量",
    "One-sided buffer": "单边缓冲区结果",
    "Output": "输出",
    "Output comma-separated values": "输出逗号分隔值",
    "Output data type": "输出数据类型",
    "Output directory": "输出目录",
    "Output extent": "输出范围",
    "Output file": "输出文件",
    "Output file resolution in target georeferenced units": "输出文件分辨率（目标地理参考单位）",
    "Output geometry type": "输出几何类型",
    "Output raster size units": "输出栅格尺寸单位",
    "Output raster type": "输出栅格类型",
    "Overlap in pixels between consecutive tiles": "相邻瓦片的重叠像元数",
    "Override projection for the output file": "覆盖输出文件投影",
    "Override source CRS": "覆盖源坐标系",
    "Override the projection for the output file": "覆盖输出文件投影",
    "Overview levels (e.g. 2 4 8 16)": "金字塔层级（如 2 4 8 16）",
    "Overviews format": "金字塔格式",
    "Overwrite existing table": "覆盖现有表",
    "PCT to RGB": "调色板转 RGB 结果",
    "Panchromatic dataset": "全色数据集",
    "Password": "密码",
    "Perform recursive exploration of the input folder": "递归扫描输入文件夹",
    "Place each input file into a separate band": "每个输入文件放入单独波段",
    "Point layer": "点图层",
    "Points along lines": "沿线生成点结果",
    "Port": "端口",
    "Pre-initialize the output image with value": "用指定值预初始化输出影像",
    "Primary key (existing field, used if the above option is left empty)": "主键（现有字段，上方选项留空时使用）",
    "Primary key (new field)": "主键（新字段）",
    "Produce 3D vector": "生成三维矢量",
    "Produce one feature for each geometry in any kind of geometry collection in the source file": "为源文件中任意几何集合内的每个几何生成一个要素",
    "Promote to Multipart": "提升为多部件",
    "Proximity map": "距离栅格结果",
    "RGB to PCT": "RGB 转调色板结果",
    "Rasterized": "栅格化结果",
    "Ratio of vertical units to horizontal": "垂直单位与水平单位的比值",
    "Read and display image statistics (force computation if necessary)": "读取并显示影像统计信息（必要时强制计算）",
    "Remove all existing overviews": "删除所有现有金字塔",
    "Reproject to this CRS on output": "输出时重投影到该坐标系",
    "Reprojected": "重投影结果",
    "Resampling algorithm": "重采样算法",
    "Resampling method": "重采样方法",
    "Resampling method to use": "使用的重采样方法",
    "Resolution": "分辨率",
    "Return 0 for flat instead of -9999": "平坦区域返回 0（而非 -9999）",
    "Return trigonometric angle instead of azimuth": "返回三角角度（而非方位角）",
    "Roughness": "粗糙度",
    "SQL dialect": "SQL 方言",
    "SQL expression": "SQL 表达式",
    "SQL result": "SQL 结果",
    "Scale (ratio of vertical units to horizontal)": "垂直比例（垂直单位与水平单位之比）",
    "Schema (schema name)": "模式（schema 名）",
    "Schema name": "模式名",
    "Search distance": "搜索距离",
    "Search for nearly white pixels instead of nearly black": "搜索接近白色的像元（而非接近黑色）",
    "Select features by extent (defined in input layer CRS)": "按范围选择要素（使用输入图层坐标系）",
    "Select features using a SQL \"WHERE\" statement (Ex: column='value')": "使用 SQL WHERE 语句选择要素（如 column='value'）",
    "Selected band(s)": "选中波段",
    "Set output NoData value": "设置输出 NoData 值",
    "Set output file resolution": "设置输出文件分辨率",
    "Shape encoding": "属性编码",
    "Sieved": "筛除小图斑结果",
    "Skip files with different projection reference": "跳过投影参考不同的文件",
    "Slope": "坡度",
    "Smoothing": "平滑系数",
    "Source CRS": "源坐标系",
    "Source coordinate reference system": "源坐标系",
    "Spectral dataset": "多光谱数据集",
    "Store absolute path to the indexed rasters": "保存栅格的绝对路径",
    "Summary output only": "仅输出摘要信息",
    "Suppress GCP info": "不输出 GCP 信息",
    "Suppress metadata info": "不输出元数据信息",
    "Table name, leave blank to use input name": "表名（留空则使用输入图层名）",
    "Table to import to (leave blank to use layer name)": "导入目标表（留空则使用图层名）",
    "Target CRS": "目标坐标系",
    "Target extent": "目标范围",
    "Target height, DEM units": "目标高度（DEM 单位）",
    "Terrain Ruggedness Index": "地形起伏度指数",
    "The first radius of search ellipse": "搜索椭圆第一半径",
    "The format in which the CRS of each tile must be written": "每个瓦片坐标系的写入格式",
    "The maximum distance to generate": "生成的最大距离",
    "The name of the field to store the SRS of each tile": "保存每个瓦片坐标系的字段名",
    "The radius of the search circle": "搜索圆半径",
    "The second radius of search ellipse": "搜索椭圆第二半径",
    "The spatial reference system used for the source input data": "源输入数据的空间参考系",
    "Threshold": "阈值",
    "Tile cutting profile": "瓦片剖分方案",
    "Tile height": "瓦片高度",
    "Tile index": "瓦片索引结果",
    "Tile width": "瓦片宽度",
    "Title of the map": "地图标题",
    "Topographic Position Index": "地形位置指数",
    "Transform geometries to the given CRS": "将几何转换到指定坐标系",
    "Transparency value to assign to the input data": "为输入数据指定的透明度值",
    "Treat all raster values as valid": "将所有栅格值视为有效值",
    "URL address where the generated tiles are going to be published": "生成瓦片的发布 URL",
    "Use 8-connectedness": "使用 8 连通",
    "Use Zevenbergen&Thorne formula instead of the Horn's one": "使用 Zevenbergen&Thorne 公式（而非 Horn 公式）",
    "Use multithreaded warping implementation": "使用多线程重投影",
    "Use separate directory for each tiles row": "每行瓦片使用单独目录",
    "Username": "用户名",
    "Validate geometries based on Simple Features specification": "按 Simple Features 规范验证几何",
    "Validity mask": "有效值掩膜",
    "Value to apply to pixels within the maximum distance of target pixels": "目标像元最大距离内应用的像元值",
    "Vector dimensions": "矢量维度",
    "Vectorized": "矢量化结果",
    "Virtual": "虚拟栅格结果",
    "Virtual vector": "虚拟矢量结果",
    "Web viewer to generate": "生成的网页查看器",
    "Weighting power": "权重幂",
    "Width/Horizontal resolution": "宽度/水平分辨率",
    "X Resolution to output bands": "输出波段的 X 分辨率",
    "XYZ ASCII file": "XYZ ASCII 文件",
    "Y Resolution to output bands": "输出波段的 Y 分辨率",
    "Z factor (vertical exaggeration)": "Z 因子（垂直夸张）",
    "Z value from field": "字段中的 Z 值",
    "Zoom levels to render": "渲染的缩放级别",
}

#: 枚举选项英文 → 中文（数据类型 / 几何类型等专有名词保持原样）
ENUM_LABELS: dict[str, str] = {
    "All": "全部",
    "Antialias": "抗锯齿",
    "Auto": "自动",
    "Average": "平均值",
    "Average MP": "平均值 MP",
    "Average distance": "平均距离",
    "Average distance between points": "点间平均距离",
    "Average in Mag/Phase Space": "Mag/Phase 空间平均值",
    "Bilinear (2x2 Kernel)": "双线性（2x2 核）",
    "Count": "计数",
    "Cubic (4x4 Kernel)": "三次卷积（4x4 核）",
    "Cubic B-Spline (4x4 Kernel)": "三次 B 样条（4x4 核）",
    "External (ERDAS Imagine .aux)": "外部（ERDAS Imagine .aux）",
    "External (GTiff .ovr)": "外部（GTiff .ovr）",
    "Fail": "失败",
    "First Quartile (Q1)": "第一四分位数（Q1）",
    "Gaussian": "高斯",
    "Geodetic": "大地坐标",
    "Georeferenced coordinates": "地理参考坐标",
    "Georeferenced units": "地理参考单位",
    "GoogleMaps": "Google 地图",
    "Highest": "最高",
    "Ignore": "忽略",
    "Internal (if possible)": "内部（如可能）",
    "Intersect": "交集",
    "Lanczos (6x6 Kernel)": "Lanczos（6x6 核）",
    "Left": "左侧",
    "Lowest": "最低",
    "Maximum": "最大值",
    "Median": "中位数",
    "Mercator": "墨卡托",
    "Minimum": "最小值",
    "Mode": "众数",
    "NONE": "无",
    "Nearest Neighbour": "最近邻",
    "Nearest Neighbour (default)": "最近邻（默认）",
    "None": "无",
    "Pixel coordinates": "像元坐标",
    "Pixels": "像元",
    "Range": "范围",
    "Raster": "栅格",
    "Right": "右侧",
    "Third Quartile (Q3)": "第三四分位数（Q3）",
    "Union": "并集",
    "Use Input Layer Data Type": "使用输入图层数据类型",
    "Use closest RGBA quadruplet": "使用最接近的 RGBA 四元组",
    "Use smoothly blended colors": "使用平滑混合颜色",
    "Use strict color matching": "使用严格颜色匹配",
    "Well-known text (WKT)": "熟知文本（WKT）",
}


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _strip_bilingual_suffix(text: str) -> str:
    """去掉「中文 - English」里的英文尾巴。

    某些 QGIS 运行环境（或加载的翻译）会把描述返回成
    ``使用8向连通 - Use 8-connectedness``。前面的中文已经完整表达了含义，
    界面不再需要保留后面的英文原文。
    """
    if " - " not in text:
        return text
    left, right = (part.strip() for part in text.split(" - ", 1))
    left_cjk = _has_cjk(left)
    right_cjk = _has_cjk(right)
    if left_cjk and not right_cjk:
        return left
    if right_cjk and not left_cjk:
        return right
    return text


def algorithm_title(algo_id: str, fallback: str = "") -> str:
    """返回算法中文标题；没有映射时用 QGIS 原名（同时去掉双语英文尾巴）。"""
    if algo_id in ALGO_TITLES:
        return ALGO_TITLES[algo_id]
    return _strip_bilingual_suffix(fallback or algo_id)


def parameter_label(description: str) -> str:
    """返回参数中文标签；没有映射时保留原文（去掉双语英文尾巴）。"""
    text = (description or "").strip()
    if text in PARAM_LABELS:
        return PARAM_LABELS[text]
    stripped = _strip_bilingual_suffix(text)
    if stripped in PARAM_LABELS:
        return PARAM_LABELS[stripped]
    return stripped


def enum_label(option: str) -> str:
    """返回枚举选项中文文本；专有名词 / 数字原样保留。"""
    text = (option or "").strip()
    if text in ENUM_LABELS:
        return ENUM_LABELS[text]
    stripped = _strip_bilingual_suffix(text)
    if stripped in ENUM_LABELS:
        return ENUM_LABELS[stripped]
    return stripped if stripped != text else option


def _format_number(value) -> str:
    try:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
    except (TypeError, ValueError):
        pass
    return str(value)


def parameter_tooltip(pdef) -> str:
    """生成不重复标签的中文提示。

    参数标签已经展示描述，所以这里只给真正额外的信息（高级 / 数值范围 /
    默认值），避免出现「标签与提示一模一样的英文句子」。
    """
    parts: list[str] = []
    if pdef.flags() & QgsProcessingParameterDefinition.Flag.FlagAdvanced:
        parts.append("高级参数")

    if isinstance(pdef, QgsProcessingParameterNumber):
        try:
            minimum = pdef.minimum()
            maximum = pdef.maximum()
            default = pdef.defaultValue()
        except Exception:  # noqa: BLE001 —— 某些子类取值失败不影响表单
            return "；".join(parts)
        if minimum is not None and minimum > -1e12:
            parts.append(f"最小值：{_format_number(minimum)}")
        if maximum is not None and maximum < 1e12:
            parts.append(f"最大值：{_format_number(maximum)}")
        if default is not None:
            parts.append(f"默认值：{_format_number(default)}")
    return "；".join(parts)


__all__ = [
    "ALGO_TITLES",
    "ENUM_LABELS",
    "PARAM_LABELS",
    "algorithm_title",
    "enum_label",
    "parameter_label",
    "parameter_tooltip",
]
