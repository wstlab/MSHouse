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
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import security
from .config import AppConfig, load_config
from .gateway import AUTH_GRACE_SECONDS, Client, Gateway
from .media import SOURCE_KINDS
from .model import ERROR_CODES, FAULT_CODES, PROTOCOL_VERSION
from .vision import DEFAULT_THRESHOLD

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
        if isinstance(data, dict) and data.get("kind") in SOURCE_KINDS:
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
            "threshold": face.get("threshold", DEFAULT_THRESHOLD),
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
    security_config=CONFIG.security,
    device_config=CONFIG.devices,
)


def _startup_banner() -> None:
    """
    启动提示。

    首次启动时把自动生成的令牌与一次性口令打印出来 ——
    这是使用者拿到凭据的唯一机会（口令只存哈希，事后无法反推）。
    """
    sec = CONFIG.security
    print("=" * 62)
    print("MSHouse 镜像家居 · 网关已启动")
    print("=" * 62)
    if sec.allow_anonymous:
        print("⚠️  安全警告：security.allow_anonymous = true")
        print("    当前**任何人都能连接并控制设备**，仅限课堂演示使用。")
    elif sec.token:
        print(f"访问令牌：{sec.token}")
        print("  请把它填进 config.toml 的 security.token 以固定下来；")
        print("  也可以用环境变量 MSHOUSE_TOKEN 覆盖。")
    if sec.generated_password:
        print(f"初始化设置口令（本次启动生成，仅显示一次）：{sec.generated_password}")
        print("  请立即保存。要换固定口令，运行：python -m server.hash_password")
        print("  然后把输出的哈希填进 config.toml 的 security.setup_password_hash。")
    if CONFIG.devices.simulate_offline:
        print(f"仿真：以下设备保持离线 -> {', '.join(CONFIG.devices.simulate_offline)}")
    if CONFIG.devices.simulate_fault:
        for dev_id, code in CONFIG.devices.simulate_fault.items():
            print(f"仿真：{dev_id} 注入故障 {code}")
    print("=" * 62)


def _extract_token(ws: WebSocket) -> str:
    """
    从握手中提取访问令牌。

    三个入口覆盖不同客户端的能力差异：
      ① 子协议头 `Sec-WebSocket-Protocol: mshouse.v1, bearer.<token>`
         —— 浏览器的 WebSocket 构造函数没法自定义请求头，子协议是它唯一能传凭据的地方
      ② 查询串 ?token=<token>  —— 命令行工具与调试脚本最顺手
      ③ 首帧 hello 携带 token —— 某些 SDK 只能连上之后才发得出数据
    """
    proto = ws.headers.get("sec-websocket-protocol") or ""
    for item in proto.split(","):
        item = item.strip()
        if item.startswith("bearer."):
            return item[len("bearer."):]
    token = ws.query_params.get("token")
    return str(token) if token else ""


def _offered_subprotocol(ws: WebSocket) -> str | None:
    """回显客户端声明的 mshouse.v1 子协议（浏览器要求服务端回显才认）。"""
    proto = ws.headers.get("sec-websocket-protocol") or ""
    names = [p.strip() for p in proto.split(",") if p.strip()]
    return "mshouse.v1" if "mshouse.v1" in names else None


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


app = FastAPI(title="MSHouse Gateway", version=PROTOCOL_VERSION, lifespan=lifespan)


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """HTML / JS / CSS 每次访问都回源校验（ETag 命中仍走 304）。

    项目按用户要求去掉了 `?v=` 版本戳，静态资源就只能靠缓存头保证「改完刷新即生效」；
    否则浏览器/CDN 会继续用旧脚本，出现「代码里明明加了新设备，页面上却没有」。
    流通道（/?stream=…）是长连接，必须跳过。
    """
    response = await call_next(request)
    path = request.url.path
    if not request.query_params.get("stream") and (
        path == "/" or path.endswith((".html", ".js", ".css"))
    ):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ------------------------------------------------------------------ #
# 视频流通道：GET /?stream=233666 → multipart/x-mixed-replace 长连接
# 像素走这里，不走 WebSocket；浏览器 <img src> 直接就能播。
# 注意：本路由必须在根路径静态挂载之前注册。
# ------------------------------------------------------------------ #
@app.get("/")
async def index(request: Request, stream: str | None = None):
    if stream:
        # 流通道鉴权。
        # <img src> 没法自定义请求头，所以除了令牌，还支持一次性签名 URL
        # （?exp=&sig=，由 /api/stream-url 签发）—— 这是真实摄像头
        # 也普遍采用的方案（HLS/RTSP-over-HTTP 的临时地址同源同理）。
        if not _stream_allowed(request, stream):
            return PlainTextResponse("未授权：视频通道需要有效令牌或签名\n", status_code=401)
        return gateway.hub.attach(request, stream)
    return FileResponse(PUBLIC / "index.html")


