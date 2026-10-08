"""
视频流通道（MJPEG over HTTP）· Python / asyncio 版

设计要点（与原 Node 版一致）：
  1. 画面不走 WebSocket。WebSocket 只传「流信令」（通道名、是否在推、云台角…），
     像素走标准 HTTP 长连接：GET /?stream=<通道名>
  2. 响应是 multipart/x-mixed-replace，浏览器 <img> 原生就能播，不需要 MSE / WebRTC。
     真实 IP 摄像头（/video.cgi、/mjpg/video.mjpg）用的就是这套，所以替换成本最低。
  3. 一个通道只渲染一次，广播给所有订阅者，CPU 不随观看人数增长。
  4. 只有存在订阅者时才启动帧循环；没人看就不烧 CPU。

Python 特有的两个决定：
  · 渲染与 JPEG 编码是 CPU 密集任务，全部通过 asyncio.to_thread 丢到线程池。
    numpy / Pillow 在计算时会释放 GIL，所以既不会卡住事件循环，也能真正并行。
  · 背压改用「每订阅者一个有界队列」实现：队列满了直接丢帧，
    等价于 Node 版判断 res.writableLength 后跳过的策略。

分层：本文件只认识「通道 / 订阅者 / JPEG」，不认识设备。设备状态由外部通过 context() 提供。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from .model import now_ms

BOUNDARY = "mshouseStream"


class Channel:
    """一个视频通道的运行期状态。"""

    __slots__ = (
        "channel", "name", "osd_name", "device_id", "source", "standby", "context",
        "port", "fps", "live", "viewers", "task", "frames", "started_at", "last_frame",
        "last_tick", "rush",
    )

    def __init__(self, channel: str, spec: dict[str, Any]) -> None:
        self.channel = channel
        self.name = spec.get("name", "")
        # 画面 OSD 上的名字（点阵字只有 ASCII）
        self.osd_name = spec.get("osdName") or spec.get("name", "")
        self.device_id = spec.get("deviceId", "")
        self.source = spec["source"]
        self.standby = spec.get("standby")
        self.context: Callable[[], dict[str, Any]] = spec.get("context", lambda: {})
        # 独立推流端口（0 / None 表示复用主服务端口，画面走 /?stream=<通道>）
        self.port = int(spec.get("port") or 0)
        # 该通道推流帧率；0 表示用 StreamHub 的默认帧率（由 define 兜底）
        self.fps = int(spec.get("fps") or 0)
        self.live = False
        self.viewers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.frames = 0
        self.started_at = 0
        self.last_frame: bytes | None = None
        self.last_tick = 0.0
        self.rush = False


class StreamHub:
    def __init__(self, fps: int = 8, idle_fps: int = 2, quality: int = 72) -> None:
        self.fps = fps
        self.idle_fps = idle_fps
        self.quality = quality
        self.channels: dict[str, Channel] = {}

    # ---------------- 通道管理 ----------------

    def define(self, channel: str, spec: dict[str, Any]) -> None:
        """注册一个通道。通道存在与否和「是否在推流」是两件事。"""
        ch = Channel(str(channel), spec)
        # 通道没单独配帧率时回落到 hub 默认帧率
        if ch.fps <= 0:
            ch.fps = self.fps
        self.channels[str(channel)] = ch

    def has(self, channel: str) -> bool:
        return str(channel) in self.channels

    def get(self, channel: str) -> Channel | None:
        return self.channels.get(str(channel))

    def set_live(self, channel: str, live: bool) -> bool:
        """由设备指令驱动：切换该通道的实景 / 待机画面。"""
        ch = self.get(channel)
        if ch is None or ch.live == bool(live):
            return False
        ch.live = bool(live)
        if ch.live:
            ch.started_at = now_ms()
        if ch.viewers:
            ch.rush = True          # 立刻换帧，观众不用等下一个周期
        return True

    def is_live(self, channel: str) -> bool:
        ch = self.get(channel)
        return bool(ch and ch.live)

    def kick(self, channel: str) -> None:
        """画面内容在外部被更换（如上传人脸）时催一帧，观众不用等下一个帧周期。"""
        ch = self.get(channel)
        if ch is not None and ch.viewers:
            ch.rush = True

    def info(self, channel: str) -> dict[str, Any] | None:
        """通道描述，进 snapshot / ack 用。"""
        ch = self.get(channel)
        if ch is None:
            return None
        return {
            "channel": ch.channel,
            "name": ch.name,
            "deviceId": ch.device_id,
            "live": ch.live,
            "viewers": len(ch.viewers),
            "fps": ch.fps if ch.live else self.idle_fps,
            # port 为 null：画面复用主端口，url 是相对地址 /?stream=<通道>；
            # port 有值：该路在独立端口推流，前端按当前主机名拼 http://主机:port/
            "port": ch.port or None,
            "url": f"/?stream={ch.channel}",
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.info(c) for c in self.channels]

    # ---------------- HTTP 订阅 ----------------

    def attach(self, request: Request, channel: str) -> Response:
        """处理 GET /?stream=<channel>：不存在 → 404，存在 → multipart 长连接。"""
        ch = self.get(channel)
        if ch is None:
            available = ", ".join(self.channels)
            return PlainTextResponse(
                f"未知的视频通道：{channel}\n可用通道：{available}\n", status_code=404
            )
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Access-Control-Allow-Origin": "*",
            "X-Stream-Channel": ch.channel,
        }
        if request.method == "HEAD":
            return Response(
                status_code=200,
                headers={"Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}", **headers},
            )
        return StreamingResponse(
            self._mjpeg(ch, request),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers=headers,
        )

    async def _mjpeg(self, ch: Channel, request: Request):
        """每个订阅者一个生成器 + 一个有界队列，队列满即丢帧（背压）。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        ch.viewers.add(q)
        self._ensure_task(ch)
        try:
            # 首帧：<img> 一挂上就有画面，不用等一个周期
            if ch.last_frame:
                yield self._part(ch.last_frame)
            while True:
                jpg = await q.get()
                yield self._part(jpg)
        finally:
            ch.viewers.discard(q)
            if not ch.viewers:
                self._stop(ch)

    @staticmethod
    def _part(jpg: bytes) -> bytes:
        head = (
            f"--{BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpg)}\r\n\r\n"
        ).encode("ascii")
        return head + jpg + b"\r\n"

    # ---------------- 帧循环 ----------------

    def _ensure_task(self, ch: Channel) -> None:
        if ch.task is None or ch.task.done():
            ch.task = asyncio.create_task(self._run(ch))

    def _stop(self, ch: Channel) -> None:
        if ch.task is not None:
            ch.task.cancel()
            ch.task = None

    async def _run(self, ch: Channel) -> None:
        try:
            while ch.viewers:
                t0 = time.monotonic()
                try:
                    jpg = await asyncio.to_thread(self._render_and_encode, ch)
                except Exception as err:  # 渲染失败不能把整个循环带走
                    print(f"[stream] 通道 {ch.channel} 渲染失败：{err!r}")
                    jpg = None
                if jpg:
                    ch.frames += 1
                    ch.last_frame = jpg
                    for q in list(ch.viewers):
                        try:
                            q.put_nowait(jpg)
                        except asyncio.QueueFull:
                            pass        # 客户端跟不上，丢这一帧
                target_fps = ch.fps if ch.live else self.idle_fps
                delay = 1.0 / max(1, target_fps) - (time.monotonic() - t0)
                if ch.rush:
                    ch.rush = False
                    delay = 0.0
                await asyncio.sleep(max(0.005, delay))
        except asyncio.CancelledError:
            raise
        finally:
            ch.task = None

    def _render_and_encode(self, ch: Channel) -> bytes | None:
        """同步渲染 + 编码。由 _run 通过 to_thread 调用（不阻塞事件循环）。

        实景源 / 待机源统一为同一个 render 签名（state/env/now/dt/fps/channel/name）。
        """
        current_fps = ch.fps if ch.live else self.idle_fps
        now = now_ms()
        dt = min(0.5, (now - ch.last_tick) / 1000) if ch.last_tick else 1.0 / current_fps
        ch.last_tick = now
        source = ch.source if ch.live else ch.standby
        if source is None:
            return None
        ctx = ch.context() or {}
        raster = source.render(
            state=ctx.get("state") or {},
            env=ctx.get("env") or {},
            now=now,
            dt=dt,
            fps=current_fps,
            channel=ch.channel,
            name=ch.osd_name,
        )
        if raster is None:
            return None
        return raster.to_jpeg(self.quality)

    def dispose(self) -> None:
        for ch in self.channels.values():
            self._stop(ch)
            ch.viewers.clear()
