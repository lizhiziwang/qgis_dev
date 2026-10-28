"""矢量数据格式转换领域服务。

使用 GDAL/OGR（经 :class:`QgsVectorFileWriter`）把矢量图层写出为常见格式，
不依赖 QGIS Processing 插件。格式清单集中定义在 :data:`VECTOR_FORMATS`，
参数对话框与说明文档共用同一份元数据。

GeoPackage / SQLite 是本工程「数据管理」能直接接管的容器：写出后会补一行
``user_relation`` 注册（见 :meth:`VectorConvertService._register_managed_layer`），
并且写入已有库时只替换同名图层，不整库重建。
"""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from app.core.domain.geom_cleanup import copy_field_aliases, copy_with_widened_string_fields
from app.infra.logging import get_logger

if TYPE_CHECKING:
    from qgis.core import QgsFeedback, QgsVectorLayer

logger = get_logger(__name__)

#: 能作为「本工程数据管理库」的驱动 —— 写出后要补一行 ``user_relation`` 注册。
#: 数据管理的目录只读注册表、不扫物理表（见 ``local_db_util.query_all_table_name``），
#: GDAL 直写的库没有这张注册行，不补就「有数据但面板里看不到」。
_MANAGED_DB_DRIVERS = frozenset({"GPKG", "SQLite"})

#: 本工程按「库」打开（可多图层）的后缀；加载 / 统计要素数要走表名定位。
_LOCAL_DB_EXTENSIONS = frozenset({".gpkg", ".sqlite", ".sqlite3", ".db"})


