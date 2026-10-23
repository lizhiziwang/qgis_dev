"""qgis_dev —— PyQGIS 桌面端 + FastAPI 服务工程化版本。

新代码全部从 ``app.*`` 导入；旧代码归档在 ``legacy/``。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 版本单一事实源
#
# 版本号只写在 ``config/app.yaml`` 的 ``app.version`` 里（major.minor.patch），
# 导入期在这里读出来，``VERSION_CODE`` 由它派生（major*10000 + minor*100 +
# patch）。打包脚本 / 更新清单 / 安装包都读同一份 yaml（见 tools/version.py），
# 不要在代码或脚本里再写死版本号。
#
# 一处例外：Windows 安装器升级时按设计保留用户 ``config/app.yaml``
# （onlyifdoesntexist），旧版本号会留在文件里；更老的配置里甚至还没有
# ``version`` 这一行。安装包根目录的 ``build_info.json`` 是本次构建刚写进去的、
# 描述磁盘上这份代码，与配置不一致（或配置里没有）时以它为准，并把版本号回写进
# yaml（没有就插入一行），下次启动就一致了。升级失败回滚后同理改回旧版本号。
#
# 这里刻意只用标准库（不引入 PyYAML）：本模块在 ``scripts/entry.py --version``
# 里被最早导入，解析失败必须给出明确路径而不是 ImportError。
# ---------------------------------------------------------------------------

_CONFIG_ENV = "QGIS_DEV_CONFIG"
_BUILD_INFO_ENV = "QGIS_DEV_BUILD_INFO"
_CONFIG_RELATIVE = Path("config") / "app.yaml"
_BUILD_INFO_FILE = "build_info.json"

#: ``app:`` 段的缩进行（遇到顶格的非注释行即结束）。
_APP_BLOCK_RE = re.compile(r"(?ms)^app:[ \t]*\r?\n((?:[ \t]+[^\r\n]*(?:\r?\n|$))*)")
#: 段内的 ``version: "1.0.1"``（引号可有可无，行尾注释会被忽略）。
_APP_VERSION_RE = re.compile(r"(?m)^[ \t]+version:[ \t]*[\"']?([^\"'\s#]+)")
#: 整行（写回时只换版本号，保留缩进与行尾）。
_VERSION_LINE_RE = re.compile(r"(?m)^[ \t]+version:[^\r\n]*")
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _config_candidates() -> list[Path]:
    """``config/app.yaml`` 的候选路径（按优先级）。"""
    candidates: list[Path] = []
    env = os.environ.get(_CONFIG_ENV)
    if env:
        candidates.append(Path(env))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / _CONFIG_RELATIVE)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / _CONFIG_RELATIVE)
    candidates.append(Path(__file__).resolve().parents[1] / _CONFIG_RELATIVE)
    candidates.append(Path.cwd() / _CONFIG_RELATIVE)
    return candidates


def _locate_config() -> Path:
    """按优先级找第一份存在的 ``config/app.yaml``。"""
    checked: list[str] = []
    for candidate in _config_candidates():
        checked.append(str(candidate))
        if candidate.is_file():
            return candidate
    raise RuntimeError("找不到 config/app.yaml（版本号来源）：" + "; ".join(checked))


def _parse_config_version(path: Path) -> str | None:
    """读 ``app.version``。

    ``app:`` 段整个缺失属于配置文件损坏，直接报错；段在但没有 ``version``
    （升级前的老配置，安装器保留了它）返回 ``None``，由调用方用
    ``build_info.json`` 兜底并回写。
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RuntimeError(f"读不到 {path}：{exc}") from exc
    block = _APP_BLOCK_RE.search(text)
    if block is None:
        raise RuntimeError(f"{path} 缺少顶层 app: 段，无法读取版本号")
    match = _APP_VERSION_RE.search(block.group(1))
    if match is None:
        return None
    return match.group(1).strip()


def _version_code(version: str) -> int:
    """``1.2.3`` → ``10203``；rc/beta 后缀不参与比较。"""
    match = _SEMVER_RE.match(version)
    if not match:
        raise RuntimeError(f"版本号必须是 major.minor.patch 形式：{version!r}")
    major, minor, patch = (int(group) for group in match.groups())
    return major * 10000 + minor * 100 + patch


