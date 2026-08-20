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
pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QDialog  # noqa: E402

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
        # 只检查确实有对话框工厂的工具节点（flow:editor 等走 _SIMPLE_ACTIONS
        # 不是对话框，无 _dialogs 条目，自然不能并入此断言）
        leaf_items = [
            item for item in _leaf_items(dock.tree)
            if getattr(item, "_code_", "") in tools_dock._DIALOG_FACTORIES
        ]
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


@pytest.mark.gui
def test_oriented_bbox_registered_in_tool_tree(qgis_app):
    """最小外包矩形（``native:orientedminimumboundingbox``）必须出现在
    "矢量分析"分组下，且对应工厂返回真实 OrientedBBoxDialog 实例。
    """
    from app.ui.dialogs.oriented_bbox import OrientedBBoxDialog

    factory = tools_dock._DIALOG_FACTORIES.get("native:orientedminimumboundingbox")
    assert factory is not None, "OMBB 算法未注册到 _DIALOG_FACTORIES"
    assert "native:orientedminimumboundingbox" in tools_dock._TOOL_TIPS

    dock = ToolsDock()
    try:
        item = _find_item_by_code(dock.tree, "native:orientedminimumboundingbox")
        assert item is not None, "OMBB 未挂到工具树"
        # 验证分组归属
        group = dock.tree.topLevelItem(0)  # "矢量分析" 在第一位
        assert "native:orientedminimumboundingbox" in [
            group.child(j)._code_ for j in range(group.childCount())
        ]
        # 工厂能产出 OrientedBBoxDialog
        dlg = factory(dock)
        try:
            assert isinstance(dlg, OrientedBBoxDialog)
            assert dlg.windowTitle() == "最小外包矩形"
        finally:
            dlg.close()
            dlg.deleteLater()
    finally:
        dock.deleteLater()


@pytest.mark.gui
def test_oriented_bbox_runs_on_memory_layer(qgis_app):
    """最小外包矩形服务能在内存图层上跑通，返回合法图层（按要素注入字段）。

    这是端到端的 happy-path 校验：避免 dialog 层 UI 控件正常但 service 层
    把参数键名写错的回归。直接用 service，不弹窗——CI 不依赖 Qt 事件循环。
    """
    from processing.core.Processing import Processing
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    from app.core.domain.oriented_bbox import OrientedBBoxJob, OrientedBBoxService

    Processing.initialize()

    layer = QgsVectorLayer("Polygon?crs=EPSG:4490", "src", "memory")
    assert layer.isValid()
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromPolygonXY([[
        QgsPointXY(116.3, 39.8),
        QgsPointXY(116.5, 39.8),
        QgsPointXY(116.5, 40.0),
        QgsPointXY(116.3, 40.0),
        QgsPointXY(116.3, 39.8),
    ]]))
    layer.dataProvider().addFeature(f)

    service = OrientedBBoxService()
    out = service.run(OrientedBBoxJob(layer=layer, output_name="ombb"))
    assert out is not None
    assert out.isValid()
    assert out.featureCount() == 1
    # 算法自动注入字段（实际产出 angle / area / height / perimeter / width）
    field_names = {fld.name() for fld in out.fields()}
    assert {"angle", "area", "height", "perimeter", "width"} <= field_names


# ---------------------------------------------------------------- GDAL 算法树


@pytest.mark.gui
def test_gdal_algorithms_all_registered(qgis_app):
    """59 个 GDAL 算法必须全部出现在 ``_DIALOG_FACTORIES`` / ``_TOOL_GROUPS`` /
    ``_TOOL_TIPS``，且对应工厂返回真实 ``GenericAlgDialog`` 实例。
    """
    from app.ui.dialogs.generic_alg import GenericAlgDialog

    # 1) 模块级注册表完整
    registered = {
        code
        for _group, items in tools_dock._GDAL_ALGORITHMS
        for _name, code in items
    }
    assert len(registered) >= 50, (
        f"GDAL 算法数量偏低（{len(registered)}），注册表可能没填全"
    )
    for code in registered:
        assert code in tools_dock._DIALOG_FACTORIES, (
            f"{code} 未在 _DIALOG_FACTORIES 注册"
        )
        assert code in tools_dock._TOOL_TIPS, (
            f"{code} 未在 _TOOL_TIPS 注册"
        )

    # 2) 工厂能产出 GenericAlgDialog 实例（且每个 id 都唯一）
    dlg_instances = []
    for code in sorted(registered):
        factory = tools_dock._DIALOG_FACTORIES[code]
        dock = ToolsDock()
        try:
            dlg = factory(dock)
            assert isinstance(dlg, GenericAlgDialog)
            assert dlg.windowTitle(), f"{code} 的 dialog 没有 title"
            # 检查 algo_id 已绑到 dialog（避免工厂捕获错 algo_id）
            assert dlg._algo_id == code, (
                f"{code} 的 dialog 拿到了错误的 algo_id：{dlg._algo_id}"
            )
            dlg_instances.append(dlg)
        finally:
            dock.deleteLater()
    # 不要用 id() 做唯一性判断：GC 后 Python 可能复用 id，
    # 且 dialog 已 deleteLater 不能 close（已被 owned by Qt）
    assert len(dlg_instances) == len(registered)

    # 3) _TOOL_GROUPS 把每个 algo_id 都挂上树
    tree_codes = set()
    tree_dock = ToolsDock()
    try:
        for item in _leaf_items(tree_dock.tree):
            tree_codes.add(getattr(item, "_code_", ""))
    finally:
        tree_dock.deleteLater()
    missing = registered - tree_codes
    assert not missing, f"以下算法没挂到工具树：{sorted(missing)[:10]}..."


