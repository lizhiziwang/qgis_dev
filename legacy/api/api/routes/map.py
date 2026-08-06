import json
import os.path
from pathlib import Path

from fastapi import APIRouter
from starlette.responses import FileResponse

router = APIRouter(tags=["getmap"])


def load_config():
    """加载 JSON 配置文件，带异常处理和安全的资源管理"""
    # 1. 优化路径处理：使用 Path 构建跨平台路径，避免硬编码的可移植性问题
    config_path = r"api/resource/config_xyz.json"

    try:
        # 2. 使用 with 语句（上下文管理器）：自动关闭文件句柄，避免泄露
        with open(config_path, "r", encoding="utf-8") as f:
            # 3. 解析 JSON 配置
            config = json.load(f)
            return config
    except FileNotFoundError as e:
        print(e)

@router.get("/test")
def test():
    return "成功！"

def get_config(id: int):
    for item in load_config():
        if item["id"] == id:
            return item
    return None

@router.get("/map/{id}/{z}/{x}/{y}", summary="获取地图瓦片图片")
def out_map(id: int, z: int, x: int, y: int):
    item_c = get_config(id)
    if item_c is None:
        return None
    tile_path = os.path.join(item_c["basePass"],  str(z), str(x), str(y)+".pbf")
    if os.path.exists(tile_path):
        return FileResponse(tile_path, media_type="application/x-protobuf")
    else:
        return {"status": "error", "code": 404, "message": "地图瓦片不存在"}



