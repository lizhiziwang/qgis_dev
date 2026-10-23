"""更新清单验签公钥（编译进客户端）。

私钥离线保管在发布机（``.keys/update-private.pem``，已 gitignore），服务器与
客户端都只有公钥。换密钥时：

1. 同时更新本文件、``keyId`` 与服务端 ``update_public_key.pem``；
2. 保留旧公钥一个发布周期（``_PUBLIC_KEYS`` 可同时放多把），避免老客户端
   拿到新清单验不过；
3. 客户端只认 ``keyId`` 命中的公钥，未知 keyId 一律拒绝。
"""

from __future__ import annotations

#: 与 Java 端一致：SPKI DER 的 SHA-256 前 16 位十六进制
KEY_ID = "cbac7533ef6de3b5"

#: RSA-3072 SPKI 公钥（PKCS#1 v1.5 / SHA-256）
UPDATE_PUBLIC_KEY_PEM = """\
-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEA0zYngPOfxK3VBuKpFu2m
OsQcORZScw8zuaezGnSvO78ZUNelU/GqlWqDvn5SclumoQsuCp6S6fRasE6p1u31
Yf2We02Ih5fZN+6+vKOM7VVzclkD8FuQF+ZE0rkB25nu7VHAimsnj2oQ3uhnwBAh
DA5wyyfmfr2G7VFuMiHoSgedXPTAlUxnn5qaVdo5mdJ/JXJyBqMeFm27mcRZAwNL
HhSRFLD0cGZlxeAXtLSHbbmpWpsWey/x6VTisYPsChMx+M8Qpbo8yQB0+8B6zy7D
6wofHP6ybgHq+dTKm1NByPKweNcmRAhcHm6vBPYKgqNNIfVyksee1xR3RJnD9XPo
2x1DsVoPPqUGy13c6NST8vNISxPC1kqUXsy0DZDGtGUGq44xgCijR+84FS3QTvYN
qqeGHRo2Ivdvh6dV2QuoiYBRgrb5sfv2G5AWbxGn2HqUJA6br85cizYzmeNtIO2k
us6VBlhnw8Ifdr+6eb8JgBNwj5ITUlj+P0i+J6c8rE4nAgMBAAE=
-----END PUBLIC KEY-----
"""

#: keyId → PEM。轮换期可并存多把；清单的 ``keyId`` 决定用哪一把。
_PUBLIC_KEYS: dict[str, str] = {
    KEY_ID: UPDATE_PUBLIC_KEY_PEM,
}


def public_key_for(key_id: str) -> str | None:
    """按 keyId 取公钥 PEM；未知 keyId 返回 ``None``（调用方必须拒绝）。"""
    return _PUBLIC_KEYS.get((key_id or "").strip().lower())


def known_key_ids() -> tuple[str, ...]:
    return tuple(_PUBLIC_KEYS)


__all__ = [
    "KEY_ID",
    "UPDATE_PUBLIC_KEY_PEM",
    "known_key_ids",
    "public_key_for",
]