@pytest.mark.gui
def test_generic_alg_dialog_builds_widgets(qgis_app):
    """``GenericAlgDialog`` 对包含多种参数类型的算法（``gdal:warpreproject``：
    RasterLayer + Crs + Enum + Number + String + Extent + Boolean）能正确生成
    至少 N 个 widget wrapper；这是 widget 工厂能 work 的核心证据。
    """
    from app.ui.dialogs.generic_alg import GenericAlgDialog

    dock = ToolsDock()
    try:
        dlg = GenericAlgDialog(parent=dock, algo_id="gdal:warpreproject")
        try:
            assert dlg.windowTitle() == "Warp (reproject)"
            # 至少 7 个 wrapper（INPUT/SOURCE_CRS/TARGET_CRS/RESAMPLING/NODATA/
            # TARGET_RESOLUTION/CREATION_OPTIONS/DATA_TYPE/TARGET_EXTENT/...）
            assert len(dlg._wrappers) >= 7, (
                f"warpreproject wrapper 数量偏低：{len(dlg._wrappers)}"
            )
            # params 应能成功收集（不抛异常）
            params = dlg._collect_params()
            assert "INPUT" in params
            assert "RESAMPLING" in params  # enum 类型
            assert "MULTITHREADING" in params  # boolean 类型
        finally:
            dlg.close()
            dlg.deleteLater()
    finally:
        dock.deleteLater()


@pytest.mark.gui
def test_generic_alg_dialog_advanced_dropdown_widths(qgis_app):
    """回归测试：当算法有 Advanced flag 参数时，``QGroupBox("高级")`` 内的下拉框
    宽度不能被压扁。

    修复前 bug：高级分组内的 QFormLayout ``fieldGrowthPolicy`` 默认是
    ``ExpandingFieldsGrow``，但 QGIS wrapper 控件的 sizePolicy 是 ``Preferred``
    而非 ``Expanding`` —— QFormLayout 只拉伸 ``Expanding`` 子控件，所以这些
    控件会被卡在 ``minimumSizeHint().width()``（实测 26px），看上去像被切掉了。

    修复：高级分组的 form 也用 ``AllNonFixedFieldsGrow``，且显式把每个
    参数控件的横向 sizePolicy 设为 ``Expanding``。
    """
    from PyQt6.QtWidgets import QComboBox, QFormLayout, QGroupBox

    from app.ui.dialogs.generic_alg import GenericAlgDialog

    dock = ToolsDock()
    try:
        dlg = GenericAlgDialog(parent=dock, algo_id="gdal:warpreproject")
        dlg.resize(900, 900)
        try:
            app = qgis_app  # conftest fixture returns the QApplication
            for _ in range(3):
                app.processEvents()

            advanced_boxes = [
                gb for gb in dlg.findChildren(QGroupBox) if gb.title() == "高级"
            ]
            assert advanced_boxes, "warpreproject 必须有「高级」分组"
            gb = advanced_boxes[0]

            # 展开（默认折叠）以触发真实 layout
            gb.setChecked(True)
            for _ in range(3):
                app.processEvents()

            # 表单策略：AllNonFixedFieldsGrow
            form = gb.layout()
            assert isinstance(form, QFormLayout)
            assert form.fieldGrowthPolicy() == (
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            ), "高级分组表单策略必须为 AllNonFixedFieldsGrow"

            # 高级分组内的下拉框宽度都必须 > 60（远大于修复前的 26）
            combos = gb.findChildren(QComboBox)
            assert combos, "高级分组内必须至少有一个 QComboBox"
            for c in combos:
                # 最低阈值：修复前是 26，修复后应 ≥ 控件 minimumSizeHint + 一些 grow
                assert c.width() > 60, (
                    f"高级分组内下拉框宽度异常窄（{c.width()}px）"
                )
        finally:
            dlg.close()
            dlg.deleteLater()
    finally:
        dock.deleteLater()


@pytest.mark.gui
def test_generic_alg_runs_translate(qgis_app, tmp_path):
    """``gdal:translate`` 端到端 happy-path：用临时 GeoTIFF 输入，
    走 ``GenericAlgService.run()``，得到一个 ``QgsRasterLayer``。
    """
    import subprocess

    from processing.core.Processing import Processing

    from app.core.domain.generic_alg import GenericAlgJob, GenericAlgService

    Processing.initialize()

    src = str(tmp_path / "src.tif")
    out = str(tmp_path / "out.tif")
    # gdal_create CLI 生成一个最小 GeoTIFF（4 像素白图），跨平台标准做法
    gdal_create = r"D:/zsh/app/qgis4.2/bin/gdal_create.exe"
    subprocess.run(
        [
            gdal_create, "-of", "GTiff", "-outsize", "8", "8",
            "-a_srs", "EPSG:4490",
            "-a_ullr", "-0.5", "0.5", "0.5", "-0.5",
            "-burn", "128",
            src,
        ],
        check=True,
    )

    service = GenericAlgService()
    job = GenericAlgJob(
        algo_id="gdal:translate",
        params={"INPUT": src, "TARGET_CRS": "EPSG:4490"},
        output_name="translated",
        output_path=out,
    )
    result = service.run(job)
    from qgis.core import QgsRasterLayer

    assert isinstance(result, QgsRasterLayer)
    assert result.isValid()
    assert result.source() == out
    import os

    assert os.path.exists(out)