def _actual_table_name(path: str, layer_name: str) -> str | None:
    """查写出后的真实表名（SQLite 驱动可能把名字改成小写）；查不到返回 ``None``。

    只在本地库后缀上调用；普通文件格式没有「表名」这个概念。
    """
    import sqlite3

    try:
        conn = sqlite3.connect(path, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND lower(name) = lower(?)",
                (layer_name,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return str(row[0]) if row is not None else None


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
        options.layerOptions = self._layer_options(job.fmt, layer)
        options.layerName = layer_name
        existed = os.path.exists(path)
        if existed and job.fmt.driver in _MANAGED_DB_DRIVERS:
            # 已有的 GPKG / SQLite 只替换同名图层。默认的 CreateOrOverwriteFile
            # 会把**整个库**删掉重建，库里其它图层连同数据管理注册行一起丢失。
            options.actionOnExistingFile = (
                QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            )
        else:
            options.actionOnExistingFile = (
                QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
            )
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
            # 只清理本次新建的半成品。覆盖既有文件（尤其是已有 GPKG / SQLite）
            # 时删掉会把用户原来的库 / 其它图层一起毁掉。
            if not existed:
                _remove_partial_output(path)
            raise RuntimeError(f"格式转换失败（{job.fmt.label}）：{message or '未知错误'}")
        result_path = new_path or path
        if job.fmt.driver in _MANAGED_DB_DRIVERS:
            # 数据管理按 user_relation 展示，GDAL 不会写这行注册；缺了它，
            # 用户把产出库加进数据管理会看到「没有任何图层」。
            self._register_managed_layer(result_path, layer_name)
        logger.info("矢量格式转换写出完成：%s（格式=%s）", result_path, job.fmt.label)
        return result_path

    @staticmethod
    def _layer_options(fmt: VectorFormatSpec, layer: QgsVectorLayer) -> list[str]:
        """按目标格式 / 输入字段生成创建选项。

        GPKG 的要素 ID 列默认叫 ``fid``（必须是整数主键）。GDB 图层若自带
        名为 ``fid`` 的字段（常见为 Real），GDAL 会报
        ``Wrong field type for fid`` 并整体失败。这里把 FID 列改名成不冲突的
        名字，用户字段 ``fid`` 原样保留。
        """
        options = list(fmt.layer_options)
        if fmt.driver != "GPKG":
            return options
        field_names = {field.name().casefold() for field in layer.fields()}
        if "fid" not in field_names:
            return options
        fid_name = "ogr_fid"
        while fid_name.casefold() in field_names:
            fid_name += "_1"
        return [*options, f"FID={fid_name}"]

    @staticmethod
    def _register_managed_layer(path: str, layer_name: str) -> None:
        """把刚写出的图层登记进库内 ``user_relation``（type=spatial）。

        整份文件的写出（新建 / 覆盖单文件格式）不需要清理旧注册；但已有库走
        ``CreateOrOverwriteLayer`` 时同名注册行还在，所以先按真实表名删一次再写，
        保证反复转换同一图层不会出现重复行。

        登记失败只记 warning：数据已经完整写出，注册只是数据管理的展示补充。
        """
        from app.core.util.local_db_util import local_db_util

        actual = layer_name
        try:
            # SQLite 驱动可能把表名改成小写：按真实名字注册，否则面板上
            # 双击打开 / 删除都对不上表。
            found = _actual_table_name(path, layer_name)
            if found is None:
                logger.warning(
                    "格式转换结果里找不到图层表，跳过数据管理登记：%s（%s）",
                    layer_name,
                    path,
                )
                return
            actual = found
            db = local_db_util(path)
            try:
                # 老库 / 刚由 OGR 建出的库可能还没有 user_relation：DELETE 失败
                # 没关系，紧接着的 add_domain_table 会幂等建表再登记。
                with contextlib.suppress(Exception):
                    db.cursor.execute(
                        "DELETE FROM user_relation WHERE table_name = ?", (actual,)
                    )
                db.add_domain_table(actual, "spatial")
            finally:
                db.conn.close()
        except Exception:  # noqa: BLE001 —— 注册失败不毁已写出的数据
            logger.warning(
                "格式转换结果登记到数据管理失败：%s（%s）", layer_name, path, exc_info=True
            )
            return
        logger.info("格式转换结果已登记到数据管理：%s（%s）", actual, path)

    def count_output_features(
        self, path: str, fmt: VectorFormatSpec, layer_name: str = ""
    ) -> int:
        """统计输出文件里的要素数，供写出后校验；无法统计时返回 ``-1``。

        已有 GPKG / SQLite 追加图层时文件里有多层，必须按 ``layer_name``
        定位刚写出的那层，不能取第 0 层。

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
            layer = None
            if layer_name:
                wanted = layer_name.casefold()
                for index in range(ds.GetLayerCount()):
                    candidate = ds.GetLayerByIndex(index)
                    if candidate is not None and candidate.GetName().casefold() == wanted:
                        layer = candidate
                        break
            if layer is None:
                if layer_name and ds.GetLayerCount() > 1:
                    return -1
                layer = ds.GetLayer(0)
            return -1 if layer is None else int(layer.GetFeatureCount())
        except Exception:
            logger.debug("统计输出要素数失败: %s", path, exc_info=True)
            return -1
        finally:
            ds = None

    def load_result(
        self,
        path: str,
        layer_name: str = "",
        source_layer: QgsVectorLayer | None = None,
    ) -> QgsVectorLayer:
        """主线程把输出文件加载为图层（不加入工程）。

        GPKG / SQLite 可能是多图层库（往已有库追加），要按表名定位刚写出的
        图层 —— 直接 ``QgsVectorLayer(path, …)`` 会打开库里的第 0 层。
        ``source_layer`` 给定时把它的字段别名补到结果（部分格式 / 驱动写出时
        不保留别名）。
        """
        from qgis.core import QgsVectorLayer

        name = layer_name or os.path.splitext(os.path.basename(path))[0]
        ext = os.path.splitext(path)[1].lower()
        if ext in _LOCAL_DB_EXTENSIONS:
            from app.core.util.local_layer_loader import open_local_vector_table

            table = _actual_table_name(path, name) or name
            result, error = open_local_vector_table(path, table)
            if result is None:
                raise RuntimeError(f"转换结果无法加载：{path}（{error}）")
        else:
            result = QgsVectorLayer(path, name, "ogr")
            if not result.isValid():
                raise RuntimeError(f"转换结果无法加载：{path}")
        logger.info("矢量格式转换完成：%s（%s 要素）", path, result.featureCount())
        if source_layer is not None:
            copy_field_aliases(source_layer, result)
        return result

    def run(
        self,
        job: VectorConvertJob,
        feedback: QgsFeedback | None = None,
    ) -> QgsVectorLayer:
        """同步执行格式转换，返回目标文件对应的图层。"""
        prepared = self.prepare(job)
        path = self.write(prepared, feedback)
        return self.load_result(path, prepared.layer_name, source_layer=prepared.layer)
