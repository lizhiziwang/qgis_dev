"""检查更新：拉取已签名清单 → 验签 → 解析 → 选择平台安装包。

信任链（顺序不能变）：

1. 服务端返回 ``Result{data:{manifest, signature, keyId, manifestSha256,…}}``；
2. 用 ``keyId`` 命中客户端**内置**公钥（未知 keyId 直接拒绝）；
3. 对 ``manifest`` 的 **base64 解码后的原始字节**做 RSA/SHA-256 验签——
   验签对象是服务端 prepare 时保存的原文，Java 与 Python 都不重新序列化；
4. 再校验 ``manifestSha256``（传输层完整性）后才 ``json.loads``；
5. 清单里的 ``appId`` / ``channel`` / ``versionCode`` / ``publishSeq`` 必须与
   响应外层字段一致（防止响应被拼接）。

网络层只用标准库 ``urllib``，与 :mod:`app.core.auth.client` 一致；任何失败都
抛 :class:`~app.update.errors.UpdateCheckError` 的子类，文案可直接展示。
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from app import APP_ID, CHANNEL, VERSION_CODE, __version__
from app.build_meta import read_build_info
from app.infra.logging import get_logger
from app.update import keys
from app.update.errors import (
    UpdateCheckError,
    UpdateConfigError,
    UpdateManifestError,
    UpdateSignatureError,
)
from app.update.manifest import UpdateManifest, UpdatePackage
from app.update.rsa import KeyFormatError, RsaPublicKey

logger = get_logger(__name__)

#: 清单响应大小上限（正常 < 100KB；防止服务端 / 代理返回巨型响应）
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: 当前允许自动更新的通道。stable 要求安装包有代码签名；在没有 Windows
#: 代码签名证书 / 麒麟 CA 之前，stable 一律 fail closed（宁可不更新，也不把
#: 未签名的可执行文件当稳定版推下去）。
_ALLOWED_CHANNELS = ("internal",)


def os_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform.lower()


def arch_name() -> str:
    machine = platform.machine().strip().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def runtime_id() -> str | None:
    """本机构建指纹；源码直跑时为 ``None``（不按 runtimeId 过滤包）。"""
    value = read_build_info().get("runtimeId", "").strip()
    return None if value in ("", "?") else value


@dataclass(frozen=True)
class CheckResult:
    """一次成功的检查结果（不代表一定有新版本）。

    ``manifest_raw`` / ``signature`` / ``key_id`` 原样保留：Linux 提权更新器
    必须**自己**再验一次签名（不能信任普通用户可写的任务文件），所以要把
    服务端返回的签名材料一并交给它。
    """

    manifest: UpdateManifest
    package: UpdatePackage | None
    manifest_raw: bytes = b""
    signature: bytes = b""
    key_id: str = ""


class UpdateChecker:
    """清单客户端。

    :param base_url: API 根地址（如 ``http://host:9003/cs-feedback/``）
    :param channel: 发布通道；空串回落到构建时的 :data:`app.CHANNEL`
    """

    def __init__(
        self,
        *,
        base_url: str,
        channel: str = "",
        timeout: float = 20.0,
        app_id: str = APP_ID,
        current_version_code: int = VERSION_CODE,
        runtime: str | None = None,
        os_name_: str | None = None,
        arch: str | None = None,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.channel = (channel or CHANNEL).strip().lower()
        self.timeout = max(3.0, float(timeout))
        self.app_id = app_id
        self.current_version_code = int(current_version_code)
        self.runtime = runtime if runtime is not None else runtime_id()
        self.os = os_name_ or os_name()
        self.arch = arch or arch_name()

    # ------------------------------------------------------------------ 公共

    def check(self, *, last_publish_seq: int = 0) -> CheckResult | None:
        """请求清单并验签。

        :return: 没有已发布版本时返回 ``None``。
        :raises UpdateConfigError: 地址为空 / 通道被禁用
        :raises UpdateCheckError: 网络或服务端错误
        :raises UpdateSignatureError: 签名 / 摘要不通过
        :raises UpdateManifestError: 清单内容非法
        """
        self._validate_config()
        url = self._build_url()
        logger.info(
            "检查更新：channel=%s versionCode=%s runtime=%s",
            self.channel,
            self.current_version_code,
            self.runtime or "unavailable",
        )
        body = self._request(url)
        data = self._unwrap(body)
        if data is None:
            logger.info("服务端没有已发布版本（channel=%s）", self.channel)
            return None
        manifest, manifest_raw, signature, key_id = self._verify_and_parse(
            data, last_publish_seq=last_publish_seq
        )
        package = manifest.select_package(
            runtime_id=self.runtime,
            os_name=self.os,
            arch=self.arch,
        )
        if package is None:
            logger.warning(
                "新版本 %s 没有匹配当前平台的安装包（runtime=%s os=%s arch=%s）",
                manifest.version,
                self.runtime or "?",
                self.os,
                self.arch,
            )
        else:
            logger.info(
                "清单验签通过：version=%s versionCode=%s publishSeq=%s package=%s(%s)",
                manifest.version,
                manifest.version_code,
                manifest.publish_seq,
                package.file_name,
                package.kind,
            )
        return CheckResult(
            manifest=manifest,
            package=package,
            manifest_raw=manifest_raw,
            signature=signature,
            key_id=key_id,
        )

    # ------------------------------------------------------------------ 内部

    def _validate_config(self) -> None:
        if not self.base_url:
            raise UpdateConfigError("未配置更新服务地址（update.base_url / services.feedback_url）")
        if not self.base_url.lower().startswith(("http://", "https://")):
            raise UpdateConfigError(f"更新服务地址不合法：{self.base_url}")
        if self.channel not in _ALLOWED_CHANNELS:
            raise UpdateConfigError(
                f"通道 {self.channel} 已禁用：当前安装包未做代码签名，"
                "只允许 internal 通道自动更新"
            )

    def _build_url(self) -> str:
        query = urllib.parse.urlencode(
            {
                "appId": self.app_id,
                "channel": self.channel,
                "versionCode": self.current_version_code,
                **({"runtimeId": self.runtime} if self.runtime else {}),
                "os": self.os,
                "arch": self.arch,
            }
        )
        return f"{self.base_url}/api/app/manifest?{query}"

    def _request(self, url: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    f"qgis_dev/{__version__} "
                    f"({self.runtime or f'{self.os}-{self.arch}'})"
                ),
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise UpdateCheckError(f"更新服务返回 HTTP {response.status}")
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise UpdateCheckError(f"更新服务返回 HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise UpdateCheckError(f"无法连接更新服务：{exc.reason}") from exc
        except TimeoutError as exc:
            raise UpdateCheckError("检查更新超时，请稍后重试") from exc
        except OSError as exc:
            raise UpdateCheckError(f"检查更新失败：{exc}") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise UpdateCheckError("更新服务响应过大，已中止")
        return body

    def _unwrap(self, body: bytes) -> dict[str, Any] | None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise UpdateCheckError("更新服务返回的不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise UpdateCheckError("更新服务响应格式不合法")
        code = payload.get("code")
        if code != 0:
            message = str(payload.get("message") or payload.get("msg") or "").strip()
            raise UpdateCheckError(f"更新服务返回错误：{message or code}")
        data = payload.get("data")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise UpdateCheckError("更新服务响应 data 字段格式不合法")
        return data

    def _verify_and_parse(
        self, data: dict[str, Any], *, last_publish_seq: int
    ) -> tuple[UpdateManifest, bytes, bytes, str]:
        try:
            manifest_raw = _b64(data.get("manifest"), "manifest")
            signature = _b64(data.get("signature"), "signature")
        except ValueError as exc:
            raise UpdateSignatureError(str(exc)) from exc
        key_id = str(data.get("keyId") or "").strip().lower()
        if keys.public_key_for(key_id) is None:
            raise UpdateSignatureError(f"清单使用了未知的签名密钥：{key_id or '(空)'}")

        expected_sha = str(data.get("manifestSha256") or "").strip().lower()
        if expected_sha and hashlib.sha256(manifest_raw).hexdigest() != expected_sha:
            raise UpdateSignatureError("更新清单摘要不匹配（传输过程可能被篡改）")

        manifest = verify_signed_manifest(
            manifest_raw,
            signature,
            key_id,
            app_id=self.app_id,
            channel=self.channel,
            last_publish_seq=last_publish_seq,
        )
        self._cross_check(data, manifest)
        return manifest, manifest_raw, signature, key_id

    def _cross_check(self, data: dict[str, Any], manifest: UpdateManifest) -> None:
        """响应外层字段必须与已验签清单一致。"""
        expected = {
            "appId": manifest.app_id,
            "channel": manifest.channel,
            "version": manifest.version,
            "versionCode": manifest.version_code,
            "publishSeq": manifest.publish_seq,
        }
        for key, value in expected.items():
            if key in data and data.get(key) != value:
                raise UpdateManifestError(
                    f"清单字段 {key} 与响应不一致：{data.get(key)!r} != {value!r}"
                )
        if manifest.app_id != self.app_id:
            raise UpdateManifestError(
                f"清单应用标识不匹配：{manifest.app_id} != {self.app_id}"
            )
        if manifest.channel != self.channel:
            raise UpdateManifestError(
                f"清单通道不匹配：{manifest.channel} != {self.channel}"
            )
        if manifest.min_version_code > manifest.version_code:
            raise UpdateManifestError("清单 minVersionCode 大于 versionCode，配置非法")


def verify_signed_manifest(
    manifest_raw: bytes,
    signature: bytes,
    key_id: str,
    *,
    app_id: str,
    channel: str,
    last_publish_seq: int = 0,
) -> UpdateManifest:
    """用内置公钥验签并解析清单（网络层与“待安装包恢复”共用）。

    :raises UpdateSignatureError: keyId 未知 / 签名不通过 / 发布序号回退
    :raises UpdateManifestError: 清单内容非法或应用/通道不匹配
    """
    key_id = (key_id or "").strip().lower()
    pem = keys.public_key_for(key_id)
    if pem is None:
        raise UpdateSignatureError(f"清单使用了未知的签名密钥：{key_id or '(空)'}")
    try:
        public_key = RsaPublicKey.from_pem(pem)
    except KeyFormatError as exc:  # 内置公钥损坏属于客户端构建问题
        raise UpdateSignatureError(f"内置更新公钥不可用：{exc}") from exc
    if not public_key.verify_sha256(manifest_raw, signature):
        raise UpdateSignatureError("更新清单签名校验失败，已拒绝本次更新")

    manifest = UpdateManifest.from_json_bytes(manifest_raw)
    if manifest.app_id != app_id:
        raise UpdateManifestError(f"清单应用标识不匹配：{manifest.app_id} != {app_id}")
    if manifest.channel != channel:
        raise UpdateManifestError(f"清单通道不匹配：{manifest.channel} != {channel}")
    if manifest.min_version_code > manifest.version_code:
        raise UpdateManifestError("清单 minVersionCode 大于 versionCode，配置非法")
    if manifest.publish_seq < last_publish_seq:
        raise UpdateSignatureError(
            f"更新清单发布序号回退（{manifest.publish_seq} < {last_publish_seq}），"
            "疑似重放的旧清单"
        )
    return manifest


def _b64(value: Any, name: str) -> bytes:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"更新清单缺少 {name}")
    try:
        # 服务端 Base64 编码器不产生换行；validate=True 同时挡住夹带空白
        return base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise ValueError(f"更新清单 {name} 不是合法 base64") from exc


__all__ = [
    "CheckResult",
    "UpdateChecker",
    "arch_name",
    "os_name",
    "runtime_id",
    "verify_signed_manifest",
]
