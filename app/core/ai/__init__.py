"""AI 能力接入层（目前为 Meta SAM 子进程客户端）。"""

from __future__ import annotations

from app.core.ai.sam_client import SamClient, default_sidecar_path

__all__ = ["SamClient", "default_sidecar_path"]
