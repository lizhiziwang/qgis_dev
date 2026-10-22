"""无模态对话框的统一打开方式：单实例复用 + 安全回调。

多个入口会重复打开同一个工具窗（连续点两次按钮、右键同一个图层两次…）。
工具窗改成无模态后，必须解决两件事：

1. **别开第二份**：同一个 ``parent + key`` 已经有一个窗口，就直接把它前置；
2. **别让回调异常杀掉进程**：PyQt 槽里未捕获异常在本环境是原生崩溃
   （exit 127，无 traceback），所以 ``accepted`` / ``finished`` 回调统一包一层。

用法::

    show_modeless(
        self,
        ("export_layer", layer.id()),
        lambda: ExportLayerDialog(parent=self, layer=layer),
        on_accepted=lambda dlg: self._do_export(dlg),
    )

窗口默认 ``WA_DeleteOnClose``：关闭即销毁，key 自动清掉；下次点击重新创建。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDialog, QWidget

from app.infra.logging import get_logger

logger = get_logger(__name__)


def safe_callback(callback: Callable[..., Any], *args: Any) -> None:
    """执行回调；异常只记日志，绝不冒泡进 Qt 槽。"""
    try:
        callback(*args)
    except Exception:  # noqa: BLE001 —— 槽内异常 = 原生崩溃，必须兜住
        logger.exception("无模态对话框回调失败")


def show_modeless(
    parent: QWidget | None,
    key: object,
    factory: Callable[[], QDialog],
    *,
    on_accepted: Callable[[QDialog], Any] | None = None,
    on_finished: Callable[[QDialog], Any] | None = None,
    configure: Callable[[QDialog], Any] | None = None,
    layer: object | None = None,
    delete_on_close: bool = True,
) -> QDialog:
    """打开（或前置）一个无模态对话框。

    Args:
        parent: 宿主窗口；窗口的引用**存放在 parent 上**，防止被 GC。
        key: 同一 parent 下的单实例键（如 ``("export_layer", layer.id())``）。
        factory: 真正创建窗口的可调用对象；只在没有现存窗口时调用。
        on_accepted: 点「确定 / 应用并关闭」后的回调（传入窗口对象）。
        on_finished: 任何关闭（确定 / 取消 / ×）后的回调；适合刷新按钮态。
        configure: 窗口创建后、显示前的配置（连接自定义信号、切 tab 等）。
        layer: 窗口依赖的图层；图层被移出工程时自动关闭窗口，避免悬空引用。
        delete_on_close: 关闭时销毁窗口（默认是；下次再点会重新创建）。

    Returns:
        当前显示（或刚创建）的窗口。
    """
    store = getattr(parent, "_modeless_dialogs", None) if parent is not None else None
    if parent is not None and store is None:
        store = {}
        setattr(parent, "_modeless_dialogs", store)

    if store is not None:
        existing = store.get(key)
        if existing is not None and not sip.isdeleted(existing):
            try:
                if existing.isMinimized():
                    existing.showNormal()
                else:
                    existing.show()
                existing.raise_()
                existing.activateWindow()
                return existing
            except RuntimeError:
                store.pop(key, None)

    dlg = factory()
    dlg.setModal(False)
    dlg.setWindowModality(Qt.WindowModality.NonModal)
    if delete_on_close:
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    if on_accepted is not None:
        dlg.accepted.connect(lambda *_: safe_callback(on_accepted, dlg))
    if on_finished is not None:
        dlg.finished.connect(lambda *_: safe_callback(on_finished, dlg))
    if configure is not None:
        safe_callback(configure, dlg)
    if layer is not None:
        from qgis.core import QgsProject

        project = QgsProject.instance()

        def _close_on_layer_removed(layer_ids) -> None:
            try:
                if layer.id() in layer_ids:
                    dlg.close()
            except Exception:  # noqa: BLE001 —— 图层/窗口已销毁
                try:
                    dlg.close()
                except Exception:  # noqa: BLE001
                    pass

        project.layersWillBeRemoved.connect(_close_on_layer_removed)

        def _drop_layer_guard(*_args: Any) -> None:
            try:
                project.layersWillBeRemoved.disconnect(_close_on_layer_removed)
            except Exception:  # noqa: BLE001 —— 工程/窗口销毁顺序不定
                pass

        dlg.destroyed.connect(_drop_layer_guard)
    if store is not None:
        store[key] = dlg

        def _forget(*_args: Any) -> None:
            if store.get(key) is dlg:
                store.pop(key, None)

        dlg.destroyed.connect(_forget)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


__all__ = ["safe_callback", "show_modeless"]
