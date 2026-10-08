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
import json
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import AppConfig, load_config
from .gateway import Client, Gateway

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"
SHARED = ROOT / "shared"          # 物模型：前端与后端同源（由 scripts/export_model.py 生成）

CONFIG: AppConfig = load_config()
HOST = CONFIG.host
PORT = CONFIG.port

# 运行时画面来源覆盖文件：初始化弹窗切换来源后写入，重启仍生效（优先级高于 config.toml）
LOCK_SOURCE_FILE = ROOT / "config.lock.json"


def _load_runtime_source() -> dict | None:
    if not LOCK_SOURCE_FILE.exists():
        return None
    try:
        data = json.loads(LOCK_SOURCE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("kind") in ("image", "camera", "stream"):
            return {
                "kind": data["kind"],
                "index": int(data.get("index", 0)),
                "url": str(data.get("url") or ""),
            }
    except Exception as err:
        print(f"[config] 画面来源覆盖文件 {LOCK_SOURCE_FILE.name} 读取失败，沿用 config.toml：{err!r}")
    return None


def _build_lock_options(cfg: AppConfig) -> dict | None:
    """把大门锁的扩展配置（天气画面 / 视频源 / 人脸模型）解析成网关可用的绝对路径。"""
    lock = cfg.by_device("lock.entry")
    if lock is None:
        return None

    def resolve(rel: str | None) -> str | None:
        return str(ROOT / rel) if rel else None

    # config.toml 静态配置为底，运行时覆盖文件（初始化弹窗写入）优先
    source = _load_runtime_source() or lock.source or {"kind": "image", "index": 0, "url": ""}
    face = lock.face or {}
    return {
        "scenes": {key: resolve(rel) for key, rel in (lock.scenes or {}).items()},
        "source": {
            "kind": source.get("kind", "image"),
            "index": source.get("index", 0),
            "url": source.get("url", ""),
        },
        "face": {
            "photo": resolve(str(face.get("photo") or "") or None),
            "threshold": face.get("threshold", 0.62),
            # 运行时「登记新用户」的人脸裁剪图目录，文件名即用户名
            "enrollDir": str(ROOT / "camera" / "faces"),
        },
        "stateFile": str(LOCK_SOURCE_FILE),
    }


gateway = Gateway(
    stream_ports=CONFIG.channel_ports(),
    stream_images={
        s.channel: str(ROOT / s.image)
        for s in CONFIG.streams if s.image
    },
    stream_fps=CONFIG.channel_fps(),
    lock_options=_build_lock_options(CONFIG),
)


def build_stream_app(channel: str) -> FastAPI:
    """
    独立推流端口上的最小应用：只提供该一路画面，地址就是 http://主机:端口/。
    与真实 IP 摄像头（/video.cgi）的部署形态一致，网关与流通道仍是同一个进程、
    同一个 StreamHub —— 一路画面只渲染一次，主端口和独立端口的观众共享帧。
    """
    sub = FastAPI(title=f"MSHouse Stream {channel}", docs_url=None, redoc_url=None)

    @sub.get("/")
    async def stream_root(request: Request):
        return gateway.hub.attach(request, channel)

    @sub.get("/health")
    async def stream_health():
        info = gateway.hub.info(channel)
        return JSONResponse({"ok": True, "service": "mshouse-stream", "stream": info})

    return sub


@asynccontextmanager
async def lifespan(app: FastAPI):
    await gateway.start()

    # config.toml 里给某路摄像头配了独立端口时，在同一事件循环里再起几个 uvicorn 服务
    extras: list[tuple[uvicorn.Server, asyncio.Task]] = []
    for sc in CONFIG.dedicated_streams():
        server = uvicorn.Server(uvicorn.Config(
            build_stream_app(sc.channel),
            host=HOST, port=sc.port,
            log_level="warning", access_log=False,
        ))
        task = asyncio.create_task(server.serve())

        def _note_done(t: asyncio.Task, channel: str = sc.channel, port: int = sc.port) -> None:
            err = t.exception()
            if err is not None:
                print(f"[config] 通道 {channel} 的独立推流端口 {port} 启动失败：{err!r}")

        task.add_done_callback(_note_done)
        extras.append((server, task))
        print(f"[config] 通道 {sc.channel}（{sc.device_id}）独立推流端口已就绪：http://{HOST}:{sc.port}/")

    try:
        yield
    finally:
        for server, task in extras:
            server.should_exit = True
        for _server, task in extras:
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
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
        "lock": gateway.lock_status(),
    })


@app.get("/api/model")
async def api_model():
    """全量设备影子（HTTP 侧只读接口，方便用 curl 直接看）。"""
    return JSONResponse(gateway.snapshot())


# ------------------------------------------------------------------ #
# 大门人脸锁：上传 / 移除人脸画面
# 直接接收图片二进制（Content-Type: image/*），避免 multipart 额外依赖；
# 画面变化后通过 WebSocket 流信令广播给所有控制端。
# ------------------------------------------------------------------ #
@app.post("/api/lock/face")
async def upload_lock_face(request: Request):
    data = await request.body()
    result = await gateway.set_lock_face(data)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.delete("/api/lock/face")
async def clear_lock_face():
    return JSONResponse(await gateway.clear_lock_face())


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
