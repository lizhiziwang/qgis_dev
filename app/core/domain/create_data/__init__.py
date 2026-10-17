"""创建矢量数据领域。

两条并列的创建路径，各有各的 spec / service：

- 建**单图层数据**（shp / GeoJSON / KML…，一个文件一层）：
  :class:`CreateDataService` + :class:`CreateDataSpec`；
- 建**本地库容器**（GeoPackage / SpatiaLite，一个文件多图层）：建空库用
  :class:`CreateContainerService`，往已有库里加图层用
  :class:`CreateLayerInDbService`（见 :mod:`.library`）。

两条路径的 spec 都带 ``fields``（向导第二步「数据字段设置」填的字段结构）：
字段模型、类型映射与落盘口径在 :mod:`.fields`，Excel 模板读写在 :mod:`.template`。
"""

from __future__ import annotations

from .fields import (
    FIELD_TYPES,
    MAX_FIELD_LENGTH,
    MAX_FIELD_PRECISION,
    MZGUID_FIELD,
    MZGUID_FIELD_NAME,
    FieldSpec,
    FieldType,
    configure_created_layer,
    fields_from_layer,
    match_layer_fields,
    qgs_field,
    sqlite_type,
    to_int,
    validate_fields,
    with_mzguid,
)
from .library import (
    CreateContainerService,
    CreateContainerSpec,
    CreateLayerInDbService,
    CreateLayerInDbSpec,
    CreateLibraryError,
    SUPPORTED_CONTAINER_DRIVERS,
)
from .service import CreateDataError, CreateDataService, CreateDataSpec, available_drivers
from .template import (
    TEMPLATE_EXTENSION,
    TEMPLATE_FILTER,
    TEMPLATE_HEADERS,
    TEMPLATE_READ_FILTER,
    FieldTemplateError,
    read_template,
    write_template,
    xlsx_available,
)

__all__ = [
    "FIELD_TYPES",
    "MAX_FIELD_LENGTH",
    "MAX_FIELD_PRECISION",
    "MZGUID_FIELD",
    "MZGUID_FIELD_NAME",
    "SUPPORTED_CONTAINER_DRIVERS",
    "TEMPLATE_EXTENSION",
    "TEMPLATE_FILTER",
    "TEMPLATE_HEADERS",
    "TEMPLATE_READ_FILTER",
    "CreateContainerService",
    "CreateContainerSpec",
    "CreateDataError",
    "CreateDataService",
    "CreateDataSpec",
    "CreateLayerInDbService",
    "CreateLayerInDbSpec",
    "CreateLibraryError",
    "FieldSpec",
    "FieldTemplateError",
    "FieldType",
    "available_drivers",
    "configure_created_layer",
    "fields_from_layer",
    "match_layer_fields",
    "qgs_field",
    "read_template",
    "sqlite_type",
    "to_int",
    "validate_fields",
    "with_mzguid",
    "write_template",
    "xlsx_available",
]
