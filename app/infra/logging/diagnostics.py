"""诊断包：把"这一次运行"的证据打成一个 zip，用户传回来就够定位。

用户回传的**不该**是裸日志文件：``logs/`` 下东西多（跨运行的 ``app.log``、崩溃转储、
会话文件），现场同事不知道挑哪个；而且里面可能带凭据（``config/app.yaml`` 里是**真实**
的天地图 key）。所以这里只做一件事——**打成一个包，顺手打码**。

包里放什么，都是为回答"他当时在做什么"：

- ``logs/session/<id>.log`` / ``<id>.jsonl``：本次运行的完整时间线（纯文本给人读，
  JSONL 给 ``jq`` 精确切段）。轮转出来的 ``.1`` 备份**一并收**——出事那段可能已经滚进
  备份里了（见 :meth:`~app.infra.logging.session.SessionLog.files`）。
- ``logs/app.log`` / ``error.log``：当前那份（跨运行累积，只作旁证）。
- ``logs/crash.log`` (+ ``.1`` …) 与 ``crash_faulthandler.log``：未捕获异常与原生崩溃。
- ``logs/environment.json``：构建 / 环境快照，启动时写下（见
  :mod:`app.infra.logging.environment`）。
- ``project.json``：调用方提供的当前工程快照（见
  :func:`app.ui.common.project_report.snapshot_text`；本模块不碰 QGIS）。
- ``config/app.yaml``：**打码后**的配置——路径、级别、底图开关都可能是问题本身。
- ``README.txt``：包里有什么、每份文件怎么看、哪些被截断 / 缺失。

单文件只保留**尾部** ``max_bytes``：故障刚发生，要的是最后那几行；把 10 MB 的
``app.log`` 原样塞进去，用户那边上传就是几分钟，而且大头是无关的历史。
"""

from __future__ import annotations

import logging
import os
import time
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from .config import LogConfig
from .environment import config_path as resolve_config_path
from .redaction import REDACTED, redact, redact_json, redact_yaml, secret_values
from .session import SessionLog, current_session

logger = logging.getLogger(__name__)

#: 诊断包文件名前缀（``diagnostics-<会话 id>-<时间戳>.zip``）。保持 ASCII：
#: 这份文件要经过聊天软件 / 邮件 / 网盘，中文名在某个环节被转义成乱码是常事。
PACKAGE_PREFIX = "diagnostics"

#: 单个文件保留的尾部字节数。会话文件本身有 5 MB × 2 的上限，这里再收一道，
#: 免得整包大到用户没法发。
DEFAULT_MAX_BYTES = 2 * 1024 * 1024

#: ``crash.log`` / ``crash_faulthandler.log`` 各收最近几份（含当前那份）
_MAX_CRASH_FILES = 4

_README_NAME = "README.txt"
_CONFIG_ARCNAME = "config/app.yaml"


