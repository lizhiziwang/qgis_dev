"""对话框基类（BaseDialog）无边框 + 精简标题栏回归测试。

覆盖：
1. BaseDialog 装配出无边框标题栏，且只保留 图标/标题/最小化/最大化/关闭
   （用户信息、主题切换、模式切换不存在）。
2. 子类经 ``self.setLayout(content)`` 加的内容出现在标题栏之下（setLayout
   重写路由进 ``_frameless_layout``），不替换无边框布局。

需要真实 QGIS（PyQt5 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QLabel, QVBoxLayout  # noqa: E402

from app.ui.common.base_dialog import BaseDialog  # noqa: E402


class _TestDialog(BaseDialog):
    """最小 BaseDialog 子类：经 setLayout 挂一块内容。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, title="测试对话框")
        self.label = QLabel("content")
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout()
        layout.addWidget(self.label)
        self.setLayout(layout)


@pytest.mark.gui
def test_base_dialog_has_minimal_title_bar(qgis_app):
    """标题栏精简：无 用户/主题/模式，三个窗口按钮齐全。"""
    dlg = _TestDialog()
    tb = dlg.title_bar
    assert tb is not None
    assert tb._user_btn is None, "对话框标题栏不应有用户信息"
    assert tb._btn_theme is None, "对话框标题栏不应有主题切换"
    assert tb._mode_switch is None, "对话框标题栏不应有模式切换"
    for btn in (tb._btn_min, tb._btn_max, tb._btn_close):
        assert btn in tb._interactive
    assert tb._icon_label is not None
    assert tb._title_label is not None
    dlg.close()
    dlg.deleteLater()


@pytest.mark.gui
def test_base_dialog_content_routed_below_title_bar(qgis_app):
    """子类 ``setLayout(content)`` 挂到内容容器（标题栏之下），不替换无边框布局。"""
    dlg = _TestDialog()
    fl = dlg._frameless_layout
    assert fl is dlg.layout(), "无边框布局必须是 dialog 顶层布局"
    assert fl.count() >= 2, "无边框布局应含 标题栏 + 内容容器"
    assert fl.itemAt(0).widget() is dlg.title_bar
    assert fl.itemAt(1).widget() is dlg._content
    # 内容 label 已挂进内容容器（标准 QWidget.setLayout 重挂父级），是 dialog 后代
    assert dlg.label.parent() is dlg._content, "内容 label 必须重挂到内容容器"
    assert dlg.label.window() is dlg
    dlg.close()
    dlg.deleteLater()


@pytest.mark.gui
def test_base_dialog_default_min_size_and_icon(qgis_app):
    """基类默认：应用图标 + 最小尺寸 800×600。"""
    dlg = _TestDialog()
    assert dlg.minimumWidth() >= 800
    assert dlg.minimumHeight() >= 600
    assert not dlg.windowIcon().isNull()
    dlg.close()
    dlg.deleteLater()
