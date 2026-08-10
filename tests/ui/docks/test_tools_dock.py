"""工具树点击复用对话框实例回归测试。

修复前（设计缺陷）：每次点击工具节点都 ``new Dialog().show()``，可无限叠加窗口。
修复后：同一算法只维护一个窗口实例 —— 已打开时点击置前（raise + activateWindow），
关闭后点击重新 show 同一实例，不新建。

需要真实 QGIS（PyQt5 + QgsApplication）；CI 默认 ``-m 'not gui'`` 跳过。
"""

from __future__ import annotations

import pytest

# tools_dock 模块顶层 import PyQt5；非 GUI 环境（如纯 pytest CI）无法收集，
# 必须先跳过整个模块，避免 collection error。
pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QDialog  # noqa: E402

from app.ui.docks import tools_dock  # noqa: E402
from app.ui.docks.tools_dock import ToolsDock  # noqa: E402

_CODE = "native:buffer"


class _FakeDialog(QDialog):
    """轻量假窗口：替代真实算法对话框，只用于验证实例复用逻辑。"""


def _find_item_by_code(tree, code):
    """按 ``_code_`` 在树里找叶子节点（树已改为多分组，不再假设单一分组）。"""
    for i in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(i)
        for j in range(group.childCount()):
            item = group.child(j)
            if getattr(item, "_code_", "") == code:
                return item
    return None


def _leaf_items(tree):
    """收集所有分组的叶子工具节点。"""
    items = []
    for i in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(i)
        for j in range(group.childCount()):
            items.append(group.child(j))
    return items


@pytest.fixture
def dock_with_fake_factory(qgis_app):
    """把 buffer 的对话框工厂换成假窗口，返回 (dock, 对应树节点)。"""
    original = tools_dock._DIALOG_FACTORIES[_CODE]
    tools_dock._DIALOG_FACTORIES[_CODE] = lambda parent: _FakeDialog(parent)
    dock = None
    try:
        dock = ToolsDock()
        item = _find_item_by_code(dock.tree, _CODE)  # 矢量缓冲区
        assert item is not None and item._code_ == _CODE
        yield dock, item
    finally:
        tools_dock._DIALOG_FACTORIES[_CODE] = original
        if dock is not None:
            dock.deleteLater()


@pytest.mark.gui
def test_same_tool_click_reuses_dialog(dock_with_fake_factory):
    """同一工具连续点击只维护一个窗口实例，关闭后点击仍复用。"""
    dock, item = dock_with_fake_factory

    # 第一次点击：创建并 show
    dock._on_item_clicked(item, 0)
    first = dock._dialogs[_CODE]
    assert isinstance(first, _FakeDialog)
    assert first.isVisible()

    # 第二次点击：复用同一实例，不新建
    dock._on_item_clicked(item, 0)
    assert dock._dialogs[_CODE] is first
    assert first.isVisible()

    # 关闭后点击：仍复用同一实例（重新 show），不是新建
    first.close()
    assert not first.isVisible()
    dock._on_item_clicked(item, 0)
    assert dock._dialogs[_CODE] is first
    assert first.isVisible()


@pytest.mark.gui
def test_different_tools_get_different_dialogs(qgis_app):
    """不同算法节点各自维护独立窗口实例，互不覆盖。"""
    original = dict(tools_dock._DIALOG_FACTORIES)

    def fake(parent, **kwargs):
        return _FakeDialog(parent)

    for code in tools_dock._DIALOG_FACTORIES:
        tools_dock._DIALOG_FACTORIES[code] = fake
    dock = None
    try:
        dock = ToolsDock()
        # 所有分组的叶子工具节点都要能独立打开
        leaf_items = _leaf_items(dock.tree)
        assert len(leaf_items) >= 3
        for item in leaf_items:
            dock._on_item_clicked(item, 0)
        assert len(dock._dialogs) == len(leaf_items)
        ids = {id(d) for d in dock._dialogs.values()}
        assert len(ids) == len(leaf_items)
    finally:
        tools_dock._DIALOG_FACTORIES.clear()
        tools_dock._DIALOG_FACTORIES.update(original)
        if dock is not None:
            dock.deleteLater()
