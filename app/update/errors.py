"""更新链路异常。

所有异常的 ``str()`` 都是**可直接展示给用户**的中文文案；排查细节走日志。
调用方只需捕获 :class:`UpdateError` 一个基类。
"""

from __future__ import annotations


class UpdateError(Exception):
    """更新链路统一异常基类。"""


class UpdateConfigError(UpdateError):
    """更新配置不可用（未配置服务地址 / 通道被策略禁用）。"""


class UpdateCheckError(UpdateError):
    """检查更新失败（网络、服务端返回错误、响应格式非法）。"""


class UpdateSignatureError(UpdateError):
    """清单签名 / 摘要校验失败——必须拒绝，不能降级为“忽略”。"""


class UpdateManifestError(UpdateError):
    """清单内容非法（缺字段、版本号异常、包信息可疑）。"""


class UpdateDownloadError(UpdateError):
    """安装包下载失败（网络中断、长度或 SHA-256 不符）。"""


class UpdateCancelledError(UpdateError):
    """用户取消了下载；已下载分片保留，可续传。"""


class UpdateInstallError(UpdateError):
    """无法启动更新器（缺少安装包 / 当前为源码运行 / 安装器格式不支持）。"""


__all__ = [
    "UpdateCancelledError",
    "UpdateCheckError",
    "UpdateConfigError",
    "UpdateDownloadError",
    "UpdateError",
    "UpdateInstallError",
    "UpdateManifestError",
    "UpdateSignatureError",
]
