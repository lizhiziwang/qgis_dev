"""处理算法日志反馈：捕获算法运行输出并转成信号。

QGIS 3.40 的 :class:`QgsProcessingFeedback` 信号面很窄（只有
``progressChanged`` / ``canceled`` / ``processedCountChanged``），算法日志走
``push*`` **虚方法**回调（在工作线程调用）。本类覆写这些虚方法，把输出转成
:attr:`log_line` 信号（``level, message``）投递到主线程，并同步记录命令级失败
标记 :attr:`failed`。

影像切片（XYZ 目录 / MBTiles）两个算法共用；worker 线程发出的信号经 Qt 队列
投递到主线程普通 Python 方法再更新 UI，避免原生崩溃（见仓库线程规则）。
"""

from __future__ import annotations

from qgis.core import QgsProcessingFeedback
from qgis.PyQt.QtCore import pyqtSignal


class ProcessingLogFeedback(QgsProcessingFeedback):
    """捕获算法运行时输出的反馈对象。

    算法日志以 ``push*`` 虚方法回调（在工作线程调用）；本类把它们转成
    :attr:`log_line` 信号（``level, message``）投递到主线程，并同步记录命令
    失败标记 :attr:`failed`。
    """

    log_line = pyqtSignal(str, str)

    def __init__(self) -> None:
        super().__init__()
        #: 命令级失败标记。QgsGdalAlgorithmBase 在 gdal 命令非零退出时经
        #: ``reportError`` 报告 "Process returned error code N"，据此判断
        #: ``processing.run`` 不抛异常但实际失败的情况。
        self.failed = False

    def pushInfo(self, info: str) -> None:
        self.log_line.emit("INFO", str(info))

    def pushCommandInfo(self, info: str) -> None:
        self.log_line.emit("CMD", str(info))

    def pushConsoleInfo(self, info: str) -> None:
        self.log_line.emit("DEBUG", str(info))

    def pushWarning(self, info: str) -> None:
        self.log_line.emit("WARNING", str(info))

    def reportError(self, error: str, fatalError: bool = False) -> None:
        text = str(error)
        if "error code" in text.lower():
            self.failed = True
        self.log_line.emit("ERROR", text)


__all__ = ["ProcessingLogFeedback"]