@dataclass(frozen=True)
class DiagnosticsPackage:
    """一次导出的结果：给界面显示、给日志留痕。"""

    #: 落盘的 zip 路径
    path: Path
    #: 包内文件名（相对路径），已排序
    entries: tuple[str, ...]
    #: 只保留了尾部的文件（包内路径）
    truncated: tuple[str, ...] = ()
    #: 想收但没找到的（包内路径），写进 README，免得看的人以为"就是没有"
    missing: tuple[str, ...] = ()
    #: 本次会话 id（找不到会话时为 ``None``）
    session_id: str | None = None
    #: 包内被打码的凭据处数，用来向用户交底"脱敏确实跑过了"
    redacted: int = 0

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def build_package(
    destination: PathLike | str,
    *,
    log_dir: PathLike | str | None = None,
    config_path: PathLike | str | None = None,
    extra: Mapping[str, str] | None = None,
    session: SessionLog | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    now: float | None = None,
) -> DiagnosticsPackage:
    """把本次运行的证据打成 zip，写到 ``destination`` 目录下。

    :param destination: 目标**目录**（文件名由本函数生成，带会话 id 与时间戳）。
    :param log_dir: 日志目录；缺省用 :meth:`LogConfig.from_env`。
    :param config_path: ``app.yaml`` 路径；缺省按
        :func:`~app.infra.logging.environment.config_path` 解析。
    :param extra: 额外文件，``{包内路径: 文本}``（如 ``project.json``）。文本同样过
        打码——图层源里就可能有 ``&tk=…``。
    :param session: 会话日志；缺省取当前会话（导出时传进来，便于测试）。
    :param max_bytes: 单文件保留的尾部字节数。
    :param now: 时间戳（写进文件名与 README）；缺省取当前时间（测试用）。
    :raises OSError: 目标目录不可写。
    """
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    directory = Path(log_dir) if log_dir is not None else LogConfig.from_env().log_dir
    current = session if session is not None else current_session()

    truncated: list[str] = []
    missing: list[str] = []
    files: list[tuple[str, str]] = []  # (包内路径, 已打码文本)
    tally = _Tally()

    # 0) 配置（**必须打码**：map.keys 里是真实天地图 key）。先读它：里面的凭据
    #    值还要拿去清日志里**裸出现**的真值（不带 ``tk=`` 前缀的那种），见
    #    :mod:`app.infra.logging.redaction` 模块说明。
    resolved_config = resolve_config_path(config_path)
    config_secrets: set[str] = set()
    if resolved_config is not None:
        text, cut = _read_tail(resolved_config, max_bytes)
        if cut:
            truncated.append(_CONFIG_ARCNAME)
        config_secrets = secret_values(text)
        cleaned = redact_yaml(text)
        tally.note(text, cleaned)
        files.append((_CONFIG_ARCNAME, cleaned))
    else:
        missing.append(_CONFIG_ARCNAME)

    # 1) 会话日志（本次运行的完整时间线，含轮转备份）
    if current is not None:
        for path in current.files():
            arcname = f"logs/session/{path.name}"
            files.append(_text_entry(path, arcname, max_bytes, truncated, tally, config_secrets))
    else:
        missing.append("logs/session/<本次会话>.log")

    # 2) 跨运行的全量与错误日志（只作旁证，取当前那份）
    for name in ("app.log", "error.log"):
        path = directory / name
        if path.is_file():
            files.append(
                _text_entry(path, f"logs/{name}", max_bytes, truncated, tally, config_secrets)
            )
        else:
            missing.append(f"logs/{name}")

    # 3) 崩溃记录：Python 未捕获异常 + faulthandler 原生崩溃转储
    for name in ("crash.log", "crash_faulthandler.log"):
        found = sorted(p for p in directory.glob(f"{name}*") if p.is_file())
        if not found:
            missing.append(f"logs/{name}")
        for path in found[:_MAX_CRASH_FILES]:
            files.append(
                _text_entry(
                    path, f"logs/{path.name}", max_bytes, truncated, tally, config_secrets
                )
            )

    # 4) 启动时写下的构建 / 环境快照
    env_file = directory / "environment.json"
    if env_file.is_file():
        files.append(
            _text_entry(env_file, "environment.json", max_bytes, truncated, tally, config_secrets)
        )
    else:
        missing.append("environment.json")

    # 5) 调用方给的现场快照（工程 / 图层树），同样过打码
    for arcname, text in (extra or {}).items():
        cleaned = _redact_for(arcname, text, config_secrets)
        tally.note(text, cleaned)
        files.append((arcname, cleaned))

    readme = _readme(
        current=current,
        files=[name for name, _ in files],
        truncated=truncated,
        missing=missing,
        max_bytes=max_bytes,
    )
    package_path = target / _package_name(current, _stamp(now))

    # 先写临时文件再改名：导出中断（磁盘满 / 进程被杀）不会留下一个"看起来能用、
    # 其实少了几个文件"的包——那种包发给技术支持，浪费的是双方的时间。
    tmp_path = package_path.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(_README_NAME, readme)
            for arcname, text in files:
                archive.writestr(arcname, text)
        os.replace(tmp_path, package_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    package = DiagnosticsPackage(
        path=package_path,
        entries=tuple(sorted([_README_NAME, *(name for name, _ in files)])),
        truncated=tuple(truncated),
        missing=tuple(missing),
        session_id=current.session_id if current is not None else None,
        redacted=tally.redactions,
    )
    logger.info(
        "诊断包已导出 %s（%d 个文件，%.1f KB，截断 %d，缺失 %d，脱敏 %d 处）",
        package_path,
        len(package.entries),
        package.size_bytes / 1024,
        len(package.truncated),
        len(package.missing),
        package.redacted,
    )
    return package


# --------------------------------------------------------------------- 内部


@dataclass
class _Tally:
    """打码计数：``***`` 增了多少个（只用于向用户交底，不参与任何判定）。"""

    redactions: int = 0

    def note(self, before: str, after: str) -> None:
        self.redactions += max(0, after.count(REDACTED) - before.count(REDACTED))


def _package_name(session: SessionLog | None, stamp: str) -> str:
    """``diagnostics-<会话 id>-<时间戳>.zip``（没有会话就省掉中间那段）。"""
    middle = f"{session.session_id}-" if session is not None else ""
    return f"{PACKAGE_PREFIX}-{middle}{stamp}.zip"


def _stamp(now: float | None) -> str:
    """文件名用的时间戳（本地时间，``20261017-153012``）。"""
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))