def _read_build_info_version(config_path: Path | None = None) -> tuple[str, int] | None:
    """读安装包根目录 ``build_info.json`` 的版本；没有 / 不合法返回 ``None``。"""
    candidates: list[Path] = []
    env = os.environ.get(_BUILD_INFO_ENV)
    if env:
        candidates.append(Path(env))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / _BUILD_INFO_FILE)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / _BUILD_INFO_FILE)
    if config_path is not None:
        # <安装目录>/config/app.yaml → <安装目录>/build_info.json
        # （源码直跑、但用环境变量指向已安装目录的配置时也能兜底）
        candidates.append(config_path.resolve().parent.parent / _BUILD_INFO_FILE)
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            version = str(data.get("version") or "").strip()
            code = int(data.get("versionCode") or 0)
        except (OSError, ValueError, TypeError):
            continue
        # 只接受自洽的构建戳（versionCode 必须由 version 算出），避免被改乱的
        # 文件把“当前版本”带到任意值。
        try:
            if version and code == _version_code(version):
                return version, code
        except RuntimeError:
            continue
    return None


def _write_config_version(path: Path, version: str) -> None:
    """把 yaml 里 ``app.version`` 那一行改成 ``version``（其余内容原样保留）。"""
    try:
        # 用 open(newline="") 而不是 read_text(newline=...)：后者是 Python 3.13
        # 才有的参数，内置的 QGIS Python 是 3.12。newline="" 保留原文件的
        # CRLF/LF，否则读进来的行尾会统一成 \n，写回时整个配置都变了。
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            text = handle.read()
        block = _APP_BLOCK_RE.search(text)
        if block is None:
            return
        segment = block.group(1)
        if _VERSION_LINE_RE.search(segment):
            updated = _VERSION_LINE_RE.sub(
                _replace_version_line(version), segment, count=1
            )
        else:
            # 老配置（升级前没有 version 行）：插到 app: 段首，缩进沿用段内第一行
            newline = "\r\n" if "\r\n" in segment else "\n"
            match = re.search(r"(?m)^([ \t]+)", segment)
            indent = match.group(1) if match else "  "
            updated = f'{indent}version: "{version}"{newline}{segment}'
        if updated == segment:
            return
        # 原子写：中途断电 / 崩溃不会留下半截 yaml（下次启动会判成配置损坏）。
        temp = path.with_name(path.name + ".tmp")
        with temp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text[: block.start(1)] + updated + text[block.end(1) :])
        os.replace(temp, path)
    except OSError:
        # 配置目录只读时不阻断启动：本次仍用新版本号，下次启动再试
        pass


def _replace_version_line(version: str):
    def replace(match: re.Match[str]) -> str:
        indent = match.group(0)[: len(match.group(0)) - len(match.group(0).lstrip())]
        return f'{indent}version: "{version}"'

    return replace


_config_path = _locate_config()
_configured_version = _parse_config_version(_config_path)
_build_version = _read_build_info_version(_config_path)

if _configured_version is None:
    # 升级前的老配置没有 app.version：以安装包 build_info 为准，并补写进配置。
    if _build_version is None:
        raise RuntimeError(
            f"{_config_path} 的 app: 段缺少 version:，"
            f"且旁边找不到可用的 {_BUILD_INFO_FILE}"
        )
    __version__, VERSION_CODE = _build_version
    _write_config_version(_config_path, __version__)
else:
    __version__ = _configured_version
    VERSION_CODE = _version_code(__version__)
    # 安装目录里的 build_info.json 描述的是**磁盘上这份代码**，配置里的版本
    # 是上一次运行留下的。两者不一致时以 build_info 为准并回写：
    #   - 升级：安装器保留了旧 app.yaml（版本偏小）
    #   - 升级失败回滚：回滚恢复的 build_info 比配置小，也要改回去，否则旧代码
    #     会用新版本号做更新检查
    if _build_version is not None and _build_version[1] != VERSION_CODE:
        _write_config_version(_config_path, _build_version[0])
        __version__, VERSION_CODE = _build_version

APP_ID = "qgis-desktop"

#: 单实例 / 安装器互斥体名（Inno Setup 的 AppMutex 用同一个名字等 App 退出）。
APP_MUTEX = "qgis-desktop-single-instance"

#: 发布通道。stable 只允许签名过的安装包；当前没有 Windows 代码签名证书，
#: 因此默认走 internal（客户端对 stable 会 fail closed）。
CHANNEL = "internal"
