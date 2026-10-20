"""字典管理领域（业务字典的存储、维护与 Excel 导入导出）。

界面入口见 :class:`app.ui.dialogs.dict_manage.DictManageDialog`；本包只依赖标准库
与 ``app.infra``，不 import UI。
"""

from __future__ import annotations

from app.core.domain.dict_manage.excel import (
    EXPORT_FILTER,
    IMPORT_FILTER,
    DictExportError,
    DictImportError,
    export_excel,
    import_excel,
    normalize_export_path,
    xlsx_available,
)
from app.core.domain.dict_manage.models import (
    DEFAULT_CATEGORIES,
    DictCategory,
    DictDomain,
    DictError,
    DictItem,
    DictRow,
    ImportReport,
)
from app.core.domain.dict_manage.service import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_SEARCH_LIMIT,
    DictService,
    default_db_path,
)

__all__ = [
    "DEFAULT_CATEGORIES",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_SEARCH_LIMIT",
    "EXPORT_FILTER",
    "IMPORT_FILTER",
    "DictCategory",
    "DictDomain",
    "DictError",
    "DictExportError",
    "DictImportError",
    "DictItem",
    "DictRow",
    "DictService",
    "ImportReport",
    "default_db_path",
    "export_excel",
    "import_excel",
    "normalize_export_path",
    "xlsx_available",
]
