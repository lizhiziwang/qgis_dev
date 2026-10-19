"""矢量数据格式转换领域服务。

使用 GDAL/OGR（经 :class:`QgsVectorFileWriter`）把矢量图层写出为常见格式，
不依赖 QGIS Processing 插件。格式清单集中定义在 :data:`VECTOR_FORMATS`，
参数对话框与说明文档共用同一份元数据。
"""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from app.core.domain.geom_cleanup import copy_with_widened_string_fields
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsFeedback, QgsVectorLayer

logger = get_logger(__name__)


@dataclass(frozen=True)
class VectorFormatSpec:
    """一种可输出的矢量格式。"""

    #: 下拉框显示名
    label: str
    #: OGR 驱动名（传给 :class:`QgsVectorFileWriter`）
    driver: str
    #: 首选扩展名（含点）
    extension: str
    #: 文件选择器过滤器
    file_filter: str
    #: 写入时的图层创建选项（driver-specific）
    layer_options: tuple[str, ...] = ()
    #: 为 ``True`` 时跳过错属性字段创建（如 DXF 不支持任意字段）
    skip_attributes: bool = False
    #: 转换后是否默认加载到工程；XML / 文本格式默认关闭，避免大文件解析卡死
    load_by_default: bool = True
    #: 界面上的简短说明
    note: str = ""


#: 常用矢量格式清单。顺序即参数对话框下拉框顺序。
VECTOR_FORMATS: tuple[VectorFormatSpec, ...] = (
    VectorFormatSpec(
        label="GeoPackage",
        driver="GPKG",
        extension=".gpkg",
        file_filter="GeoPackage (*.gpkg)",
        note="单文件数据库格式，保留坐标系、属性与几何，适合作为通用交换格式。",
    ),
    VectorFormatSpec(
        label="ESRI Shapefile",
        driver="ESRI Shapefile",
        extension=".shp",
        file_filter="ESRI Shapefile (*.shp)",
        layer_options=("ENCODING=UTF-8",),
        note="经典的 GIS 交换格式；字段名最长 10 字符，长字段名可能被截断。",
    ),
    VectorFormatSpec(
        label="GeoJSON",
        driver="GeoJSON",
        extension=".geojson",
        file_filter="GeoJSON (*.geojson)",
        layer_options=("COORDINATE_PRECISION=6",),
        load_by_default=False,
        note="文本格式，适合 Web 交换；坐标保留 6 位小数。大图层加载/编辑较慢，默认不加载到工程。",
    ),
    VectorFormatSpec(
        label="ESRI File Geodatabase",
        driver="OpenFileGDB",
        extension=".gdb",
        file_filter="ESRI File Geodatabase (*.gdb)",
        note="ESRI 文件地理数据库，保留坐标系、属性与几何。",
    ),
    VectorFormatSpec(
        label="KML",
        driver="KML",
        extension=".kml",
        file_filter="Keyhole Markup Language (*.kml)",
        load_by_default=False,
        note="Google Earth 交换格式；输出会自动转换为 WGS84 经纬度。大文件加载可能内存不足，默认不加载。",
    ),
    VectorFormatSpec(
        label="GML",
        driver="GML",
        extension=".gml",
        file_filter="Geography Markup Language (*.gml)",
        load_by_default=False,
        note="OGC 标准 XML 格式，保留坐标系、属性与几何。大文件加载较慢，默认不加载。",
    ),
    VectorFormatSpec(
        label="FlatGeobuf",
        driver="FlatGeobuf",
        extension=".fgb",
        file_filter="FlatGeobuf (*.fgb)",
        note="单文件二进制格式，读取快，适合大数据量交换。",
    ),
    VectorFormatSpec(
        label="SQLite",
        driver="SQLite",
        extension=".sqlite",
        file_filter="SQLite (*.sqlite)",
        note="SQLite / SpatiaLite 数据库格式，保留坐标系、属性与几何。",
    ),
    VectorFormatSpec(
        label="MapInfo TAB",
        driver="MapInfo File",
        extension=".tab",
        file_filter="MapInfo TAB (*.tab)",
        layer_options=("ENCODING=UTF-8",),
        note="MapInfo 交换格式，字段使用 UTF-8 编码。",
    ),
    VectorFormatSpec(
        label="AutoCAD DXF",
        driver="DXF",
        extension=".dxf",
        file_filter="AutoCAD DXF (*.dxf)",
        skip_attributes=True,
        note="CAD 交换格式；不支持任意属性字段，仅导出几何，属性会丢失。",
    ),
    VectorFormatSpec(
        label="CSV",
        driver="CSV",
        extension=".csv",
        file_filter="CSV (*.csv)",
        layer_options=("GEOMETRY=AS_WKT", "SEPARATOR=COMMA", "HEADERS=YES"),
        note="导出属性表，几何以 WKT 文本列保存；不保存坐标系。",
    ),
)


def _remove_partial_output(path: str) -> None:
    """删除失败时留下的半成品，避免用户把残缺文件当成完整结果。

    Shapefile / MapInfo 是「主文件 + 边车文件」，只删主文件会留下孤儿文件。
    """
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
        return
    ext = os.path.splitext(path)[1].lower()
    sidecars = {
        ".shp": (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".qpj", ".shp.xml"),
        ".tab": (".tab", ".dat", ".map", ".id", ".ind"),
    }.get(ext)
    if sidecars:
        base = os.path.splitext(path)[0]
        for suffix in sidecars:
            with contextlib.suppress(OSError):
                os.remove(base + suffix)
        return
    with contextlib.suppress(OSError):
        os.remove(path)


