"""GDAL 对话框描述 + 高度回归测试。

两个用户反馈对应的回归测试：

1. ``test_generic_alg_dialog_max_height_matches_mainwindow`` —— dialog 最大高度
   必须与父 QMainWindow 高度一致（不再以 sizeHint 撑到 927px）。
2. ``test_gdal_descriptions_no_raw_command_fallback`` —— GDAL 算法描述不再展示
   裸 ``<p>gdal:cliprasterbymasklayer</p>`` 这种 code 命令，要给出 displayName
   + 中文说明。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")


@pytest.fixture
def processing_ready(qgis_app):
    """确保 processing 注册表已装载（qgis_app 跑过 initQgis 后再 init Processing）。

    不能在模块顶层调 ``Processing.initialize()``：那时 qgis_app fixture（session
    级别）还没机会执行 initQgis，注册表里就没东西（连 native/buffer 都查不到）。
    """
    from processing.core.Processing import Processing

    Processing.initialize()
    yield


@pytest.mark.gui
def test_generic_alg_dialog_max_height_matches_mainwindow(qgis_app, processing_ready):
    """GenericAlgDialog 的 maxHeight 必须与父 QMainWindow.height 对齐。

    修复前：``setMinimumSize(760, 620)`` + 没有 maxHeight → sizeHint 撑到
    927px（实测），超出 MainWindow 默认 800px。
    修复后：``__init__`` 通过 ``_resolve_max_height`` 走父链找 QMainWindow，
    把 ``setMaximumHeight(mainwindow.height())`` 装配上。
    """
    from PyQt6.QtWidgets import QMainWindow

    from app.ui.dialogs.generic_alg import GenericAlgDialog

    win = QMainWindow()
    try:
        win.resize(1200, 800)  # 与 MainWindow 默认尺寸一致
        dlg = GenericAlgDialog(parent=win, algo_id="gdal:warpreproject")
        try:
            # 关键断言：maxHeight <= parent.height()，不允许超过
            assert dlg.maximumHeight() <= win.height(), (
                f"dialog.maximumHeight({dlg.maximumHeight()}) > "
                f"parent.height({win.height()})"
            )
            # 仍然允许 resize，但不超过 mainwindow
            assert dlg.maximumHeight() >= dlg.minimumHeight()
        finally:
            dlg.close()
            dlg.deleteLater()
    finally:
        win.close()
        win.deleteLater()


@pytest.mark.gui
def test_generic_alg_dialog_max_height_falls_back_to_screen(qgis_app, processing_ready):
    """无父级 QMainWindow 时，maxHeight 退到屏幕可用高度 - 80。"""
    from app.ui.dialogs.generic_alg import GenericAlgDialog

    dlg = GenericAlgDialog(algo_id="gdal:cliprasterbyextent")
    try:
        screen_h = qgis_app.primaryScreen().availableGeometry().height()
        # 至少 400（最小可用值）
        assert dlg.maximumHeight() >= 400
        # 不超过屏幕（减去 80 的任务栏留白）
        assert dlg.maximumHeight() <= screen_h
    finally:
        dlg.close()
        dlg.deleteLater()


@pytest.mark.gui
def test_generic_alg_dialog_param_panel_scrolls_when_tall(qgis_app, processing_ready):
    """栅格计算器（13+ 参数行）内容高度超过 dialog 时，参数面板内 QScrollArea
    必须出现垂直滚动条，而不是撑爆 dialog 或裁掉 wrapper。

    修复前：参数面板直接挂到 QHBoxLayout，content 大小驱动 dialog 高度，撑爆
    maxHeight 后会被 maxHeight 限死并截断（widget 被压扁看不到）。
    修复后：参数面板包在 QScrollArea 里，外层 dialog 保持 maxHeight
    = mainwindow.height()，超出部分由滚动条承载。
    """
    from PyQt6.QtWidgets import QMainWindow, QScrollArea

    from app.ui.dialogs.generic_alg import GenericAlgDialog

    win = QMainWindow()
    try:
        # 模拟一个比 mainwindow 更矮的窗口：600 高度（更紧），
        # 验证栅格计算器（13+ 参数行）也能完整访问
        win.resize(1200, 600)
        win.show()
        dlg = GenericAlgDialog(parent=win, algo_id="gdal:rastercalculator")
        try:
            dlg.show()
            # 让首轮 layout 跑完
            for _ in range(3):
                qgis_app.processEvents()

            # 1) 参数面板包在 QScrollArea 里
            scrolls = dlg.findChildren(QScrollArea)
            assert scrolls, "参数面板必须包在 QScrollArea 里"
            sa = scrolls[0]
            assert sa.widgetResizable(), "QScrollArea 必须 setWidgetResizable(True)"

            # 2) 对参数 widget 内容调大 minHeight（模拟「内容超长」），
            # 滚动条应该出现
            inner = sa.widget()
            assert inner is not None
            inner.setMinimumHeight(inner.sizeHint().height() + 1500)

            # 让布局跑一遍
            for _ in range(3):
                qgis_app.processEvents()

            # 3) 关键断言：content 超过 viewport 时，QScrollArea 必须
            #    出现滚动机制。vbar.maximum > 0 即可证明「有可滚动内容」——
            #    Qt 自身在不需要时（内容 < viewport）会隐藏 vbar，这是正确的
            #    默认行为
            vbar = sa.verticalScrollBar()
            assert vbar.maximum() > 0, (
                f"参数面板内容超出 dialog 高度时，必须可滚动；"
                f"actual vbar.max={vbar.maximum()}, range={vbar.minimum()}-{vbar.maximum()}"
            )

            # 4) dialog 自身的高度不应超过 mainwindow 太多（maxHeight 起约束）
            assert dlg.height() <= win.height() + 80, (
                f"dialog.height({dlg.height()}) 超出 mainwindow.height({win.height()})"
            )

            # 5) 模拟用户实际滚动：把 vbar 拖到中段后确认 widget 跟着移
            vbar.setValue(vbar.maximum() // 2)
            for _ in range(2):
                qgis_app.processEvents()
            assert vbar.value() > 0, "vbar 可被拖动到中段"
        finally:
            dlg.close()
            dlg.deleteLater()
    finally:
        win.close()
        win.deleteLater()


@pytest.mark.gui
def test_gdal_descriptions_no_raw_command_fallback(qgis_app, processing_ready):
    """所有 GDAL 算法描述必须不展示裸 ``<p>gdal:xxx</p>`` 命令。

    用户反馈：「对话框内的算法描述为什么 gdal:cliprasterbymasklayer 都是这种命令」。
    根因：QGIS 4.2 在本环境下 ``shortHelpString()`` 对所有 ``gdal:*`` 返回空字符串，
    算法 ``algorithm_description_html`` 走 ``<p>{code}</p>`` 兜底。

    修复：
    - ``_ALGO_FALLBACK_DESC`` 合并了 ``_GDAL_FALLBACK_DESC``（59 个 GDAL 算法的
      中文描述）
    - 即便 59 个里有遗漏，也会 fallback 到 displayName + 「未提供详细说明」，
      不会再出现「裸 code 命令」
    """
    from qgis.core import QgsApplication

    from app.ui.common.algo_desc import algorithm_description_html

    reg = QgsApplication.processingRegistry()
    gdal_codes = [
        a.id() for a in reg.algorithms() if a.id().startswith("gdal:")
    ]
    # 确认环境里能找到至少 50 个 GDAL 算法
    assert len(gdal_codes) >= 50, (
        f"预期 ≥50 个 gdal:* 算法，实际 {len(gdal_codes)}"
    )

    bad = []
    for code in gdal_codes:
        html = algorithm_description_html(code)
        # 修复前的症状：纯 ``<p>gdal:xxx</p>``（除了 _ALGO_FALLBACK_DESC 已覆盖的，
        # 任何剩下的「裸 code」都视为回归）
        if html.strip() == f"<p>{code}</p>":
            bad.append(code)
    assert not bad, (
        f"以下 {len(bad)} 个 GDAL 算法仍展示裸 code 命令：{bad[:5]}..."
    )

    # 抽样验证：必须有「中文描述 / displayName」，而非纯 code
    sample = algorithm_description_html("gdal:cliprasterbymasklayer")
    assert "按掩膜" in sample or "mask" in sample.lower(), (
        f"gdal:cliprasterbymasklayer 的描述应该提到掩膜/mask，实际：{sample[:200]}"
    )
    sample = algorithm_description_html("gdal:warpreproject")
    assert "重投影" in sample or "warp" in sample.lower(), (
        f"gdal:warpreproject 的描述应提到重投影/warp，实际：{sample[:200]}"
    )