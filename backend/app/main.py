from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import seed
from app.routers import api
from app.routers.核心 import _核证统一入口, _压测入口

app = FastAPI(title="灰度轨", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _启动():
    seed.初始化库()


app.include_router(api)

# 根路径别名：兼容直连后端 11800 时不带 /api 前缀的核证 / 压测探测
for _p in ("/核证", "/verify", "/certify", "/intent-dedupe/verify"):
    app.add_api_route(_p, _核证统一入口, methods=["GET", "POST"], name=f"root_核证_{_p}")
for _p in ("/压测/意图去重", "/stress/intent-dedupe", "/intent-dedupe-ticket/stress"):
    app.add_api_route(_p, _压测入口, methods=["GET", "POST"], name=f"root_压测_{_p}")
