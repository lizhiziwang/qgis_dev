"""影像标注（EISeg 风格）：数据模型、画布、面板与页面。"""

from __future__ import annotations


def __getattr__(name: str):
    """按需导出标注页面，避免导入数据模型时连带加载 Qt 面板。"""
    if name in ("AnnotationPage", "AnnotationWindow"):
        from app.ui.deep_learning.annotation_page import AnnotationPage

        return AnnotationPage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AnnotationPage", "AnnotationWindow"]
