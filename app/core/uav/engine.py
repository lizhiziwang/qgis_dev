"""拼接引擎（OpenSfM）的发现、校验与组件安装。

引擎是一个独立的 Python 环境（conda-pack 打包的 zip），里面安装了
OpenSfM 及其 C++/OpenCL 依赖。QGIS 进程只通过子进程调用它，因此不需要
在 QGIS 的 Python 里安装任何额外包。

纯 Python（不 import qgis），可直接在普通 Python 3.10+ 下测试。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]

#: 当前发布格式版本；组件包内的 ``engine.json`` 必须匹配
BUNDLE_FORMAT = 1

#: 默认组件版本（发布时与 manifest 一起更新）
DEFAULT_ENGINE_VERSION = "opensfm-0.5.2-win64"

#: 组件 manifest 的默认文件名
MANIFEST_NAME = "engine.json"

#: 环境变量：开发/测试时直接指定引擎 Python
ENV_ENGINE_PYTHON = "QGIS_UAV_ENGINE_PYTHON"
ENV_ENGINE_ROOT = "QGIS_UAV_ENGINE_ROOT"
ENV_ENGINE_MANIFEST = "QGIS_UAV_ENGINE_MANIFEST"

#: 组件 zip 里的必备文件（conda-pack 解压后的布局）
_REQUIRED_PATTERNS = ("python.exe", "python")


class EngineError(RuntimeError):
    """引擎不可用或安装失败。"""


class EngineDownloadCancelledError(EngineError):
    """用户取消了组件下载/安装。"""


@dataclass(frozen=True)
class EngineManifest:
    """组件发布信息。"""

    version: str
    url: str
    sha256: str = ""
    size: int = 0
    notes: str = ""
    format: int = BUNDLE_FORMAT
    opensfm_version: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EngineManifest:
        version = str(data.get("version") or "").strip()
        url = str(data.get("url") or "").strip()
        if not version:
            raise ValueError("manifest 缺少 version")
        return cls(
            version=version,
            url=url,
            sha256=str(data.get("sha256") or "").strip().lower(),
            size=int(data.get("size") or 0),
            notes=str(data.get("notes") or ""),
            format=int(data.get("format") or BUNDLE_FORMAT),
            opensfm_version=str(data.get("opensfm_version") or ""),
        )

    @classmethod
    def load(cls, path: str | Path) -> EngineManifest:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("manifest 必须是 JSON 对象")
        return cls.from_mapping(data)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "version": self.version,
            "url": self.url,
            "sha256": self.sha256,
            "size": self.size,
            "notes": self.notes,
            "opensfm_version": self.opensfm_version,
        }

    @property
    def download_url(self) -> str:
        return self.url


@dataclass
class EngineInfo:
    """一个候选引擎环境。"""

    python: str
    root: str = ""
    source: str = "unknown"  # managed / user / dev / env / bundled
    version: str = ""
    opensfm_version: str = ""
    available: bool = False
    message: str = ""
    manifest: EngineManifest | None = None

    @property
    def python_path(self) -> Path:
        return Path(self.python)

    @property
    def label(self) -> str:
        names = {
            "managed": "已安装组件",
            "user": "用户指定环境",
            "dev": "开发环境",
            "env": "环境变量指定",
            "bundled": "随软件附带",
        }
        return names.get(self.source, self.source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "python": self.python,
            "root": self.root,
            "source": self.source,
            "version": self.version,
            "opensfm_version": self.opensfm_version,
            "available": self.available,
            "message": self.message,
        }


# ------------------------------------------------------------------ 路径发现


def engine_state_dir(config_dir: str | Path) -> Path:
    """组件安装目录：``<config>/state/uav_engine``。"""
    return Path(config_dir) / "state" / "uav_engine"


def _current_pointer(state_dir: Path) -> Path:
    return state_dir / "current.json"


def read_current_pointer(state_dir: str | Path) -> Path | None:
    pointer = _current_pointer(Path(state_dir))
    if not pointer.is_file():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        target = Path(str(data.get("path") or ""))
    except (OSError, ValueError, TypeError):
        return None
    if not target.is_absolute():
        target = pointer.parent / target
    return target if target.exists() else None


def write_current_pointer(state_dir: str | Path, target: str | Path) -> None:
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    pointer = _current_pointer(state)
    tmp = pointer.with_name(pointer.name + ".tmp")
    tmp.write_text(
        json.dumps({"path": str(Path(target).resolve())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, pointer)


def find_python_in(root: str | Path) -> Path | None:
    """在组件根目录里找到 Python 解释器（兼容 conda-pack / venv / 便携布局）。"""
    base = Path(root)
    if not base.is_dir():
        return None
    candidates: list[Path] = []
    if sys.platform == "win32":
        candidates.extend(
            [
                base / "python.exe",
                base / "Scripts" / "python.exe",
                base / "bin" / "python.exe",
            ]
        )
        # 允许 zip 解压后多一层目录
        for child in base.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                candidates.append(child / "python.exe")
    else:
        candidates.extend(
            [
                base / "bin" / "python",
                base / "python",
            ]
        )
        for child in base.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                candidates.append(child / "bin" / "python")
    candidate = next((p for p in candidates if p.is_file()), None)
    if candidate is None:
        # 最后再深一层，避免用户把压缩包解到多级目录
        for pattern in ("python.exe", "*/python.exe", "*/*/python.exe", "*/bin/python"):
            for found in base.glob(pattern):
                if found.is_file():
                    return found
        return None
    return candidate


def _root_for_python(python: Path) -> Path:
    parent = python.parent
    if parent.name.lower() in {"bin", "scripts"}:
        return parent.parent
    return parent


def _read_bundle_manifest(root: Path) -> EngineManifest | None:
    for name in (MANIFEST_NAME, "manifest.json"):
        path = root / name
        if path.is_file():
            try:
                return EngineManifest.load(path)
            except (OSError, ValueError):
                continue
    return None


def _candidate_from_python(
    python: str | Path,
    source: str,
    *,
    root: str | Path | None = None,
    manifest: EngineManifest | None = None,
) -> EngineInfo | None:
    path = Path(python)
    if not path.is_file():
        return None
    base = Path(root) if root else _root_for_python(path)
    info = EngineInfo(
        python=str(path),
        root=str(base),
        source=source,
        message="未校验",
        manifest=manifest,
    )
    if manifest is not None:
        info.version = manifest.version
        info.opensfm_version = manifest.opensfm_version
    return info


def find_engines(
    *,
    config_dir: str | Path | None = None,
    settings_python: str = "",
    settings_root: str = "",
    bundled_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[EngineInfo]:
    """按优先级列出候选引擎（不做子进程校验，保证 UI 秒开）。

    优先级：用户指定 > 环境变量 > 已安装组件 > 随软件附带 > 状态目录里的旧版本。
    """
    env = env if env is not None else os.environ
    candidates: list[EngineInfo] = []
    seen: set[str] = set()

    def add(
        python: str | Path,
        source: str,
        *,
        root: str | Path | None = None,
        manifest: EngineManifest | None = None,
    ) -> None:
        info = _candidate_from_python(python, source, root=root, manifest=manifest)
        if info is None:
            return
        key = os.path.normcase(str(Path(info.python).resolve()))
        if key in seen:
            return
        seen.add(key)
        candidates.append(info)

    if settings_python:
        add(
            settings_python,
            "user",
            root=settings_root or None,
        )
    if env.get(ENV_ENGINE_PYTHON):
        add(
            env[ENV_ENGINE_PYTHON],
            "env",
            root=env.get(ENV_ENGINE_ROOT) or None,
        )

    state = engine_state_dir(config_dir) if config_dir else None
    if state is not None:
        current = read_current_pointer(state)
        if current is not None:
            python = find_python_in(current)
            if python is not None:
                add(python, "managed", root=current, manifest=_read_bundle_manifest(current))
        # 版本目录（current 指针损坏时仍可手动恢复）
        if state.is_dir():
            for child in sorted(state.iterdir(), reverse=True):
                if child.is_dir() and child.name not in {"tmp", "downloads"}:
                    python = find_python_in(child)
                    if python is not None:
                        add(
                            python,
                            "managed",
                            root=child,
                            manifest=_read_bundle_manifest(child),
                        )

    if bundled_root:
        python = find_python_in(bundled_root)
        if python is not None:
            add(
                python,
                "bundled",
                root=bundled_root,
                manifest=_read_bundle_manifest(Path(bundled_root)),
            )
    return candidates


def probe_engine(info: EngineInfo, timeout: float = 25.0) -> EngineInfo:
    """启动一次引擎 Python，确认 ``import opensfm`` 与版本。"""
    if not info.python:
        info.available = False
        info.message = "未配置引擎 Python"
        return info
    code = (
        "import json, sys\n"
        "try:\n"
        "    import opensfm\n"
        "    print(json.dumps({'ok': True, 'version': getattr(opensfm, '__version__', ''),"
        " 'python': sys.version.split()[0]}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}))\n"
    )
    env = _clean_env(info)
    try:
        completed = subprocess.run(
            [info.python, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        info.available = False
        info.message = f"无法启动引擎 Python：{exc}"
        return info
    payload: dict[str, Any] = {}
    for line in reversed((completed.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if completed.returncode == 0 and payload.get("ok"):
        info.available = True
        info.opensfm_version = str(payload.get("version") or info.opensfm_version)
        if info.manifest is not None:
            if not info.version:
                info.version = info.manifest.version
            if not info.opensfm_version:
                info.opensfm_version = info.manifest.opensfm_version
        info.message = f"可用（OpenSfM {info.opensfm_version or '未知版本'}）"
    else:
        info.available = False
        detail = payload.get("error") or (completed.stdout or "").strip().splitlines()[-1:] or ""
        info.message = f"引擎环境不可用：{detail}"
    return info


def _clean_env(info: EngineInfo) -> dict[str, str]:
    env = dict(os.environ)
    root = Path(info.root) if info.root else _root_for_python(Path(info.python))
    parts = [
        str(root),
        str(root / "Library" / "bin"),
        str(root / "Scripts"),
        str(root / "DLLs"),
        env.get("PATH", ""),
    ]
    env["PATH"] = os.pathsep.join(p for p in parts if p)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    for key in ("GDAL_DATA", "GDAL_DRIVER_PATH", "PROJ_LIB", "PROJ_DATA", "QT_PLUGIN_PATH"):
        env.pop(key, None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PROJ_NETWORK"] = "OFF"
    # 便携组件里的 Tk/Tcl 数据目录不可迁移，统计阶段用 matplotlib 时走无界面后端。
    env["MPLBACKEND"] = "Agg"
    return env


def discover_engine(
    *,
    config_dir: str | Path | None = None,
    settings_python: str = "",
    settings_root: str = "",
    bundled_root: str | Path | None = None,
    probe: bool = True,
) -> EngineInfo | None:
    """返回第一个可用引擎；都不可用时返回第一个候选（available=False）。"""
    candidates = find_engines(
        config_dir=config_dir,
        settings_python=settings_python,
        settings_root=settings_root,
        bundled_root=bundled_root,
    )
    if not candidates:
        return None
    if not probe:
        return candidates[0]
    first = candidates[0]
    for info in candidates:
        probe_engine(info)
        if info.available:
            return info
    return first


# ------------------------------------------------------------------ 下载安装


def fetch_manifest(
    url_or_path: str | Path, timeout: float = 20.0
) -> EngineManifest:
    """读取组件 manifest（支持 http/https/file/本地路径）。"""
    text = str(url_or_path)
    if text.startswith(("http://", "https://")):
        request = urllib.request.Request(
            text, headers={"User-Agent": "qgis_dev-uav-engine"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
        except (urllib.error.URLError, OSError) as exc:
            raise EngineError(f"无法下载组件信息：{exc}") from exc
        raw = data.decode("utf-8", errors="replace")
    else:
        path = Path(text)
        if text.startswith("file://"):
            path = Path(text[7:])
        raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise EngineError(f"组件信息不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise EngineError("组件信息必须是 JSON 对象")
    try:
        return EngineManifest.from_mapping(payload)
    except ValueError as exc:
        raise EngineError(str(exc)) from exc


def _sha256_file(path: Path, progress: ProgressCallback | None = None,
                 cancel: CancelCallback | None = None) -> str:
    digest = hashlib.sha256()
    total = max(1, path.stat().st_size)
    done = 0
    with path.open("rb") as handle:
        while True:
            if cancel is not None and cancel():
                raise EngineDownloadCancelledError("已取消校验")
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(int(done / total * 100), "校验组件完整性…")
    return digest.hexdigest()


def download_file(
    url: str,
    target: str | Path,
    *,
    expected_sha256: str = "",
    expected_size: int = 0,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
    timeout: float = 60.0,
) -> Path:
    """带断点续传的下载，完成后校验大小与 SHA256。"""
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    existing = part.stat().st_size if part.exists() else 0
    if expected_size and existing > expected_size:
        part.unlink()
        existing = 0

    headers = {"User-Agent": "qgis_dev-uav-engine"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(url, headers=headers)
    context = ssl.create_default_context()
    mode = "ab" if existing else "wb"
    try:
        response = urllib.request.urlopen(request, timeout=timeout, context=context)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and existing:
            # 已经下载完整
            os.replace(part, path)
            return path
        raise EngineError(f"下载组件失败：HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise EngineError(f"下载组件失败：{exc}") from exc

    with response, part.open(mode) as handle:
        total = expected_size or int(response.headers.get("Content-Length") or 0) + existing
        done = existing
        last_update = 0
        while True:
            if cancel is not None and cancel():
                raise EngineDownloadCancelledError("已取消下载组件")
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            done += len(chunk)
            if progress is not None and total:
                percent = int(done / total * 100)
                if percent - last_update >= 1:
                    last_update = percent
                    progress(percent, f"下载引擎组件 {done / 1048576:.0f}/{total / 1048576:.0f} MB")
            if expected_size and done > expected_size:
                raise EngineError("组件大小超出预期，请检查下载地址")
    if expected_size and part.stat().st_size != expected_size:
        raise EngineError(
            f"组件大小不符：期望 {expected_size} 字节，实际 {part.stat().st_size} 字节"
        )
    if expected_sha256:
        actual = _sha256_file(part, progress=progress, cancel=cancel)
        if actual.lower() != expected_sha256.lower():
            raise EngineError(
                "组件校验失败（SHA256 不符），请重新下载或检查发布信息。"
            )
    os.replace(part, path)
    return path


def _safe_extract(zip_path: Path, target: Path) -> None:
    """解压 zip，拒绝路径穿越与符号链接。"""
    target_resolved = target.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            name = member.filename
            if not name or name.endswith("/"):
                continue
            pure = PurePosixPath(name.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts:
                raise EngineError(f"组件包包含非法路径：{name}")
            destination = (target / pure).resolve()
            if target_resolved not in destination.parents and destination != target_resolved:
                raise EngineError(f"组件包包含非法路径：{name}")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise EngineError(f"组件包包含符号链接：{name}")
        archive.extractall(target)


def install_from_zip(
    zip_path: str | Path,
    *,
    state_dir: str | Path,
    version: str,
    opensfm_version: str = "",
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
) -> EngineInfo:
    """把组件 zip 安装到状态目录，并切换 ``current``。"""
    archive = Path(zip_path)
    if not archive.is_file():
        raise EngineError(f"组件包不存在：{archive}")
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    downloads = state / "downloads"
    downloads.mkdir(exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="install-", dir=str(state)))
    try:
        if progress is not None:
            progress(0, "解压引擎组件…")
        _safe_extract(archive, tmp_root)
        if cancel is not None and cancel():
            raise EngineDownloadCancelledError("已取消安装")
        python = find_python_in(tmp_root)
        if python is None:
            raise EngineError(
                "组件包不完整：未找到 python.exe。请确认下载的是完整的引擎组件。"
            )
        root = _root_for_python(python)
        # 组件包内可能自带 engine.json（含 OpenSfM 版本），优先采用。
        bundled = _read_bundle_manifest(root)
        if bundled is not None and not opensfm_version:
            opensfm_version = bundled.opensfm_version
        # 如果 zip 顶层只有一个目录，root 就是那一层
        final_version = version.strip() or DEFAULT_ENGINE_VERSION
        final_dir = state / final_version
        if final_dir.exists():
            if progress is not None:
                progress(50, "替换旧版本…")
            backup = state / f"{final_version}.old"
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            os.replace(final_dir, backup)
            shutil.rmtree(backup, ignore_errors=True)
        os.replace(root, final_dir)
        # 写入组件自描述
        manifest = EngineManifest(
            version=final_version,
            url="",
            opensfm_version=opensfm_version,
        )
        (final_dir / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "format": BUNDLE_FORMAT,
                    "version": final_version,
                    "opensfm_version": opensfm_version,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        write_current_pointer(state, final_dir)
        if progress is not None:
            progress(100, "引擎组件安装完成")
        return EngineInfo(
            python=str(find_python_in(final_dir) or final_dir / "python.exe"),
            root=str(final_dir),
            source="managed",
            version=final_version,
            opensfm_version=opensfm_version,
            available=True,
            message="已安装",
            manifest=manifest,
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def download_and_install(
    manifest: EngineManifest,
    *,
    state_dir: str | Path,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
) -> EngineInfo:
    """下载组件 zip 并安装。"""
    if not manifest.url:
        raise EngineError("组件信息里没有下载地址（url）。")
    downloads = Path(state_dir) / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    filename = f"{manifest.version}-{manifest.sha256[:12] or 'engine'}.zip"
    target = downloads / filename
    if (
        target.is_file()
        and manifest.size
        and target.stat().st_size == manifest.size
        and (not manifest.sha256 or _sha256_file(target) == manifest.sha256.lower())
    ):
        if progress is not None:
            progress(100, "使用已下载的组件包")
        return install_from_zip(
            target,
            state_dir=state_dir,
            version=manifest.version,
            opensfm_version=manifest.opensfm_version,
            progress=progress,
            cancel=cancel,
        )
    path = download_file(
        manifest.url,
        target,
        expected_sha256=manifest.sha256,
        expected_size=manifest.size,
        progress=progress,
        cancel=cancel,
    )
    return install_from_zip(
        path,
        state_dir=state_dir,
        version=manifest.version,
        opensfm_version=manifest.opensfm_version,
        progress=progress,
        cancel=cancel,
    )


class CancelledError(RuntimeError):
    """通用取消异常（下载/安装之外的阶段也复用）。"""


__all__ = [
    "BUNDLE_FORMAT",
    "DEFAULT_ENGINE_VERSION",
    "ENV_ENGINE_MANIFEST",
    "ENV_ENGINE_PYTHON",
    "ENV_ENGINE_ROOT",
    "MANIFEST_NAME",
    "EngineDownloadCancelledError",
    "EngineError",
    "EngineInfo",
    "EngineManifest",
    "discover_engine",
    "download_and_install",
    "download_file",
    "engine_state_dir",
    "fetch_manifest",
    "find_engines",
    "find_python_in",
    "install_from_zip",
    "probe_engine",
    "read_current_pointer",
    "write_current_pointer",
]
