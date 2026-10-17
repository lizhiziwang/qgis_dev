"""通用 processing 后台任务与输出解析。

工具树上的算法对话框统一走这里：:class:`QgsProcessingAlgRunnerTask` 在 worker
线程执行算法，主线程事件循环不阻塞——与 QGIS 自家 processing 对话框同一机制。

两条本仓库实测出来的规则写在这里，别再踩：

- ``feedback.progressChanged`` 只能接**普通 Python 方法**再重发
  :attr:`AlgRunnerTask.progress_updated` 信号；直连（或排队连接）到控件槽会
  原生崩溃 ``0xC0000409``，无 Python 异常可记日志（见 memory / ``BufferDialog``）。
- 后台任务里 ``memory:`` 输出在 ``results`` 里是**图层 id 字符串**（不是图层
  对象，也不是 ``memory:`` 源串，2026-10 实测）；必须在主线程用
  :meth:`QgsProcessingContext.takeResultLayer` 取回。文件输出才是路径字符串。
  :func:`resolve_vector_output` 把两种形态统一成 ``QgsVectorLayer``。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from qgis.core import QgsProcessingAlgRunnerTask
from qgis.PyQt.QtCore import pyqtSignal

if TYPE_CHECKING:
    from qgis.core import (
        QgsProcessingAlgorithm,
        QgsProcessingContext,
        QgsProcessingFeedback,
    )


class AlgRunnerTask(QgsProcessingAlgRunnerTask):
    """在后台线程跑一个 processing 算法的任务。

    相比裸 :class:`QgsProcessingAlgRunnerTask`，只多一个
    :attr:`progress_updated` 信号：把 ``feedback.progressChanged``（``float``）
    转成 ``int`` 百分比，绕开「信号直连控件槽 → 原生崩溃」的坑。

    :param keepalive: 需要活到任务结束的 Python 对象（通常是主线程清洗输入时
        创建的临时 memory 图层 / 参数 dict）。任务内部只持有裸指针，被 GC 会
        悬垂 → worker 线程原生崩溃。
    """

    #: 算法进度 0–100（worker 线程发出 → 队列投递到主线程普通方法）
    progress_updated = pyqtSignal(int)

    def __init__(
        self,
        algorithm: QgsProcessingAlgorithm,
        parameters: dict,
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
        keepalive: object = None,
    ) -> None:
        super().__init__(algorithm, parameters, context, feedback)
        self.keepalive = keepalive
        # 只接普通方法重发信号：直连控件槽本构建会原生崩溃（0xC0000409）
        feedback.progressChanged.connect(self._on_progress)

    def _on_progress(self, value: float) -> None:
        self.progress_updated.emit(int(value))


def resolve_vector_output(context: QgsProcessingContext, value, name: str):
    """把任务结果里的矢量输出解析成 ``QgsVectorLayer``（**主线程**调用）。

    支持三种形态：

    - 图层对象（部分 native 算法同步返回 / 某些构建直出对象）
    - 后台任务返回的**图层 id**（``memory:`` 输出）→ ``context.takeResultLayer``
    - 文件路径（用户指定输出文件，或算法落盘的临时文件）

    解析不出来直接抛 ``RuntimeError``——静默返回空图层只会让调用方在更远的
    地方报一个看不懂的错。
    """
    from qgis.core import QgsProcessingUtils, QgsVectorLayer

    if value is None:
        raise RuntimeError(f"{name}：算法没有返回输出")
    if hasattr(value, "isValid"):
        return value

    text = str(value)
    # memory: 输出：先按图层 id 从上下文临时图层仓库取回（所有权随 take 转移）
    layer = context.takeResultLayer(text)
    if layer is None and text.startswith("memory:"):
        # 兜底：少数算法直接回 ``memory:<名称>`` 源串
        layer = QgsProcessingUtils.mapLayerFromString(text, context)
    if layer is None and os.path.isfile(text):
        layer = QgsVectorLayer(text, name, "ogr")
    if layer is None:
        raise RuntimeError(f"{name}：无法解析算法输出 {text!r}")
    return layer


__all__ = ["AlgRunnerTask", "resolve_vector_output"]
