#!/usr/bin/env python3
"""生成 ``app/_build.py``：把版本 / 构建戳固化进本次产物。

在 **Cython 编译与 PyInstaller 之前**运行（见 tools/build_windows.ps1 STAGE 1.5
与 tools/build_linux.sh STAGE 0）。生成的模块随 ``app`` 包进 PYZ，运行期不再
依赖外部文件；产物根的 ``build_info.json`` 由 tools/stamp_build.py 另写一份。

默认要求工作区干净（``--allow-dirty`` 可放宽）：产物里的 git 哈希必须能对上
源码，否则 traceback 里的 .pyd 文件名:行号没有意义。

用法::

    python tools/stamp_source.py --channel internal
    python tools/stamp_source.py --allow-dirty --build-id 1.0.0+local.1
"""

from __future__ import annotations

import argparse
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from version import read_version, version_code_for  # noqa: E402

_PROJECT_ROOT = _HERE.parent
_BUILD_PY = _PROJECT_ROOT / "app" / "_build.py"

#: 参与“干净度”检查的路径。logs/dist/build/.tmp 等构建产物不在其中。
_SOURCE_PATHS = ("app", "scripts", "config", "resource", "tools", "pyproject.toml")


def _run_git(*args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def git_revision() -> str:
    rev = _run_git("rev-parse", "--short=8", "HEAD")
    return rev or "?"


def dirty_entries(allow_untracked: bool = True) -> list[str]:
    """返回影响构建的工作区改动。

    ``git diff-index`` 只看已跟踪文件；未跟踪但位于源码目录下的新文件同样会
    被打进产物（PyInstaller 收集 app.*），所以一并检查。``app/_build.py``
    是本脚本即将生成的，忽略。
    """
    entries: list[str] = []
    diff = _run_git("diff-index", "--name-only", "HEAD", "--", *_SOURCE_PATHS)
    if diff:
        entries.extend(diff.splitlines())
    if allow_untracked:
        status = _run_git("ls-files", "--others", "--exclude-standard", "--", *_SOURCE_PATHS)
        if status:
            entries.extend(status.splitlines())
    cleaned = []
    for item in entries:
        name = item.strip()
        if not name or name == "app/_build.py" or name.endswith((".pyc", ".pyo")):
            continue
        if "__pycache__" in name:
            continue
        cleaned.append(name)
    return sorted(set(cleaned))


def python_abi() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def qgis_version() -> str:
    try:
        from qgis.core import Qgis
    except Exception:  # noqa: BLE001 —— 纯 Python 环境生成源码戳也允许
        return "?"
    return str(getattr(Qgis, "QGIS_VERSION", "?"))


def _machine() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine or "?")


def _os_tag() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return re.sub(r"\W+", "-", sys.platform).strip("-") or "?"


def runtime_id(qgis: str, abi: str) -> str:
    """包兼容指纹：OS + 架构 + Python ABI + QGIS 次版本。

    只用于“这个包能不能装在这台机器”的粗匹配；full 包内部已自带全部运行库，
    真正的完整性校验始终是包 SHA-256 + 清单签名。
    """
    qgis_tag = "qgis?"
    match = re.match(r"(\d+)\.(\d+)", qgis)
    if match:
        qgis_tag = f"qgis{match.group(1)}.{match.group(2)}"
    return f"{_os_tag()}-{_machine()}-{abi}-{qgis_tag}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 app/_build.py 构建戳")
    parser.add_argument("--project-root", type=Path, default=_PROJECT_ROOT)
    parser.add_argument("--channel", default=None, help="覆盖 app/__init__.py 的 CHANNEL")
    parser.add_argument("--build-id", default=None, help="显式指定 buildId（默认 version+git）")
    parser.add_argument("--allow-dirty", action="store_true", help="工作区有改动也继续")
    args = parser.parse_args(argv)

    try:
        info = read_version(args.project_root)
        expected_code = version_code_for(info.version)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if info.version_code != expected_code:
        print(
            f"ERROR: VERSION_CODE={info.version_code} 与版本 {info.version} 不匹配，应为 {expected_code}",
            file=sys.stderr,
        )
        return 1

    changes = dirty_entries()
    if changes and not args.allow_dirty:
        print("ERROR: 工作区有未提交改动，构建戳会对不上源码：", file=sys.stderr)
        for name in changes[:30]:
            print(f"  {name}", file=sys.stderr)
        if len(changes) > 30:
            print(f"  ... 共 {len(changes)} 项", file=sys.stderr)
        print("提交后再打包，或显式传 --allow-dirty（仅用于本地调试）。", file=sys.stderr)
        return 1
    if changes:
        print(f"WARNING: --allow-dirty，忽略 {len(changes)} 项未提交改动", file=sys.stderr)

    git = git_revision()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    build_id = args.build_id or f"{info.version}+{git}" + (".dirty" if changes else "")
    channel = args.channel or info.channel
    abi = python_abi()
    qgis = qgis_version()
    rid = runtime_id(qgis, abi)

    content = f'''"""本次构建的版本戳（由 tools/stamp_source.py 生成，勿手改）。

buildId={build_id}
git={git}
channel={channel}
runtimeId={rid}
"""

from __future__ import annotations

BUILD_ID = {build_id!r}
BUILD_TIME = {now!r}
GIT_REVISION = {git!r}
GIT_DIRTY = {bool(changes)!r}
CHANNEL = {channel!r}
RUNTIME_ID = {rid!r}
PYTHON_ABI = {abi!r}
QGIS_VERSION = {qgis!r}
'''
    target = Path(args.project_root) / "app" / "_build.py"
    target.write_text(content, encoding="utf-8")
    print(f"OK: {target} buildId={build_id} runtimeId={rid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
