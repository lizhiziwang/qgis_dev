"""UI 资源（图标、QSS 加载）。

全局 QSS 已由主题系统统一管理（:mod:`app.ui.common.theme` 的 ``app_qss``，
由 :class:`ThemeManager` 在启动时设到 QApplication），这里不再维护
硬编码的 ``apply_app_style``。
"""

from __future__ import annotations
