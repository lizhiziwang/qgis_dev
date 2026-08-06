import multiprocessing
import threading
import time

# from PyQt5.QtCore import QThread
from fastapi import FastAPI, APIRouter
from starlette.middleware.cors import CORSMiddleware
import uvicorn

from .routes.map import router

app = FastAPI(title="pgis")
# 配置跨域中间件（基础版：允许所有）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源（域名/端口），生产环境不推荐
    allow_credentials=True,  # 允许携带 Cookie 等认证信息
    allow_methods=["*"],  # 允许所有 HTTP 请求方法（GET、POST、PUT 等）
    allow_headers=["*"],  # 允许所有请求头（如 Content-Type、Authorization 等）
)

# api_router = APIRouter()
#
# api_router.include_router(router)

app.include_router(router, prefix="/getmap")
def run_api():

    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1)

def run():
    try:
        threading.Thread(target=run_api).start()
        return True
    except Exception as e:
        print(e)
        return False
# class MapServerThread(QThread):
#     def __init__(self):
#         super().__init__()
#
#     def run(self):