def _text_entry(
    path: Path,
    arcname: str,
    max_bytes: int,
    truncated: list[str],
    tally: _Tally,
    secrets: Iterable[str] | None = None,
) -> tuple[str, str]:
    """磁盘文件 → (包内路径, 已打码文本)，顺带登记截断与打码处数。"""
    text, cut = _read_tail(path, max_bytes)
    if cut:
        truncated.append(arcname)
    cleaned = _redact_for(arcname, text, secrets)
    tally.note(text, cleaned)
    return arcname, cleaned


def _redact_for(arcname: str, text: str, secrets: Iterable[str] | None = None) -> str:
    """按文件类型挑打码方式：配置文件走"结构化 + 兜底"，其余走 ``k=v`` + 字面替换。"""
    lowered = arcname.lower()
    if lowered.endswith((".yaml", ".yml")):
        return redact_yaml(text, secrets)
    if lowered.endswith(".json"):
        return redact_json(text, secrets)
    return redact(text, secrets)


def _read_tail(path: Path, max_bytes: int) -> tuple[str, bool]:
    """读文本并只保留**尾部** ``max_bytes``，返回 (文本, 是否截断)。

    先按字节截再解码：日志文件可能是 10 MB，不截就整份读进内存（打包机上同时开着
    几个实例时这是实打实的开销）。截到半个多字节字符 / 半行是正常的，所以解码后
    再按第一个换行切齐，并在开头补一行说明。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return f"（读取失败：{path.name}）", False

    cut = len(raw) > max_bytes
    if cut:
        raw = raw[-max_bytes:]
    text = raw.decode("utf-8", errors="replace")
    if cut:
        _head, sep, rest = text.partition("\n")
        if sep:
            text = rest
        text = f"……（本文件过长，仅保留最后 {max_bytes // 1024} KB，更早的记录已省略）\n{text}"
    return text, cut


def _readme(
    *,
    current: SessionLog | None,
    files: list[str],
    truncated: list[str],
    missing: list[str],
    max_bytes: int,
) -> str:
    """包内说明：让现场同事（和我）不必猜这包里是什么。"""
    session_line = (
        f"本次会话 {current.session_id}：logs/session/{current.session_id}.log（给人读）"
        f" / .jsonl（给工具读）"
        if current is not None
        else "**没找到会话日志**：启动时的会话装配可能失败了，先看 logs/app.log 开头"
    )
    lines = [
        "诊断包",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "一、先看这三样",
        f"  - {session_line}",
        "  - environment.json  版本 / 环境（先确认是不是同一版，再谈别的）",
        "  - project.json      出问题时打开的工程与图层清单",
        "",
        "二、出过崩溃再加这两样",
        "  - logs/crash.log             未捕获异常（含 Qt 槽里抛出的）",
        "  - logs/crash_faulthandler.log 原生崩溃时的全部线程栈",
        "",
        "三、旁证",
        "  - logs/app.log / error.log   跨运行累积，只留了尾部",
        "  - config/app.yaml            当前配置（凭据已打成 ***）",
        "",
        "四、本次导出说明",
        f"  - 会话：{current.session_id if current is not None else '未装配'}",
        f"  - 单文件上限：{max_bytes // 1024} KB（超出只留尾部）",
        "  - 被截断的文件：" + ("、".join(truncated) if truncated else "无"),
        "  - 没找到的文件：" + ("、".join(missing) if missing else "无"),
        f"  - 凭据（tk / key / token / password 等）在本包内已统一打成 {REDACTED}",
        "",
        "五、怎么用它定位一次失败",
        "  1) 在会话纯文本里找“OP op3 失败”，看清用户点的是什么操作；",
        "  2) 用同一个编号把整段捞出来：jq 'select(.op==\"op3\")' <会话 id>.jsonl；",
        "  3) 堆栈对源码：environment.json 里的 git 就是需要的那个 commit。",
        "",
        "包内文件清单：",
    ]
    lines += [f"  - {name}" for name in sorted(files)]
    return "\n".join(lines) + "\n"


__all__ = ["DEFAULT_MAX_BYTES", "PACKAGE_PREFIX", "DiagnosticsPackage", "build_package"]
