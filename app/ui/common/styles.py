"""UI 通用样式（已并入主题系统）。

本模块历史上有 QSS 字符串集中（``TITLE_BAR_QSS`` / ``DIALOG_BASE_QSS`` /
``TEXT_BROWSER_QSS`` / ``CANVAS_ZOOM_*``）与 ``apply_*_style`` 助手，
随着主题系统（:mod:`app.ui.common.theme`）落地，所有要素颜色统一由
:class:`Theme` 管理：主窗体 chrome 用 ``window_qss / toolbar_qss /
title_bar_qss``，弹窗 / 右键菜单 / 通用控件用 ``app_qss``（QApplication 级）。

保留本模块仅为兼容历史 import；新代码请直接使用 :mod:`app.ui.common.theme`。
"""

from __future__ import annotations
