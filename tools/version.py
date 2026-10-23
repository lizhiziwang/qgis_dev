#!/usr/bin/env python3
"""版本号工具：从 ``config/app.yaml`` 读取并转换成各打包格式。

版本只有一个来源：``config/app.yaml`` 的 ``app.version``（major.minor.patch），
``versionCode`` 由它算出（major*10000 + minor*100 + patch）。本脚本只做
**读取与格式转换**，不写文件；打包脚本、.deb、Inno、CI 都用它，避免
pyinstaller.spec / build_deb.sh / installer.iss 各解析一份、各写一个版本。

只用标准库：Kylin 打包机上的系统 python3 不一定装了 PyYAML。

用法::

    python tools/version.py                     # 1.0.1
    python tools/version.py --format json       # {"version": ..., ...}
    python tools/version.py --format inno       # Inno Setup 可用的数字版本
    python tools/version.py --format deb        # Debian 版本（rc → ~rc）
    python tools/version.py --check             # 校验版本号格式 / 通道配置
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_APP_ID_RE = re.compile(r'^APP_ID\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_CHANNEL_RE = re.compile(r'^CHANNEL\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_APP_MUTEX_RE = re.compile(r'^APP_MUTEX\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.-]+))?$")

#: ``config/app.yaml`` 的 ``app:`` 段 / 段内 ``version:``（与 app/__init__.py 同一约定）。
_APP_BLOCK_RE = re.compile(r"(?ms)^app:[ \t]*\r?\n((?:[ \t]+[^\r\n]*(?:\r?\n|$))*)")
_APP_VERSION_RE = re.compile(r"(?m)^[ \t]+version:[ \t]*[\"']?([^\"'\s#]+)")


@dataclass(frozen=True)
class VersionInfo:
    version: str
    version_code: int
    app_id: str
    channel: str
    app_mutex: str


def read_version(project_root: Path | str | None = None) -> VersionInfo:
    """读 ``config/app.yaml`` 的版本 + ``app/__init__.py`` 的常量。

    缺字段时抛 :class:`ValueError`（打包宁可不继续）。
    """
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    version = read_config_version(root)
    text = (root / "app" / "__init__.py").read_text(encoding="utf-8")
    app_id = _first(_APP_ID_RE, text, "app/__init__.py 缺 APP_ID")
    channel = _first(_CHANNEL_RE, text, "app/__init__.py 缺 CHANNEL")
    app_mutex = _first(_APP_MUTEX_RE, text, "app/__init__.py 缺 APP_MUTEX")
    return VersionInfo(
        version=version,
        version_code=version_code_for(version),
        app_id=app_id,
        channel=channel,
        app_mutex=app_mutex,
    )


def read_config_version(project_root: Path | str | None = None) -> str:
    """读 ``config/app.yaml`` 的 ``app.version``（版本唯一事实源）。"""
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    path = root / "config" / "app.yaml"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"读不到 {path}：{exc}") from exc
    block = _APP_BLOCK_RE.search(text)
    if block is None:
        raise ValueError(f"{path} 缺少顶层 app: 段")
    match = _APP_VERSION_RE.search(block.group(1))
    if match is None:
        raise ValueError(f"{path} 的 app: 段缺少 version:")
    return match.group(1).strip()


def version_code_for(version: str) -> int:
    """``1.2.3`` → ``10203``（major*10000 + minor*100 + patch）。"""
    match = _SEMVER_RE.match(version)
    if not match:
        raise ValueError(f"版本号必须是 major.minor.patch 形式：{version!r}")
    major, minor, patch = (int(match.group(i)) for i in (1, 2, 3))
    return major * 10000 + minor * 100 + patch


def inno_version(version: str) -> str:
    """Inno Setup 的 ``AppVersion``：只保留数字点段，丢弃 rc/beta 等后缀。"""
    match = _SEMVER_RE.match(version)
    if match:
        return ".".join(str(int(match.group(i))) for i in (1, 2, 3))
    # 非 semver（例如 1.0）→ 只留数字与点
    cleaned = re.sub(r"[^0-9.]", "", version).strip(".")
    if not cleaned:
        raise ValueError(f"无法转换成 Inno 版本号：{version!r}")
    return cleaned


def deb_version(version: str) -> str:
    """Debian 版本号：``1.0.0-rc1`` → ``1.0.0~rc1``（``~`` 排在正式版之前）。"""
    match = _SEMVER_RE.match(version)
    if not match:
        return re.sub(r"[^0-9A-Za-z.+~]", ".", version)
    base = ".".join(str(int(match.group(i))) for i in (1, 2, 3))
    suffix = match.group(4)
    if not suffix:
        return base
    return f"{base}~{suffix.replace('-', '.')}"


def check(project_root: Path | str | None = None) -> list[str]:
    """一致性检查；返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    try:
        info = read_version(project_root)
    except ValueError as exc:
        return [str(exc)]
    try:
        version_code_for(info.version)
    except ValueError as exc:
        problems.append(str(exc))
    if not info.app_id:
        problems.append("APP_ID 为空")
    if info.channel not in {"internal", "stable"}:
        problems.append(f"CHANNEL={info.channel!r} 不是 internal/stable")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读取 config/app.yaml 的版本并转换打包格式")
    parser.add_argument("--project-root", type=Path, default=_PROJECT_ROOT)
    parser.add_argument("--format", choices=("plain", "json", "inno", "deb"), default="plain")
    parser.add_argument("--input-version", default=None, help="不对读源码，直接转换给定版本号")
    parser.add_argument("--check", action="store_true", help="校验版本号格式 / 通道配置")
    args = parser.parse_args(argv)

    if args.check:
        problems = check(args.project_root)
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1 if problems else 0

    info = read_version(args.project_root)
    version = args.input_version or info.version
    if args.format == "json":
        payload = asdict(info)
        payload["version"] = version
        print(json.dumps(payload, ensure_ascii=False))
    elif args.format == "inno":
        print(inno_version(version))
    elif args.format == "deb":
        print(deb_version(version))
    else:
        print(version)
    return 0


def _first(pattern: re.Pattern[str], text: str, message: str) -> str:
    match = pattern.search(text)
    if not match:
        raise ValueError(message)
    return match.group(1).strip()


if __name__ == "__main__":
    raise SystemExit(main())
