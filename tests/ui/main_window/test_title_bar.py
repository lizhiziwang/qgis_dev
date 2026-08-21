"""标题栏（TitleBar）回归测试。

覆盖：
1. 任务模式 / 工程模式 切换控件存在且默认 ``MODE_PROJECT``
2. 点击切换触发 ``mode_changed`` 信号 + 内部状态同步
3. 程序化 ``set_mode`` 也能切换
4. 切换后其它控件仍可交互（按钮 hover / 切换主题不被影响）
5. 模式切换控件进入 _interactive 列表（frameless mixin 命中判定）
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")


@pytest.fixture
def title_bar(qgis_app):
    """构造真实 TitleBar（与 main_window.py 用法一致）。"""
    from PyQt6.QtWidgets import QMainWindow

    from app.ui.main_window.title_bar import TitleBar

    win = QMainWindow()
    tb = TitleBar(win)
    yield tb, win
    tb.deleteLater()
    win.close()
    win.deleteLater()


@pytest.mark.gui
def test_mode_switch_default_is_project_mode(title_bar):
    """默认模式必须是 ``MODE_PROJECT``（GIS 工程模式更常用）。"""
    from app.ui.main_window.title_bar import MODE_PROJECT

    tb, _ = title_bar
    sw = tb._mode_switch
    assert sw.mode == MODE_PROJECT
    # 选中的按钮对应「工程模式」
    labels = [b.text() for b in sw._buttons.values()]
    assert labels == ["任务模式", "工程模式"]
    assert sw._buttons[MODE_PROJECT].isChecked()
    assert not sw._buttons["task"].isChecked()


@pytest.mark.gui
def test_mode_switch_emits_signal_on_click(title_bar, qgis_app):
    """点击「任务模式」→ ``mode_changed(MODE_TASK)`` 被 emit + 内部状态更新。"""
    from app.ui.main_window.title_bar import MODE_PROJECT, MODE_TASK

    tb, _ = title_bar
    received: list[str] = []
    tb.mode_changed.connect(lambda k: received.append(k))

    sw = tb._mode_switch
    # 模拟点击「任务模式」按钮
    sw._buttons[MODE_TASK].click()
    qgis_app.processEvents()

    assert received == [MODE_TASK], f"应收到 [{MODE_TASK}]，实际 {received}"
    assert sw.mode == MODE_TASK
    assert sw._buttons[MODE_TASK].isChecked()
    assert not sw._buttons[MODE_PROJECT].isChecked()


@pytest.mark.gui
def test_mode_switch_set_mode_programmatic(title_bar, qgis_app):
    """``set_mode`` 程序化切换：不重复 emit 信号 / 状态一致。"""
    from app.ui.main_window.title_bar import MODE_PROJECT, MODE_TASK

    tb, _ = title_bar
    received: list[str] = []
    tb.mode_changed.connect(lambda k: received.append(k))

    sw = tb._mode_switch

    # 切换到任务模式：emit=True（默认）
    sw.set_mode(MODE_TASK)
    assert received == [MODE_TASK]
    assert sw.mode == MODE_TASK

    # 切换到工程模式
    sw.set_mode(MODE_PROJECT)
    assert received == [MODE_TASK, MODE_PROJECT]
    assert sw.mode == MODE_PROJECT

    # 切换到相同模式：不 emit、不触发信号
    sw.set_mode(MODE_PROJECT)
    assert received == [MODE_TASK, MODE_PROJECT], "重复 set 不应再 emit"

    # emit=False：程序化但不发信号
    sw.set_mode(MODE_TASK, emit=False)
    assert received == [MODE_TASK, MODE_PROJECT], "emit=False 必须不发信号"
    assert sw.mode == MODE_TASK


@pytest.mark.gui
def test_mode_switch_in_interactive_list(title_bar):
    """模式切换控件必须进入 TitleBar._interactive，否则 frameless mixin 会
    把它当成「标题栏空白区」做拖动 → 点击切不动。

    修复前 bug：忘了把新控件加进 _interactive 列表，WM_NCHITTEST 走
    HTCAPTION → 拖动窗口而不是切模式。
    """
    tb, _ = title_bar
    sw = tb._mode_switch
    assert sw in tb._interactive, (
        "模式切换控件必须加入 TitleBar._interactive，避免 frameless mixin "
        "误判为拖动区"
    )


@pytest.mark.gui
def test_mode_switch_apply_theme_changes_qss(title_bar):
    """切换主题后，模式切换控件的 QSS 必须被刷新（颜色跟随主题）。"""
    from app.ui.common.theme import ThemeManager

    tb, _ = title_bar
    sw = tb._mode_switch

    # 显式回浅色基线：ThemeManager 单例 + QSettings 持久化会把上次运行（探针/
    # 其它测试）的主题带进来，基线不固定会导致 set_theme("dark") 成 no-op、对比失效。
    ThemeManager.instance().set_theme("light")
    tb._apply_theme(ThemeManager.instance().current())

    original_qss = sw.styleSheet()
    assert original_qss, "模式切换控件必须自带 QSS"

    # 切到深色主题
    ThemeManager.instance().set_theme("dark")
    tb._apply_theme(ThemeManager.instance().current())
    assert sw.styleSheet() != original_qss, "切换主题后模式切换控件 QSS 必须刷新"

    # 切回浅色
    ThemeManager.instance().set_theme("light")
    tb._apply_theme(ThemeManager.instance().current())
    assert sw.styleSheet() != "", "切回浅色主题后 QSS 必须非空"


@pytest.mark.gui
def test_mode_switch_container_renders_background(title_bar):
    """容器必须开 WA_StyledBackground。

    否则轨道背景（menu_bg）不渲染、容器透明，蓝/绿主题下选中段
    （input_focus == title_bg，与标题栏同色）会隐形，看不出选中态。
    """
    from PyQt6.QtCore import Qt

    tb, _ = title_bar
    sw = tb._mode_switch
    assert sw.testAttribute(Qt.WidgetAttribute.WA_StyledBackground), (
        "模式切换容器缺 WA_StyledBackground：QSS 背景不渲染，蓝/绿主题下"
        "选中段与标题栏同色会隐形"
    )


@pytest.mark.gui
def test_mode_switch_blue_theme_selected_differs_from_title_bar(title_bar):
    """蓝色主题下选中段必须与标题栏不同色。

    蓝/绿主题 input_focus == title_bg（标题栏即主题主色）。选中段改用深色
    menu_text 打底（白字照常），未选中段用浅色 menu_bg 轨道色 → 选中态清晰。
    """
    from app.ui.common.theme import THEMES

    tb, _ = title_bar
    sw = tb._mode_switch
    blue = THEMES["blue"]
    sw.apply_theme(blue)
    qss = sw.styleSheet()
    assert blue.title_bg == blue.input_focus, "前提：蓝主题标题栏与 input_focus 同色"
    assert blue.menu_bg in qss, "轨道背景色（menu_bg）必须出现在 QSS 里"
    assert blue.menu_text in qss, "选中段回退色（menu_text）必须出现在 QSS 里"
    assert blue.input_focus not in qss, "蓝主题选中段不应再用与标题栏同色的 input_focus"


def test_mode_switch_qss_selected_differs_from_title_bar():
    """选中段背景色在所有主题下都必须与标题栏（title_bg）不同。

    纯函数测试（只调 ``_mode_switch_qss``，不需要 QApplication）：蓝/绿主题
    input_focus == title_bg 时应回退到深色 menu_text，其余主题用 input_focus。
    """
    import re

    from app.ui.common.theme import THEMES
    from app.ui.main_window.title_bar import _mode_switch_qss

    for key, theme in THEMES.items():
        qss = _mode_switch_qss(theme)
        m = re.search(
            r"QPushButton#ModeSwitchBtn:checked \{.*?background-color: ([^;]+);.*?\}",
            qss,
            re.S,
        )
        assert m, f"{key}: 缺少选中段 QSS"
        sel = m.group(1).strip()
        assert sel != theme.title_bg, f"{key}: 选中段 {sel} 与标题栏 {theme.title_bg} 同色"
        if theme.input_focus == theme.title_bg:
            assert sel == theme.menu_text, f"{key}: input_focus==title_bg 时应回退 menu_text"
        else:
            assert sel == theme.input_focus, f"{key}: 正常应使用 input_focus"
        # 彩色主题（标题栏=主色）下未选中段也不能与标题栏同色（不能透出标题栏色）
        if theme.input_focus == theme.title_bg:
            base = re.search(
                r"QPushButton#ModeSwitchBtn \{.*?background-color: ([^;]+);.*?\}",
                qss,
                re.S,
            )
            assert base, f"{key}: 缺少未选中段 QSS"
            unsel = base.group(1).strip()
            assert unsel != theme.title_bg, f"{key}: 未选中段 {unsel} 与标题栏同色"


@pytest.mark.gui
def test_mode_switch_exclusive_group(title_bar, qgis_app):
    """QButtonGroup 必须 exclusive——同时只能有一个被选中。"""
    tb, _ = title_bar
    sw = tb._mode_switch
    assert sw._group.exclusive()

    # 默认：工程模式被选中
    assert sw._buttons["project"].isChecked()
    assert sum(1 for b in sw._buttons.values() if b.isChecked()) == 1

    # 点任务模式
    sw._buttons["task"].click()
    qgis_app.processEvents()
    assert sum(1 for b in sw._buttons.values() if b.isChecked()) == 1
    assert sw._buttons["task"].isChecked()
    assert not sw._buttons["project"].isChecked()