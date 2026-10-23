"""构建 / 环境快照：``BUILD`` 日志行与诊断包里的 ``environment.json`` 同源。

排查的第一步永远是"用户跑的是哪一版"。打包后源码被编译成 ``.pyd``，产物里没有
``.py``，traceback 里的文件名:行号只有对**同一个 commit** 才有意义——所以 ``git``
必须存在，而且必须和日志里那行是同一份数据。分开写两处（日志一处、诊断包一处）早晚
会漂移：用户答"我装的是 3.40"而日志写着别的版本，排查就从这里开始偏。

用同一份快照还有个好处：诊断包里的 ``environment.json`` 直接就是启动时写下的那份
（见 :func:`app.bootstrap._write_environment_snapshot`），不必在导出时重新推一遍
——"现在的状态"和"出问题那一刻的状态"可能已经不是一回事了。

取不到的字段一律退化成 ``"?"``：快照本身绝不能把启动流程带崩。
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import Any

from app import __version__ as app_version
from app.build_meta import BUILD_INFO_FILE, read_build_info
from app.infra.fs.paths import Paths

from .config import LogConfig
from .session import current_session

#: 打包时可由构建脚本写在产物根的版本戳（``{"git": "abc1234"}``），有则优先用。
__all__ = ["BUILD_INFO_FILE", "as_json", "build_snapshot", "config_path", "qgis_version", "read_git_revision"]


def build_snapshot(
    cfg: Any,
    *,
    project_root: PathLike | str | None = None,
    log_dir: PathLike | str | None = None,
    session_id: str | None = None,
) -> dict[str, str]:
    """构建 / 环境快照：**顺序即日志里的字段顺序**。

    :param cfg: 鸭子类型的 :class:`~app.env.config.AppConfig`（只用到 ``app_name`` /
        ``env`` / ``crs``）；换成替身也能用，启动早期拿不到完整配置时不会因此失败。
    :param session_id: 缺省取 :func:`~app.infra.logging.session.current_session`。
    """
    session = current_session()
    directory = log_dir if log_dir is not None else LogConfig.from_env().log_dir
    build = read_build_info(project_root)
    git = build.get("git") or "?"
    if git == "?":
        git = read_git_revision(project_root)
    return {
        "app": _text(getattr(cfg, "app_name", None)),
        "version": app_version,
        "version_code": _text(build.get("versionCode")),
        "build_id": _text(build.get("buildId")),
        "runtime_id": _text(build.get("runtimeId")),
        "channel": _text(build.get("channel")),
        "git": git,
        "env": _text(getattr(cfg, "env", None)),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "frozen": str(bool(getattr(sys, "frozen", False))),
        "qgis": qgis_version(),
        "crs": _text(getattr(cfg, "crs", None)),
        "log_dir": str(directory),
        # 现场回传日志时先看这一项：它就是 logs/session/ 下面那对文件名的主体，
        # 而 session 目录里的文件才是"用户这一次"的完整时间线
        "session": session_id or (session.session_id if session else "?"),
    }


def as_json(snapshot: Mapping[str, str]) -> str:
    """快照 → 可直接落盘的 JSON 文本（加生成时间，中文不转义）。"""
    payload = dict(snapshot)
    payload["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def read_git_revision(project_root: PathLike | str | None) -> str:
    """当前提交的短哈希：优先打包时写入的版本戳，否则直接读 ``.git/HEAD``。

    直接读文件而不调 ``git rev-parse``：打包后的机器上没有 git，开发态也省一次
    子进程。``.git`` 在工作区（worktree）里是个指向真实 git 目录的文本文件，
    所以要先跟着它跳一次。
    """
    if project_root is None:
        return "?"
    root = Path(project_root)
    try:
        stamped = json.loads((root / BUILD_INFO_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stamped = {}
    revision = str(stamped.get("git") or "").strip() if isinstance(stamped, dict) else ""
    if revision:
        return revision

    try:
        git_dir = root / ".git"
        if git_dir.is_file():  # worktree：内容是 "gitdir: <真实 git 目录>"
            pointer = git_dir.read_text(encoding="utf-8").strip()
            git_dir = (root / pointer.split(":", 1)[1].strip()).resolve()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:8]  # detached HEAD：文件里直接是哈希
        ref_file = git_dir / head.split(":", 1)[1].strip()
        # 引用被打包进 packed-refs 时读不到单文件，返回 "?" 即可（不猜）
        if ref_file.is_file():
            return ref_file.read_text(encoding="utf-8").strip()[:8]
    except (OSError, IndexError):
        pass
    return "?"


def qgis_version() -> str:
    """QGIS 版本串；拿不到返回 ``"?"``（日志快照不该因为取版本而失败）。"""
    try:
        from qgis.core import Qgis
    except Exception:  # noqa: BLE001
        return "?"
    return str(getattr(Qgis, "QGIS_VERSION", "?"))


def _text(value: Any) -> str:
    """取到的字段值 → 非空文本；空 / 缺失退化成 ``"?"``。"""
    text = str(value).strip() if value is not None else ""
    return text or "?"


def portable_root() -> Path | None:
    """冻结态下的便携包根目录（可执行文件所在目录）；非冻结态返回 ``None``。

    判据与 :func:`app.bootstrap._ensure_portable_env` 一致，避免把普通安装目录
    当成便携包：

    - ``runtime/proj.db``：Windows 便携包（QGIS 运行时平铺在 ``runtime/``）
    - ``runtime/share/qgis``：新 Linux 便携包（``runtime/`` 是迷你 conda 前缀）
    - 产物根同时有 ``proj.db`` + ``share/qgis``：旧 Linux 平铺布局
    """
    if not getattr(sys, "frozen", False):
        return None
    bundle = Path(sys.executable).resolve().parent
    runtime = bundle / "runtime"
    if (runtime / "proj.db").is_file() or (runtime / "share" / "qgis").is_dir():
        return bundle
    if (bundle / "proj.db").is_file() and (bundle / "share" / "qgis").is_dir():
        return bundle
    return None


def config_path(explicit: PathLike | str | None = None) -> Path | None:
    """实际生效的 ``app.yaml``：显式给的 > ``QGIS_DEV_CONFIG`` > 仓库内默认位置。

    与 :func:`app.bootstrap._resolve_config_path` 同一套判据（那边是启动期，这边是
    导出诊断包时），拿不到存在的那份就返回 ``None``——调用方记一条"缺失"就够了。
    """
    if explicit is not None:
        path = Path(explicit)
    else:
        env = os.environ.get("QGIS_DEV_CONFIG")
        path = Path(env) if env else Paths.default().config_file("app.yaml")
    return path if path.is_file() else None
