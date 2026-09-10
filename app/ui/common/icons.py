"""统一图标管理。

所有 ``QIcon("resource/png/...")`` 散落字符串走 :class:`Icons` 集中管理。
路径从 :class:`app.infra.fs.paths.Paths` 解析，确保打包后仍能找到图标。
"""

from __future__ import annotations

from qgis.PyQt.QtGui import QIcon

from app.infra.fs.paths import Paths


class Icons:
    """图标管理（统一从 :class:`Paths` 解析）。"""

    APP = ("png", "app_icon.png")
    REMOVE = ("png", "remove.png")
    REFRESH = ("png", "刷新.png")
    SAVE_EDIT = ("png", "保存编辑.png")
    STOP_EDIT = ("png", "停止编辑.png")
    FILTER = ("png", "属性筛选器.png")
    FIELD_CALC = ("png", "字段计算器.png")
    EDIT = ("png", "编辑.png")
    ZOOM_IN = ("svg", "zoom_in.svg")
    ZOOM_OUT = ("svg", "zoom_out.svg")

    @classmethod
    def _resolve(cls, name: tuple[str, ...]) -> str:

        return str(Paths.default().resource(*name))

    @classmethod
    def app(cls) -> QIcon:
        return QIcon(cls._resolve(cls.APP))

    @classmethod
    def remove(cls) -> QIcon:
        return QIcon(cls._resolve(cls.REMOVE))

    @classmethod
    def refresh(cls) -> QIcon:
        return QIcon(cls._resolve(cls.REFRESH))

    @classmethod
    def save_edit(cls) -> QIcon:
        return QIcon(cls._resolve(cls.SAVE_EDIT))

    @classmethod
    def stop_edit(cls) -> QIcon:
        return QIcon(cls._resolve(cls.STOP_EDIT))

    @classmethod
    def filter(cls) -> QIcon:
        return QIcon(cls._resolve(cls.FILTER))

    @classmethod
    def field_calc(cls) -> QIcon:
        return QIcon(cls._resolve(cls.FIELD_CALC))

    @classmethod
    def edit(cls) -> QIcon:
        return QIcon(cls._resolve(cls.EDIT))

    @classmethod
    def zoom_in(cls) -> QIcon:
        return QIcon(cls._resolve(cls.ZOOM_IN))

    @classmethod
    def zoom_out(cls) -> QIcon:
        return QIcon(cls._resolve(cls.ZOOM_OUT))

    @classmethod
    def custom(cls, *parts: str) -> QIcon:
        """自定义图标。"""
        return QIcon(cls._resolve(parts))
