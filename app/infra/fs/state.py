"""应用级状态的落盘（**不跟 QGIS 工程走**）。

「数据管理里登记了哪些库」这类配置属于**系统**、不属于某个工程：用户绑定好数据库之后，
换个工程打开、甚至当前工程压根没保存过，下次也应当还是关掉前的样子。所以它落在
**配置文件目录**下的独立状态文件里：

    <config>/state/<名字>.json

``<config>`` 由 :class:`~app.infra.fs.paths.Paths` 解析（``Paths.from_env()``），
可用 ``QGIS_DEV_PATHS_CONFIG`` 重定向 —— 自检脚本就是这么把它指到临时目录的，
不会污染用户真实配置。

**为什么用 JSON 而不是 yaml**：这份是程序写、程序读的，不需要人手改
（``config/app.yaml`` 那份才是给人读改的）。**为什么不用 QSettings**：注册表是全局副作用、
测试没法隔离（见项目里那条「系统级配置放 yaml/文件，别放 QSettings」的经验）。

写盘一律「先写临时文件再 ``os.replace``」：直接覆盖写到一半崩溃会留下半截 JSON，
下次启动读不出来就等于把用户的配置弄丢了。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from app.infra.fs.paths import Paths
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: 状态文件放配置目录下的这个子目录（与给人改的 app.yaml 分开）
_STATE_DIR = "state"

#: 状态名只允许这些字符（防着拼接出目录穿越的路径）
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def state_dir() -> Path:
    """状态文件所在目录（不自动创建；:func:`save_state` 会按需建）。"""
    return Paths.from_env().config / _STATE_DIR


def state_path(name: str) -> Path:
    """某个状态的落盘路径。

    Raises:
        ValueError: ``name`` 里有不安全的字符。
    """
    if not _SAFE_NAME.match(str(name)):
        raise ValueError(f"状态名只能由字母 / 数字 / 下划线 / 减号组成：{name!r}")
    return state_dir() / f"{name}.json"


def load_state(name: str) -> dict:
    """读一份状态；没有 / 读坏了一律返回空字典（**不让状态文件拦住启动**）。"""
    path = state_path(name)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读状态文件 %s 失败（按空处理）：%s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("状态文件 %s 里不是字典（按空处理）", path)
        return {}
    return data


def save_state(name: str, data: dict) -> bool:
    """把一份状态写盘；返回是否成功（失败只记日志，不抛）。"""
    path = state_path(name)
    temp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)  # 原子替换：要么是旧的完整文件，要么是新的完整文件
    except OSError as exc:
        logger.warning("写状态文件 %s 失败：%s", path, exc)
        try:
            temp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover —— 清理失败无所谓
            pass
        return False
    logger.info("已保存状态 %s → %s", name, path)
    return True


__all__ = ["load_state", "save_state", "state_dir", "state_path"]
