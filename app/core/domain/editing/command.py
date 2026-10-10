"""图层编辑命令的成对开关。

``QgsVectorLayer.beginEditCommand()`` 与 ``endEditCommand()`` **必须成对**：一旦有一次
异常从两者之间逃出去，命令就悬着不关，该图层的撤销栈从此 ``canUndo()`` 恒为假 ——
用户看到的是「改了东西但 Ctrl+Z 没反应」。PyQGIS **没有** ``cancelEditCommand``，
写了会 AttributeError 再抛一次，因此出错时唯一正确的收尾是
:meth:`QgsVectorLayer.destroyEditCommand`（丢弃本次改动并关掉命令）。

仓库里这段配对原本手抄了十几处（``feature_edit._commit`` / ``select_feature`` /
``delete_features`` / ``explode_parts`` / ``boundary_edit`` / ``dig_hole`` /
``split_polygon`` / ``split_by_selection`` / ``merge_geometry`` / ``copy_to_layer`` /
``move_features`` …），这个模块把正确写法收成一处，新代码直接用它，别再抄第十三份。

用法::

    with edit_command(layer, "删除节点") as cmd:
        if not layer.changeGeometry(fid, geom):
            cmd.abort()
    # 正常退出          → endEditCommand()
    # abort() 或抛异常  → destroyEditCommand()，异常照常向上抛

**块内只做改动本身**：``clear_spatial_index`` / 触发画布重绘这类收尾放在 ``with``
**之后** —— 放进块内一旦抛异常，会连同已经成功的改动一起被 destroy 掉。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer

__all__ = ["edit_command"]


class _EditCommand:
    """``edit_command`` 的句柄：只在需要丢弃本次改动时调 :meth:`abort`。"""

    def __init__(self) -> None:
        self._aborted = False

    def abort(self) -> None:
        """丢弃本次改动（对应 ``destroyEditCommand``）。块内改失败时调它。"""
        self._aborted = True


@contextmanager
def edit_command(layer: "QgsVectorLayer", text: str):
    """把一段几何 / 要素改动包成图层撤销栈上的**一个**命令。

    :param text: 撤销栈里显示的命令名（如「删除节点」）。

    .. note::
       ``beginEditCommand`` 会截断该图层的 redo 历史，所以**没有实际改动时不要开**
       （见 ``move_features.py`` / ``boundary_edit.py`` / ``merge_geometry.py``
       里的同一约定）。
    """
    layer.beginEditCommand(text)
    cmd = _EditCommand()
    try:
        yield cmd
    except BaseException:
        # 异常逃出去也必须关掉命令，否则图层撤销栈永久失效；关完照常上抛
        layer.destroyEditCommand()
        raise
    if cmd._aborted:
        layer.destroyEditCommand()
    else:
        layer.endEditCommand()
