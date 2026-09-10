"""启动时的 gpkg 别名清扫。

清理上次异常退出遗留的 gpkg 别名残留，失败时按退避重试。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from app.core.util.local_db_util import (
    release_gpkg_alias,
    sweep_startup_gpkg_aliases,
)
from app.infra.logging import get_logger

logger = get_logger(__name__)


class GpkgAliasMixin:
    """GpkgAliasMixin：见模块 docstring。"""

    def _sweep_startup_gpkg_aliases(self) -> None:
        """启动清扫(延迟到 UI 就绪后跑一次): 删上一会话残留的 .gpkg 别名。

        仅老 ``.db`` 兼容路径会产生别名; 新规 ``.gpkg`` 本体库在 `_is_our_alias`
        闸门(需同 stem ``.db`` 为 gpkg 内容)下不会被误删。建别名的数据目录
        在上一会话已记进 QGIS settings(见 local_db_util
        ``sweep_startup_gpkg_aliases``)——退出/崩溃后目录里不留别名文件,
        下次加载某老库时由 ``gpkg_open_path`` 自动重建。正在被工程引用的
        别名(若启动即恢复工程)不会误删。
        """
        try:

            n = sweep_startup_gpkg_aliases()
            if n:
                logger.info("启动清扫: 已清理 %d 个残留 .gpkg 别名", n)
        except Exception:  # noqa: BLE001
            logger.debug("启动清扫失败", exc_info=True)

    def _release_gpkg_alias_retry(self, alias: str, attempt: int) -> None:
        """尽力删除已无活图层引用的 .gpkg 别名(分段重试, 最长约 3s)。

        QGIS 移除图层后异步析构、释放 GDAL 句柄需要几个事件循环拍; 期间
        别名可能仍被占用(WinError 32)。"删除"是尽力而为——仍失败就留给
        下一启动的清扫兜底。"""
        from qgis.PyQt.QtCore import QTimer

        try:

            if release_gpkg_alias(alias):
                return
        except Exception:  # noqa: BLE001
            logger.debug("释放别名异常 %s", alias, exc_info=True)
            return
        if attempt >= 6:
            return
        QTimer.singleShot(500, lambda: self._release_gpkg_alias_retry(alias, attempt + 1))
