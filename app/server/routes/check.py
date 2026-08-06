"""质检服务路由。

接收 JSON 配置 → :class:`CheckService.run_from_json` → 返回 :class:`CheckResult` 列表。
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.domain.check import CheckService
from app.core.domain.check.types import (
    CheckResult,
    CheckSeverity,
    CheckStatus,
)
from app.server.dependencies import get_check_service


router = APIRouter(tags=["check"])


class CheckRequest(BaseModel):
    """质检请求体。"""

    layer_uri: str = Field(..., description="QgsVectorLayer URI")
    layer_name: str = Field(..., description="layer name for in-memory provider")
    items: list[dict] = Field(default_factory=list, description="旧 checkParam JSON 列表")


def _result_to_dict(r: CheckResult) -> dict:
    return {
        "status": r.status.value,
        "severity": r.severity.value,
        "failed_feature_ids": list(r.failed_feature_ids),
        "failed_count": r.failed_count,
        "info": dict(r.info),
    }


@router.post("/run", summary="运行一组质检")
def run(
    payload: CheckRequest,
    svc: CheckService = Depends(get_check_service),
) -> dict:
    """从 JSON 配置 + layer 信息运行 :class:`CheckService.run_from_json`。

    注意：本路由在 server 进程跑，**不持有 QGIS GUI**，layer 必须用 in-memory
    或 PostGIS provider 提供；客户端负责把 :class:`QgsVectorLayer` 序列化成
    URI + name。
    """
    # server 进程内不能直接创建 UI layer；只跑 schema 校验
    # 真正执行依赖客户端上传的 layer（这里留作 schema 校验 + JSON 转换）
    try:
        from qgis.core import QgsVectorLayer

        layer = QgsVectorLayer(payload.layer_uri, payload.layer_name, "ogr")
        results: List[CheckResult] = []
        for item in payload.items:
            results.append(svc.run_from_json(item, layer=layer))
        return {
            "layer_uri": payload.layer_uri,
            "results": [_result_to_dict(r) for r in results],
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/types", summary="返回所有已注册的 check type_id")
def list_types(svc: CheckService = Depends(get_check_service)) -> list[str]:
    return svc.available_types()
