"""删除要素：删掉当前编辑图层上**选中**的要素。

工具条「删除」按钮背后的动作——与画布 Delete 键（:mod:`app.ui.tools.select_feature`
里的 ``_delete_selected_editable``）不同，这里只删**当前编辑图层**上的选中要素，
与工具条其它命令的作用域保持一致。

改动包进一个 ``beginEditCommand("删除要素")``，Ctrl+Z 一次撤销。
"""

from __future__ import annotations

from collections.abc import Callable

from qgis.core import QgsVectorLayer

from app.core.domain.check.space_index import clear_spatial_index
from app.infra.logging import get_logger

logger = get_logger(__name__)


def delete_selected_features(
    layer: QgsVectorLayer | None,
    hint: Callable[[str], None] | None = None,
) -> int | None:
    """删除 ``layer`` 上当前选中的要素。

    :return: 删掉的要素数；未编辑 / 图层不可用返回 ``None``；没有选中返回 ``0``
        （调用方据此区分「失败」与「无事可做」）。
    """
    say = hint or (lambda text: None)
    if layer is None or layer.type() != layer.VectorLayer:
        return None
    if not layer.isEditable():
        say("图层未处于编辑态，无法删除要素")
        return None

    fids = list(layer.selectedFeatureIds())
    if not fids:
        say("请先用点选 / 框选选中要删除的要素")
        return 0

    layer.beginEditCommand("删除要素")
    try:
        deleted = layer.deleteFeatures(fids)
    except RuntimeError:
        layer.destroyEditCommand()
        logger.exception("删除要素失败: layer=%s", layer.name())
        say("删除要素失败：图层编辑 API 异常")
        return None
    if not deleted:
        layer.destroyEditCommand()
        logger.warning("删除要素未生效: layer=%s fids=%s", layer.name(), fids)
        say("删除要素失败（详见日志）")
        return None
    layer.endEditCommand()

    # 要素已删，空间索引缓存失效 → 清掉让下次命中重建
    clear_spatial_index(layer)
    return len(fids)


__all__ = ["delete_selected_features"]