def _stream_allowed(request: Request, channel: str) -> bool:
    if CONFIG.security.allow_anonymous:
        return True
    token = request.query_params.get("token") or ""
    if not token:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if gateway.check_access_token(token):
        return True
    try:
        expires = int(request.query_params.get("exp") or 0)
    except (TypeError, ValueError):
        return False
    sig = request.query_params.get("sig") or ""
    ok, _reason = security.verify_stream(channel, expires, sig, gateway.token)
    return ok


@app.get("/api/stream-url")
async def stream_url(request: Request, channel: str):
    """签发一次性流地址（供 <img> 这类无法带请求头的场景使用）。"""
    if not CONFIG.security.allow_anonymous:
        token = request.query_params.get("token") or ""
        if not gateway.check_access_token(token):
            return JSONResponse({"ok": False, "code": ERROR_CODES.UNAUTHORIZED,
                                 "message": "需要有效令牌"}, status_code=401)
    expires = int(time.time()) + CONFIG.security.stream_sign_ttl
    sig = security.sign_stream(channel, expires, gateway.token)
    return JSONResponse({
        "ok": True,
        "channel": channel,
        "url": f"/?stream={channel}&exp={expires}&sig={sig}",
        "expiresAt": expires,
        "ttl": CONFIG.security.stream_sign_ttl,
    })