@dataclass(frozen=True)
class VectorConvertJob:
    """一次矢量格式转换的参数。"""

    #: 输入矢量图层（必须是图层对象，不能是 source 字符串）
    layer: QgsVectorLayer
    #: 输出文件路径（扩展名应与 ``fmt.extension`` 一致）
    output_path: str
    #: 目标格式
    fmt: VectorFormatSpec
    #: 输出图层名；空字符串时取输出文件名
    layer_name: str = ""
    #: 是否只导出选中要素
    only_selected: bool = False


class VectorConvertService:
    """矢量格式转换服务门面。

    GUI 走「后台准备字段 → 后台写出 → 主线程装载」三段式：
    :meth:`prepare` / :meth:`write` 可在后台线程执行（准备阶段创建的临时
    图层只在同一线程内使用），:meth:`load_result` 必须在主线程调用。
    :meth:`run` 是同步组合，供测试或简单场景使用。
    """

    def prepare(self, job: VectorConvertJob) -> VectorConvertJob:
        """预处理：修正字符串字段长度，返回可直接写出的任务。

        GDB/ArcGIS 等来源常有「字段声明长度 < 实际数据长度」的脏数据
        （如 DLBM 声明 5 实际存 6 位代码），Shapefile 等驱动会拒绝超长值。
        先拷贝为字段长度已修正的临时图层，避免整层写出中途失败。

        大图层上字段扫描 / 拷贝可能耗时，调用方应在后台线程执行，避免卡 UI。
        """
        layer = job.layer
        if layer is None or not layer.isValid():
            raise ValueError("输入矢量图层无效")
        if not job.output_path.strip():
            raise ValueError("输出文件路径不能为空")
        source_layer = copy_with_widened_string_fields(layer, only_selected=job.only_selected)
        return replace(job, layer=source_layer)

    def write(
        self,
        job: VectorConvertJob,
        feedback: QgsFeedback | None = None,
    ) -> str:
        """执行 GDAL 写出（可在后台线程），返回实际输出路径。

        Args:
            job: 已经过 :meth:`prepare` 的转换参数。
            feedback: 可选的进度反馈；GDAL 会按要素写入进度更新它。

        Raises:
            ValueError: 输入图层无效或输出路径为空。
            RuntimeError: GDAL 写出失败。
        """
        from qgis.core import QgsCoordinateTransformContext, QgsProject, QgsVectorFileWriter

        layer = job.layer
        if layer is None or not layer.isValid():
            raise ValueError("输入矢量图层无效")
        if not job.output_path.strip():
            raise ValueError("输出文件路径不能为空")

        path = os.path.abspath(job.output_path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        layer_name = job.layer_name or os.path.splitext(os.path.basename(path))[0]

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = job.fmt.driver
        options.fileEncoding = "UTF-8"
        options.layerOptions = list(job.fmt.layer_options)
        options.layerName = layer_name
        options.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
        options.skipAttributeCreation = job.fmt.skip_attributes
        options.onlySelectedFeatures = job.only_selected
        if feedback is not None:
            options.feedback = feedback

        context = QgsProject.instance().transformContext()
        if context is None:
            context = QgsCoordinateTransformContext()
        logger.info(
            "执行矢量格式转换：%s → %s（%s，%s 要素）",
            layer.name(),
            path,
            job.fmt.label,
            layer.featureCount(),
        )
        error, message, new_path, _new_layer = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer,
            path,
            context,
            options,
        )
        if int(error) != 0:
            # 失败时删除半成品，避免用户误以为已完整写出
            _remove_partial_output(path)
            raise RuntimeError(f"格式转换失败（{job.fmt.label}）：{message or '未知错误'}")
        result_path = new_path or path
        logger.info("矢量格式转换写出完成：%s（格式=%s）", result_path, job.fmt.label)
        return result_path

    def count_output_features(self, path: str, fmt: VectorFormatSpec) -> int:
        """统计输出文件里的要素数，供写出后校验；无法统计时返回 ``-1``。

        KML / GML 的 OGR 驱动会整文件解析，统计可能反而触发 OOM，直接跳过。
        该函数可在后台线程调用。
        """
        if fmt.driver in {"KML", "GML"}:
            return -1
        ds = None
        try:
            from osgeo import ogr

            ds = ogr.Open(path, 0)
            if ds is None:
                return -1
            layer = ds.GetLayer(0)
            return -1 if layer is None else int(layer.GetFeatureCount())
        except Exception:
            logger.debug("统计输出要素数失败: %s", path, exc_info=True)
            return -1
        finally:
            ds = None

    def load_result(self, path: str, layer_name: str = "") -> QgsVectorLayer:
        """主线程把输出文件加载为图层（不加入工程）。"""
        from qgis.core import QgsVectorLayer

        name = layer_name or os.path.splitext(os.path.basename(path))[0]
        result = QgsVectorLayer(path, name, "ogr")
        if not result.isValid():
            raise RuntimeError(f"转换结果无法加载：{path}")
        logger.info("矢量格式转换完成：%s（%s 要素）", path, result.featureCount())
        return result

    def run(
        self,
        job: VectorConvertJob,
        feedback: QgsFeedback | None = None,
    ) -> QgsVectorLayer:
        """同步执行格式转换，返回目标文件对应的图层。"""
        prepared = self.prepare(job)
        path = self.write(prepared, feedback)
        return self.load_result(path, prepared.layer_name)
