"""
MSHouse 镜像家居 · 网关入口（Python 版 / FastAPI）

HTTP       提供前端静态资源 + 视频流通道（/?stream=<通道名>，MJPEG）
WebSocket  提供设备控制、状态与流信令（不含像素）

运行：
    python -m server.main          # 或 uvicorn server.main:app

注意：**必须单 worker 运行**。流通道的订阅者与设备影子都在进程内存里，
多 worker 会导致每个进程各持一份状态（详见 README 的「并发模型」一节）。
渲染是 CPU 密集任务，已通过 asyncio.to_thread 丢到线程池，不会阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .gateway import Client, Gateway

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"
SHARED = ROOT / "shared"          # 物模型：前端与后端同源（由 scripts/export_model.py 生成）
PORT = int(os.environ.get("PORT", "8080"))
HOST = "0.0.0.0"

gateway = Gateway()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await gateway.start()
    try:
        yield
    finally:
        await gateway.dispose()


app = FastAPI(title="MSHouse Gateway", version="1.0", lifespan=lifespan)


# ------------------------------------------------------------------ #
# 视频流通道：GET /?stream=233666 → multipart/x-mixed-replace 长连接
# 像素走这里，不走 WebSocket；浏览器 <img src> 直接就能播。
# 注意：本路由必须在根路径静态挂载之前注册。
# ------------------------------------------------------------------ #
@app.get("/")
async def index(request: Request, stream: str | None = None):
    if stream:
        return gateway.hub.attach(request, stream)
    return FileResponse(PUBLIC / "index.html")


@app.get("/streams")
async def streams():
    return JSONResponse({"streams": gateway.hub.list()},
                        headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return JSONResponse({
        "ok": True,
        "service": "mshouse",
        "runtime": "python",
        "version": "1.0",
        "clients": len(gateway.clients),
        "streams": gateway.hub.list(),
    })


@app.get("/api/model")
async def api_model():
    """全量设备影子（HTTP 侧只读接口，方便用 curl 直接看）。"""
    return JSONResponse(gateway.snapshot())


# ------------------------------------------------------------------ #
# WebSocket
# ------------------------------------------------------------------ #
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    client = Client(ws)
    client.pump = asyncio.create_task(client.run())
    gateway.attach(client)
    try:
        while True:
            raw = await ws.receive_text()
            await gateway.handle(client, raw)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        gateway.detach(client)


# ------------------------------------------------------------------ #
# 静态资源（放最后，避免吃掉上面的路由）
# ------------------------------------------------------------------ #
app.mount("/shared", StaticFiles(directory=str(SHARED)), name="shared")
app.mount("/", StaticFiles(directory=str(PUBLIC), html=True), name="public")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
