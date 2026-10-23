#!/usr/bin/env python3
"""在 PyInstaller 产物根写 ``build_info.json``。

与 ``app/_build.py`` 同源（都来自 tools/stamp_source.py），区别只是落点：
本文件在产物根，更新器 / 运维脚本不启动 Qt 就能读；``app/_build.py`` 在包内，
运行期不依赖外部文件。两边字段必须一致，``--verify`` 用于 CI / 打包收尾校验。

用法::

    python tools/stamp_build.py --dist dist/app
    python tools/stamp_build.py --dist dist/app --verify
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from version import read_version, version_code_for  # noqa: E402

_PROJECT_ROOT = _HERE.parent
_FIELDS = (
    "buildId",
    "buildTime",
    "git",
    "dirty",
    "channel",
    "runtimeId",
    "pythonAbi",
    "qgisVersion",
)
_STR_RE = r'["\']([^"\']*)["\']'


def _read_stamp(path: Path) -> dict[str, str | bool]:
    text = path.read_text(encoding="utf-8")
    values: dict[str, str | bool] = {}
    mapping = {
        "BUILD_ID": "buildId",
        "BUILD_TIME": "buildTime",
        "GIT_REVISION": "git",
        "CHANNEL": "channel",
        "RUNTIME_ID": "runtimeId",
        "PYTHON_ABI": "pythonAbi",
        "QGIS_VERSION": "qgisVersion",
    }
    for attr, key in mapping.items():
        match = re.search(rf"^{attr}\s*=\s*{_STR_RE}", text, re.MULTILINE)
        if not match:
            raise ValueError(f"app/_build.py 缺 {attr}（先跑 tools/stamp_source.py）")
        values[key] = match.group(1)
    dirty = re.search(r"^GIT_DIRTY\s*=\s*(True|False)", text, re.MULTILINE)
    if not dirty:
        raise ValueError("app/_build.py 缺 GIT_DIRTY（先跑 tools/stamp_source.py）")
    values["dirty"] = dirty.group(1) == "True"
    return values


def expected_info(project_root: Path, channel: str | None = None) -> dict[str, object]:
    info = read_version(project_root)
    expected_code = version_code_for(info.version)
    if info.version_code != expected_code:
        raise ValueError(
            f"VERSION_CODE={info.version_code} 与版本 {info.version} 不匹配，应为 {expected_code}"
        )
    stamp = _read_stamp(project_root / "app" / "_build.py")
    if channel and stamp["channel"] != channel:
        raise ValueError(f"app/_build.py 的 CHANNEL={stamp['channel']} 与本次要求 {channel} 不一致")
    payload: dict[str, object] = {
        "appId": info.app_id,
        "version": info.version,
        "versionCode": info.version_code,
        "platform": f"{_os_tag()}-{_machine()}",
        "python": platform.python_version(),
    }
    for key in _FIELDS:
        payload[key] = stamp[key]
    return payload


def _os_tag() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _machine() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine or "?")


def _exe_name(platform_tag: str) -> str:
    return "app.exe" if platform_tag.startswith("windows") else "qgis_dev"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="写 / 校验产物根 build_info.json")
    parser.add_argument("--project-root", type=Path, default=_PROJECT_ROOT)
    parser.add_argument("--dist", type=Path, default=None, help="默认 <project-root>/dist/app")
    parser.add_argument("--channel", default=None)
    parser.add_argument("--verify", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).resolve()
    dist = Path(args.dist) if args.dist else project_root / "dist" / "app"
    if not dist.is_dir():
        print(f"ERROR: 产物目录不存在：{dist}", file=sys.stderr)
        return 1

    try:
        payload = expected_info(project_root, args.channel)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    exe = dist / _exe_name(str(payload["platform"]))
    if not exe.is_file():
        print(f"ERROR: 产物缺少可执行文件：{exe}", file=sys.stderr)
        return 1

    target = dist / "build_info.json"
    if args.verify:
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"ERROR: 读不到 {target}：{exc}", file=sys.stderr)
            return 1
        mismatched = {k: (current.get(k), v) for k, v in payload.items() if current.get(k) != v}
        if mismatched:
            for key, (got, want) in mismatched.items():
                print(f"ERROR: {key}: build_info={got!r} 期望={want!r}", file=sys.stderr)
            return 1
        print(f"OK: build_info.json 校验通过 version={payload['version']} buildId={payload['buildId']}")
        return 0

    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"OK: {target} version={payload['version']} buildId={payload['buildId']} "
        f"runtimeId={payload['runtimeId']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