@app.get("/streams")
async def streams():
    return JSONResponse({"streams": gateway.hub.list()},
                        headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    stats = gateway.stats()
    return JSONResponse({
        "ok": True,
        "service": "mshouse",
        "runtime": "python",
        "version": PROTOCOL_VERSION,
        "clients": stats["clients"],
        "authedClients": stats["authedClients"],
        "streams": gateway.hub.list(),
        "lock": gateway.lock_status(),
        "devices": stats["devices"],
        "backpressure": {
            "outboxDropped": stats["outboxDropped"],
            "outboxCoalesced": stats["outboxCoalesced"],
        },
        "commands": {
            "total": stats["commandsTotal"],
            "rateLimited": stats["rateLimited"],
        },
    })


@app.post("/api/simulate")
async def simulate(request: Request):
    """
    仿真控制：让设备离线/恢复、注入或清除故障。

    仅用于教学演示与测试（真实设备当然不会提供这种接口）。
    它本身就是敏感操作 —— 所以与命令走同一套令牌校验，
    不能因为「只是个演示接口」就放行。
    """
    if not CONFIG.security.allow_anonymous:
        token = request.query_params.get("token") or ""
        if not gateway.check_access_token(token):
            return JSONResponse({"ok": False, "code": ERROR_CODES.UNAUTHORIZED,
                                 "message": "需要有效令牌"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": ERROR_CODES.BAD_JSON,
                             "message": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "code": ERROR_CODES.INVALID_PARAM,
                             "message": "请求体必须是对象"}, status_code=400)

    device_id = str(body.get("deviceId") or "")
    device = gateway.devices.get(device_id)
    if device is None:
        return JSONResponse({"ok": False, "code": ERROR_CODES.DEVICE_NOT_FOUND,
                             "message": f"设备不存在：{device_id}"}, status_code=404)

    action = str(body.get("action") or "")
    if action == "offline":
        gateway._simulated_offline.add(device_id)
        device.missed_heartbeats = 99
        gateway._set_online(device, False, reason="仿真置为离线")
    elif action == "online":
        gateway._simulated_offline.discard(device_id)
        device.missed_heartbeats = 0
        device.last_seen_at = int(time.time() * 1000)
        gateway._set_online(device, True, reason="仿真置为在线")
    elif action == "fault":
        code = str(body.get("faultCode") or "")
        if code not in vars(FAULT_CODES).values():
            return JSONResponse({"ok": False, "code": ERROR_CODES.INVALID_PARAM,
                                 "message": f"未知故障码：{code}"}, status_code=400)
        gateway.raise_fault(device_id, code)
    elif action == "clearFault":
        gateway.clear_fault(device_id, body.get("faultCode") or None)
    else:
        return JSONResponse({"ok": False, "code": ERROR_CODES.INVALID_PARAM,
                             "message": "action 必须是 offline/online/fault/clearFault"},
                            status_code=400)

    return JSONResponse({
        "ok": True, "deviceId": device_id, "action": action,
        "online": device.online, "faults": list(device.faults),
    })


@app.get("/api/diagnostics")
async def diagnostics():
    """诊断接口：最近事件、故障、限流与背压统计。"""
    return JSONResponse({
        "ok": True,
        "version": PROTOCOL_VERSION,
        "stats": gateway.stats(),
        "faults": {
            d.id: list(d.faults.values())
            for d in gateway.devices.values() if d.faults
        },
        "recentEvents": gateway.events[:40],
        "config": {
            "protocolVersion": PROTOCOL_VERSION,
            "commandRateLimit": CONFIG.devices.command_rate_limit,
            "coalesceWindowMs": CONFIG.devices.coalesce_window_ms,
            "statePersist": CONFIG.devices.state_persist or None,
            "allowAnonymous": CONFIG.security.allow_anonymous,
        },
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
# 大门锁 · 人脸登记库（设置弹窗「人脸锁」卡片）
#
# 读接口开放（和 /health 里的 lock.face 同源，不含任何隐私内容）；
# 写接口（登记 / 删除）要带设置口令 —— 它们是破坏性操作，而 HTTP 端点
# 不像 WebSocket 那样有会话可挂鉴权，所以由前端把设置弹窗里已经验证过的
# 口令放进 X-Setup-Password 请求头带过来。
# ------------------------------------------------------------------ #
@app.get("/api/lock/faces")
async def list_lock_faces():
    return JSONResponse(gateway.lock_faces())


@app.post("/api/lock/enroll")
async def enroll_lock_face(request: Request, name: str = ""):
    if not gateway.verify_setup_password(request.headers.get("x-setup-password")):
        return JSONResponse({"ok": False, "error": "初始化口令不正确"}, status_code=403)
    data = await request.body()
    result = await gateway.enroll_lock_face(name, data)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.delete("/api/lock/enroll")
async def remove_lock_face(request: Request, name: str = ""):
    if not gateway.verify_setup_password(request.headers.get("x-setup-password")):
        return JSONResponse({"ok": False, "error": "初始化口令不正确"}, status_code=403)
    result = await gateway.remove_lock_face(name)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


# ------------------------------------------------------------------ #
# 大门锁 · 浏览器摄像头推帧
#
# 浏览器端 getUserMedia 拿到画面 → 画到 canvas → toBlob('image/jpeg') → POST 到这里。
# 为什么像素不走 WebSocket：本项目一贯的分工是「WebSocket 走控制与信令、
# HTTP 走像素」（见 README 的并发模型一节）。摄像头的 MJPEG 也是这么出去的，
# 推帧方向反过来而已 —— 保持一致，学生只需要理解一套模型。
#
# 为什么不做成服务端去拉浏览器：浏览器里的摄像头根本没有可访问的地址，
# 服务端也没法反向连进浏览器，所以只能由浏览器主动推。
# ------------------------------------------------------------------ #
@app.post("/api/lock/frame")
async def push_lock_frame(request: Request):
    data = await request.body()
    result = await gateway.push_browser_frame(data)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


# ------------------------------------------------------------------ #
# WebSocket
# ------------------------------------------------------------------ #
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """
    WebSocket 接入。

    与旧版最大的区别：**连上不等于能用**。
    旧版 `await ws.accept()` 之后不校验任何东西，任何人发一条
    {"action":"unlock"} 就能开门。现在握手阶段先取令牌，取不到就只给
    一条 hello，要求客户端在宽限期内补交。
    """
    token = _extract_token(ws)
    authed = gateway.check_access_token(token) if token else gateway.allow_anonymous
    await ws.accept(subprotocol=_offered_subprotocol(ws))

    client = Client(ws)
    client.authed = authed
    client.pump = asyncio.create_task(client.run())
    gateway.attach(client)

    watchdog: asyncio.Task | None = None
    if not client.authed:
        async def auth_watchdog() -> None:
            """认证宽限期看门狗。没有它，未认证连接可以无限占位。"""
            await asyncio.sleep(AUTH_GRACE_SECONDS)
            if client.authed:
                return
            gateway._send_error(client, ERROR_CODES.UNAUTHORIZED, "认证超时，连接即将关闭")
            await asyncio.sleep(0.05)
            try:
                await ws.close(code=4401, reason="auth timeout")
            except Exception:
                pass
        watchdog = asyncio.create_task(auth_watchdog())

    try:
        while True:
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                break
            try:
                await gateway.handle(client, raw)
            except Exception as err:
                # 关键改动：单条报文处理失败**不再带走整个连接**。
                # 旧版这里是一句 `except Exception: pass`，结果是
                # 一个畸形参数（比如 brightness:"abc"）会让客户端
                # 直接掉线，而且拿不到任何原因 —— 排查成本极高。
                print(f"[ws] 处理报文失败：{err!r}")
                traceback.print_exc()
                gateway._send_error(client, ERROR_CODES.INTERNAL,
                                    f"处理报文时出错：{type(err).__name__}")
    finally:
        if watchdog is not None:
            watchdog.cancel()
        gateway.detach(client)


# ------------------------------------------------------------------ #
# 静态资源（放最后，避免吃掉上面的路由）
# ------------------------------------------------------------------ #
app.mount("/shared", StaticFiles(directory=str(SHARED)), name="shared")
app.mount("/", StaticFiles(directory=str(PUBLIC), html=True), name="public")


def main() -> None:
    _startup_banner()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
