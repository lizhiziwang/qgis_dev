"""主窗口菜单栏 → Tab 切换器回归测试。

菜单栏被改造为顶部 QTabBar：点不同 tab，下方工具条跟着切换；默认的
「地图工具」工具条放进「通用」tab，原「文件 / 查看 / 流程」菜单功能拆成
独立工具条挂到「文件 / 查看」tab。

窗口未 show 时 ``isVisible()`` 恒为 False，这里用 ``isHidden()`` 断言
``_on_tab_changed`` 对每个工具条设置的显式可见性（setVisible(False) → hidden）。

需要真实 QGIS（PyQt6 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")


@pytest.fixture
def win(qgis_app):
    """构造主窗口（不 show）。"""
    from app.ui.main_window.main_window import MainWindow

    w = MainWindow()
    yield w
    w.close()


def _visible(toolbars) -> bool:
    """工具条列表是否都处于"未显式隐藏"态。"""
    return all(not tb.isHidden() for tb in toolbars)


@pytest.mark.gui
def test_tab_bar_has_four_tabs_and_default_show_general(win):
    """顶部 Tab 顺序：文件/查看/编辑/通用，默认激活「文件」只显示文件工具条。"""
    tab_bar = win._tab_bar
    assert tab_bar.count() == 4
    assert [tab_bar.tabText(i) for i in range(4)] == ["文件", "查看", "编辑", "通用"]
    assert tab_bar.currentIndex() == 0

    # 默认只显示「文件」下的工具条；其余全部隐藏
    assert not win._file_toolbar.isHidden()
    assert win._view_toolbar.isHidden()
    assert win.digitizing_toolbar.digitizing_toolbar.isHidden()
    assert win.tool_bar.isHidden()


@pytest.mark.gui
def test_switching_tabs_swaps_visible_toolbar(win):
    """点不同 tab → 对应工具条显示、其余隐藏（互斥切换）。"""
    tab_bar = win._tab_bar
    expected = {
        0: [win._file_toolbar],
        1: [win._view_toolbar],
        2: [win.digitizing_toolbar.digitizing_toolbar],
        3: [win.tool_bar],
    }
    for idx, toolbars in expected.items():
        tab_bar.setCurrentIndex(idx)
        assert _visible(toolbars), f"tab {idx} 应显示其工具条"
        # 其余 tab 的工具条必须全隐藏
        for other, others in expected.items():
            if other != idx:
                assert all(tb.isHidden() for tb in others), f"tab {idx} 时 tab {other} 的工具条应隐藏"


@pytest.mark.gui
def test_file_toolbar_has_menu_actions_with_shortcuts(win):
    """「文件」工具条含 新建工程/打开工程/打开文件/退出，快捷键保留。"""
    acts = [a for a in win._file_toolbar.actions() if not a.isSeparator()]
    texts = [a.text() for a in acts]
    assert texts == ["新建工程", "打开工程", "打开文件", "退出"]
    by_text = {a.text(): a for a in acts}
    assert by_text["新建工程"].shortcut().toString() == "Ctrl+N"
    assert by_text["打开工程"].shortcut().toString() == "Ctrl+Shift+O"
    assert by_text["打开文件"].shortcut().toString() == "Ctrl+O"
    assert by_text["退出"].shortcut().toString() == "Ctrl+Q"


@pytest.mark.gui
def test_view_toolbar_log_action_opens_log_dock(win):
    """「查看」工具条点「日志」→ 懒创建 LogDock（原菜单行为）。"""
    acts = [a for a in win._view_toolbar.actions() if not a.isSeparator()]
    texts = [a.text() for a in acts]
    assert texts == ["日志", "流程节点编辑器"]
    assert win.log_dock is None
    acts[0].trigger()
    assert win.log_dock is not None


@pytest.mark.gui
def test_main_window_uses_configured_app_name(qgis_app):
    """MainWindow 透传 ``app_name``：标题用配置名、不再硬编码 ``qgis_dev``。"""
    from app.ui.main_window.main_window import MainWindow

    win = MainWindow(app_name="我的应用")
    try:
        assert win.windowTitle() == "我的应用"
        assert win._app_name == "我的应用"
    finally:
        win.close()


@pytest.mark.gui
def test_main_window_default_app_name_when_unspecified(win):
    """不传 ``app_name`` 时走默认 "qgis_dev"（向后兼容现有测试）。"""
    assert win.windowTitle() == "qgis_dev"
    assert win._app_name == "qgis_dev"
