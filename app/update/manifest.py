"""更新清单解析、校验与安装包选择。

清单由服务端生成、离线签名，客户端**先验签再解析**（见
:mod:`app.update.checker`）。本模块只处理“已经验过签的原始字节”，但仍然做
严格校验——签名保证来源可信，不保证服务端代码没有 bug；字段类型、文件名、
URL、哈希格式都要能挡住畸形数据。

字段与 Java 端 ``AppReleaseService.buildManifest`` 一一对应：

.. code-block:: json

    {
      "schema": 1, "appId": "qgis-desktop", "channel": "internal",
      "publishSeq": 7, "version": "1.0.1", "versionCode": 10001,
      "releaseId": 12, "generatedAt": "2026-10-22T08:00:00Z",
      "releaseNotes": "...", "mandatory": false, "minVersionCode": 0,
      "packages": [{
        "id": 3, "kind": "full", "fileName": "qgis_dev-1.0.1.exe",
        "sizeBytes": 471859200, "sha256": "…", "runtimeId": "windows-x86_64-cp312-qgis3.40",
        "os": "windows", "arch": "x86_64", "url": "api/app/download/3"
      }]
    }

包选择顺序：``runtimeId`` 精确匹配优先，其次 ``os`` / ``arch``（空串视为
通配）；同等条件下 ``full`` 优先于 ``code``——自动安装走完整安装器，代码包
留给后续版本。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from app.update.errors import UpdateManifestError

#: 当前支持的清单结构版本
MANIFEST_SCHEMA = 1

#: 安装包类型
KIND_FULL = "full"
KIND_CODE = "code"
_PACKAGE_KINDS = (KIND_FULL, KIND_CODE)

#: 服务端允许的最大包体（8GB，与 AppUploadService 一致）
_MAX_PACKAGE_BYTES = 8 * 1024 * 1024 * 1024

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# --------------------------------------------------------------------- 数据类


@dataclass(frozen=True)
class UpdatePackage:
    """清单中的一个安装包。"""

    id: int
    kind: str
    file_name: str
    size_bytes: int
    sha256: str
    runtime_id: str
    os: str
    arch: str
    url: str

    @property
    def is_full(self) -> bool:
        return self.kind == KIND_FULL

    def matches(self, *, runtime_id: str | None, os_name: str, arch: str) -> bool:
        """硬性条件：runtimeId / os / arch 不匹配就排除（空值视为通配）。"""
        if self.runtime_id and (not runtime_id or self.runtime_id != runtime_id):
            return False
        if self.os and self.os.lower() != (os_name or "").lower():
            return False
        return not (self.arch and _norm_arch(self.arch) != _norm_arch(arch))

    def score_for(self, *, runtime_id: str | None, os_name: str, arch: str) -> int:
        score = 0
        if self.runtime_id and self.runtime_id == runtime_id:
            score += 40
        if self.os and self.os.lower() == (os_name or "").lower():
            score += 20
        if self.arch and _norm_arch(self.arch) == _norm_arch(arch):
            score += 10
        score += 5 if self.is_full else 1
        return score


@dataclass(frozen=True)
class UpdateManifest:
    """已验签的更新清单。"""

    schema: int
    app_id: str
    channel: str
    publish_seq: int
    version: str
    version_code: int
    release_id: int
    generated_at: str
    release_notes: str
    mandatory: bool
    min_version_code: int
    packages: tuple[UpdatePackage, ...]

    def select_package(
        self,
        *,
        runtime_id: str | None,
        os_name: str,
        arch: str,
    ) -> UpdatePackage | None:
        """挑一个可安装的包；没有匹配返回 ``None``。"""
        candidates = [
            (pkg.score_for(runtime_id=runtime_id, os_name=os_name, arch=arch), pkg)
            for pkg in self.packages
            if pkg.matches(runtime_id=runtime_id, os_name=os_name, arch=arch)
        ]
        if not candidates:
            return None
        # 分数相同时取 id 大的（后上传的包通常修正了前一个包的问题）
        candidates.sort(key=lambda item: (item[0], item[1].id), reverse=True)
        return candidates[0][1]

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> UpdateManifest:
        """解析清单原文；格式不合法抛 :class:`UpdateManifestError`。"""
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise UpdateManifestError("更新清单不是合法的 UTF-8 JSON") from exc
        if not isinstance(data, dict):
            raise UpdateManifestError("更新清单根节点必须是对象")

        schema = _int(data, "schema", minimum=1)
        if schema != MANIFEST_SCHEMA:
            raise UpdateManifestError(f"不支持的清单结构版本：{schema}")

        app_id = _str(data, "appId")
        channel = _str(data, "channel")
        publish_seq = _int(data, "publishSeq", minimum=1)
        version = _str(data, "version")
        if not _VERSION_RE.match(version):
            raise UpdateManifestError(f"版本号格式不合法：{version}")
        version_code = _int(data, "versionCode", minimum=1)
        release_id = _int(data, "releaseId", minimum=1)
        generated_at = _str(data, "generatedAt", default="")
        release_notes = _str(data, "releaseNotes", default="")
        mandatory = _bool(data, "mandatory", default=False)
        min_version_code = _int(data, "minVersionCode", default=0, minimum=0)

        raw_packages = data.get("packages")
        if not isinstance(raw_packages, list) or not raw_packages:
            raise UpdateManifestError("更新清单没有安装包")
        packages = tuple(_parse_package(item, index) for index, item in enumerate(raw_packages))

        return cls(
            schema=schema,
            app_id=app_id,
            channel=channel,
            publish_seq=publish_seq,
            version=version,
            version_code=version_code,
            release_id=release_id,
            generated_at=generated_at,
            release_notes=release_notes,
            mandatory=mandatory,
            min_version_code=min_version_code,
            packages=packages,
        )


# --------------------------------------------------------------------- 解析


def _parse_package(item: Any, index: int) -> UpdatePackage:
    where = f"packages[{index}]"
    if not isinstance(item, dict):
        raise UpdateManifestError(f"{where} 必须是对象")
    package_id = _int(item, "id", minimum=1, where=where)
    kind = _str(item, "kind", where=where)
    if kind not in _PACKAGE_KINDS:
        raise UpdateManifestError(f"{where}.kind 不合法：{kind}")
    file_name = _str(item, "fileName", where=where)
    _validate_file_name(file_name, where)
    size_bytes = _int(item, "sizeBytes", minimum=1, where=where)
    if size_bytes > _MAX_PACKAGE_BYTES:
        raise UpdateManifestError(f"{where}.sizeBytes 超出上限")
    sha256 = _str(item, "sha256", where=where).lower()
    if not _SHA256_RE.match(sha256):
        raise UpdateManifestError(f"{where}.sha256 不是 64 位十六进制")
    url = _str(item, "url", where=where)
    _validate_url(url, where)
    return UpdatePackage(
        id=package_id,
        kind=kind,
        file_name=file_name,
        size_bytes=size_bytes,
        sha256=sha256,
        runtime_id=_str(item, "runtimeId", default="", where=where),
        os=_str(item, "os", default="", where=where),
        arch=_str(item, "arch", default="", where=where),
        url=url,
    )


def _validate_file_name(name: str, where: str) -> None:
    if not name or name in (".", ".."):
        raise UpdateManifestError(f"{where}.fileName 为空")
    if len(name) > 255:
        raise UpdateManifestError(f"{where}.fileName 过长")
    if "/" in name or "\\" in name or "\x00" in name:
        raise UpdateManifestError(f"{where}.fileName 不能包含路径分隔符")


def _validate_url(url: str, where: str) -> None:
    """允许 http(s) 绝对地址或站内相对路径；拒绝协议跳转与路径穿越。"""
    if not url or _CONTROL_RE.search(url):
        raise UpdateManifestError(f"{where}.url 为空或含控制字符")
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            raise UpdateManifestError(f"{where}.url 协议不合法：{parts.scheme}")
        if not parts.hostname:
            raise UpdateManifestError(f"{where}.url 缺少主机名")
        if parts.username or parts.password:
            raise UpdateManifestError(f"{where}.url 不得包含账号信息")
        return
    if url.startswith(("/", "\\")):
        raise UpdateManifestError(f"{where}.url 必须相对 API 根路径")
    if ":" in url.split("/", 1)[0]:
        raise UpdateManifestError(f"{where}.url 含非法协议前缀")
    if any(segment == ".." for segment in re.split(r"[/\\]", url)):
        raise UpdateManifestError(f"{where}.url 含路径穿越")


def _str(
    data: dict[str, Any],
    key: str,
    *,
    default: str | None = None,
    where: str = "manifest",
) -> str:
    if key not in data and default is not None:
        return default
    value = data.get(key)
    if value is None and default is not None:
        return default
    if not isinstance(value, str):
        raise UpdateManifestError(f"{where}.{key} 必须是字符串")
    return value


def _int(
    data: dict[str, Any],
    key: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    where: str = "manifest",
) -> int:
    if key not in data and default is not None:
        value: Any = default
    else:
        value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UpdateManifestError(f"{where}.{key} 必须是整数")
    if minimum is not None and value < minimum:
        raise UpdateManifestError(f"{where}.{key} 不能小于 {minimum}")
    return value


def _bool(data: dict[str, Any], key: str, *, default: bool = False) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise UpdateManifestError(f"manifest.{key} 必须是布尔值")


def _norm_arch(arch: str) -> str:
    value = (arch or "").strip().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(value, value)


def join_url(base_url: str, package_url: str) -> str:
    """把清单里的包地址拼到 API 根地址上（支持绝对 URL 原样返回）。"""
    if "://" in package_url:
        return package_url
    base = base_url if base_url.endswith("/") else base_url + "/"
    return urljoin(base, package_url)


__all__ = [
    "KIND_CODE",
    "KIND_FULL",
    "MANIFEST_SCHEMA",
    "UpdateManifest",
    "UpdatePackage",
    "join_url",
]
