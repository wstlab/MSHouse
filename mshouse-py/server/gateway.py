"""
IoT Gateway（Python 版）

职责：维护设备影子、执行场景、周期遥测、向所有客户端广播状态与流信令。

教学点：这是「设备接入层」的模拟实现。真实项目里，MQTT/Zigbee 适配器会把
物理设备映射成同样的 state / command 接口，上层不用改。

视频：像素不经过 WebSocket。网关只做两件事 ——
  ① 按 stream 指令开关通道（StreamHub）
  ② 把通道地址作为「信令」广播出去，客户端自己去 HTTP 流里取画面

并发模型（与 Node 版的差异）：
  Node 里 broadcast() 直接 ws.send() 即可。Python 的发送是协程，若在同步的
  状态机里 await，会把整条调用链染成 async。所以这里给每个客户端配一个
  **出站队列**：同步逻辑只管把报文塞进队列（不阻塞），由每个连接自己的
  发送协程负责真正写出去。这既保持了业务逻辑的同步写法，也天然带上了背压。
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from PIL import Image

from .media import VideoSource
from .model import (
    DEVICE_CATALOG,
    GATEWAY_NAME,
    GATEWAY_PROTOCOL,
    SCENES,
    SETUP_PASSWORD,
    DeviceSpec,
    device_by_id,
    envelope,
    now_ms,
)
from .render.scenes import EntrySource, LivingRoomSource, StandbySource, _cover_crop
from .stream import StreamHub
from .vision import FaceMatcher

# 内置识别通过时显示的人名（与登记照片 camera/face.jpg 对应）
LOCK_RESIDENT_NAME = "登记住户"

# 陌生人脸登记提示的去抖参数：
#   连续 N 次识别到未登记人脸才弹一次询问；人脸消失 N 次后结束本轮；提示最长保留秒数
UNKNOWN_PROMPT_RUNS = 2
ENROLL_GONE_MISSES = 3
ENROLL_PROMPT_TTL = 60.0

# 上传人脸画面的限制：8MB 以内、最短边至少 64px、统一裁成通道分辨率
MAX_FACE_BYTES = 8 * 1024 * 1024
STREAM_WIDTH, STREAM_HEIGHT = 512, 320

# WMO 天气码 → 大门锁场景 key（对应 config.toml 的 [streams.lock_entry.scenes]）
def weather_category(code: Any) -> str:
    try:
        code = int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "cloudy"
    if code in (0, 1):
        return "sunny"
    if code == 2:
        return "cloudy"
    if code == 3:
        return "overcast"
    if code in (45, 48):
        return "fog"
    if (51 <= code <= 67) or (80 <= code <= 82) or (95 <= code <= 99):
        return "rain"
    if (71 <= code <= 77) or code in (85, 86):
        return "snow"
    return "cloudy"


# 日照相位 → 画面 OSD 英文标签（点阵字体只有 ASCII）
PHASE_LABELS = ("DAWN", "DAY", "DUSK", "NIGHT")

# 没有日出日落数据时的兜底日出日落（当地钟点，分钟）：06:30 / 17:50
_FALLBACK_SUNRISE = 6 * 60 + 30
_FALLBACK_SUNSET = 17 * 60 + 50

# 调色锚点偏移（相对日出 / 日落的分钟数）与 RGB 增益 + 亮度偏移：
#   黎明前冷蓝 → 日出暖橙 → 上午回到自然色 → 傍晚再暖 → 日落后冷蓝 → 深夜压暗
def _light_keyframes(sunrise_min: float, sunset_min: float) -> list[tuple[float, tuple[float, float, float], float]]:
    night: tuple[float, float, float] = (0.46, 0.55, 0.80)
    return [
        (sunrise_min - 45, (0.64, 0.62, 0.76), -6.0),    # 黎明前：蓝调时刻
        (sunrise_min + 8, (1.13, 0.82, 0.56), 2.0),      # 日出：金色暖光
        (sunrise_min + 70, (1.0, 1.0, 1.0), 0.0),        # 上午：自然日光
        (sunset_min - 55, (1.0, 1.0, 1.0), 0.0),         # 下午
        (sunset_min - 12, (1.14, 0.80, 0.54), 2.0),      # 日落：暖橙最强
        (sunset_min + 32, (0.70, 0.64, 0.78), -6.0),     # 黄昏：蓝调时刻
        (sunset_min + 75, night, -12.0),                 # 入夜
    ]


def _interp_light(t_min: float, sunrise_min: float, sunset_min: float) -> tuple[tuple[float, float, float], float]:
    """按一天中的分钟数在调色锚点之间线性插值；深夜段保持 night 参数。"""
    frames = _light_keyframes(sunrise_min, sunset_min)
    # 日落后 75 分钟 ~ 黎明前 45 分钟之间是深夜：压暗 + 偏蓝
    night_gain, night_add = (0.46, 0.55, 0.80), -12.0
    if t_min <= frames[0][0] or t_min >= frames[-1][0]:
        return night_gain, night_add
    for (t0, g0, b0), (t1, g1, b1) in zip(frames, frames[1:]):
        if t0 <= t_min <= t1:
            f = (t_min - t0) / (t1 - t0) if t1 > t0 else 0.0
            gain = (
                g0[0] + (g1[0] - g0[0]) * f,
                g0[1] + (g1[1] - g0[1]) * f,
                g0[2] + (g1[2] - g0[2]) * f,
            )
            return gain, b0 + (b1 - b0) * f
    return (1.0, 1.0, 1.0), 0.0


def clamp(n: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, n))


def round1(n: float) -> float:
    return round(n * 10) / 10


def round4(n: float) -> float:
    return round(n * 10000) / 10000


# 镜像设置开箱默认地址：浙江温州 · 温州科技高级中学
DEFAULT_SITE_NAME = "浙江温州"
DEFAULT_SITE_ADDRESS = "浙江省温州市瓯海区郭溪街道科高路 1 号（温州科技高级中学）"
DEFAULT_LAT = 27.9925
DEFAULT_LON = 120.5801


@dataclass
class Device:
    """设备影子：静态规格 + 运行期状态。"""

    id: str
    type: str
    name: str
    room: str
    floor: int
    capabilities: tuple[str, ...]
    channel: str | None
    state: dict[str, Any] = field(default_factory=dict)
    online: bool = True
    updated_at: int = 0

    @classmethod
    def from_spec(cls, spec: DeviceSpec, state: dict[str, Any]) -> "Device":
        return cls(
            id=spec.id, type=spec.type, name=spec.name, room=spec.room,
            floor=spec.floor, capabilities=spec.capabilities, channel=spec.channel,
            state=state, updated_at=now_ms(),
        )


class Client:
    """一个 WebSocket 连接。出站报文走队列，避免同步逻辑被迫 async 化。"""

    __slots__ = ("ws", "outbox", "pump")

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self.pump: asyncio.Task | None = None

    def push(self, data: str) -> None:
        """非阻塞投递。队列满了就丢最旧的一条，保证慢客户端不拖累网关。"""
        try:
            self.outbox.put_nowait(data)
            return
        except asyncio.QueueFull:
            pass
        try:
            self.outbox.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            self.outbox.put_nowait(data)
        except asyncio.QueueFull:
            pass

    async def run(self) -> None:
        """发送协程：把队列里的报文写进 WebSocket。"""
        while True:
            data = await self.outbox.get()
            await self.ws.send_text(data)


class Gateway:
    def __init__(self, stream_ports: dict[str, int] | None = None,
                 stream_images: dict[str, str] | None = None,
                 stream_fps: dict[str, int] | None = None,
                 lock_options: dict[str, Any] | None = None) -> None:
        self.clients: set[Client] = set()
        # 默认「回家模式」：每次首页访问前端也会重新下发一次 home 场景
        self.scene = "home"
        self.occupancy = "home"
        self.outdoor: dict[str, Any] = {"temp": 28.4, "humidity": 62, "source": "default"}
        self.site: dict[str, Any] = {
            "configured": False,
            "name": DEFAULT_SITE_NAME,
            "address": DEFAULT_SITE_ADDRESS,
            "lat": DEFAULT_LAT, "lon": DEFAULT_LON,
            "timezone": None, "weather": None,
        }
        self.seq = 1
        self.events: list[dict[str, Any]] = []
        self.devices: dict[str, Device] = {}
        self._last_signal: dict[str, str] = {}
        self.hub = StreamHub(fps=8, quality=72)
        self._tasks: list[asyncio.Task] = []
        self._http: httpx.AsyncClient | None = None
        # 通道号 → 独立推流端口（来自 config.toml，空表示全部复用主端口）
        self.stream_ports: dict[str, int] = dict(stream_ports or {})
        # 通道号 → 默认底图绝对路径（如大门锁的 camera/lock.jpg）
        self.stream_images: dict[str, str] = dict(stream_images or {})
        # 通道号 → 推流帧率（摄像头 5fps、大门锁 2fps，来自 config.toml）
        self.stream_fps: dict[str, int] = dict(stream_fps or {})
        # 大门锁「上传的人脸画面」：一张已裁成 512x320 的 PIL 图；None 时镜头朝门外草坪
        self.lock_face: Image.Image | None = None

        # —— 大门锁扩展配置（来自 config.toml [streams.lock_entry]）——
        lock_options = lock_options or {}
        # 场景 key（sunny/rain/.../night）→ 画面文件绝对路径
        self.lock_scenes: dict[str, str] = dict(lock_options.get("scenes") or {})
        source_cfg = lock_options.get("source") or {}
        self.video_source = VideoSource(
            kind=str(source_cfg.get("kind") or "image"),
            index=int(source_cfg.get("index") or 0),
            url=str(source_cfg.get("url") or ""),
            width=STREAM_WIDTH, height=STREAM_HEIGHT,
        )
        face_cfg = lock_options.get("face") or {}
        self.face_matcher = FaceMatcher(
            face_cfg.get("photo"), float(face_cfg.get("threshold") or 0.62),
            enroll_dir=face_cfg.get("enrollDir"), primary_name=LOCK_RESIDENT_NAME,
        )
        # 陌生人脸「是否登记用户」提示状态机：
        #   _unknown_run   连续识别到未登记人脸的次数
        #   _unknown_miss  连续没看到人脸的次数
        #   _enroll_cooldown 本轮点过「忽略」后，人脸离开前不再询问
        #   _enroll_prompt 当前待确认的询问（含触发帧与检测框，登记直接用这一帧）
        self._unknown_run = 0
        self._unknown_miss = 0
        self._enroll_cooldown = False
        self._enroll_prompt: dict[str, Any] | None = None
        # 运行时画面来源覆盖（初始化弹窗设置）持久化到这个 sidecar JSON
        self.lock_source_file: str | None = lock_options.get("stateFile")
        # 最近一次识别的画面级信息（供画面 OSD 画人脸框）：box/detected/distance
        self._last_vision: dict[str, Any] = {"detected": False, "box": None, "distance": None}
        self._live_scan_at = 0.0

        self._define_streams()
        self._init_devices()

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        """启动周期任务。必须在事件循环里调用。"""
        self._http = httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "mshouse/1.0 (teaching)"})
        self._tasks.append(asyncio.create_task(self._loop_telemetry()))
        self._tasks.append(asyncio.create_task(self._loop_video_signal()))
        self._tasks.append(asyncio.create_task(self._loop_lock_vision()))
        print(f"[lock] 人脸模型：{self.face_matcher.reason}，阈值 {self.face_matcher.threshold}")

    async def dispose(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        self.hub.dispose()
        self.video_source.close(timeout=2.0)
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---------------- 初始化 ----------------

    def _define_streams(self) -> None:
        """
        注册视频通道。通道名来自物模型，地址形如 /?stream=233666。
        通道存在与否和「是否在推流」是两件事：通道一直在线，推流由 stream 指令控制。
        """
        standby = StandbySource(width=STREAM_WIDTH, height=STREAM_HEIGHT)
        cam = device_by_id("camera.living")
        lock = device_by_id("lock.entry")
        assert cam and lock

        # 大门锁：只有「推送画面」时镜头才对着门外（默认草坪道路 lock.jpg，
        # 上传人脸后切到人脸）；停止推送后和摄像头一样回到 STANDBY 无画面提示。
        entry = EntrySource(
            width=STREAM_WIDTH, height=STREAM_HEIGHT,
            channel=lock.channel, label="ENTRY LOCK",
            default_image=self.stream_images.get(lock.channel),
        )

        def cam_context() -> dict[str, Any]:
            c = self.devices.get("camera.living")
            lamp = self.devices.get("light.living")
            hour = time.localtime().tm_hour
            return {
                "state": c.state if c else {},
                "env": {
                    "night": hour < 6 or hour >= 18,
                    "hour": hour,
                    "occupancy": self.occupancy,
                    "lamp": (
                        {
                            "on": lamp.state["power"],
                            "brightness": lamp.state["brightness"],
                            "colorTemp": lamp.state["colorTemp"],
                        }
                        if lamp else {"on": False, "brightness": 0, "colorTemp": 4000}
                    ),
                },
            }

        self.hub.define(cam.channel, {
            "name": cam.name,                  # 中文名：进 /streams 与流信令
            "osdName": "LIVING ROOM CAM",      # 画面 OSD 用英文：点阵字只有 ASCII
            "deviceId": cam.id,
            "source": LivingRoomSource(channel=cam.channel, label="LIVING CAM"),
            "standby": standby,
            "context": cam_context,
            "port": self.stream_ports.get(cam.channel) or 0,
            "fps": self.stream_fps.get(cam.channel) or 0,
        })

        self.hub.define(lock.channel, {
            "name": lock.name,
            "osdName": "ENTRY DOOR LOCK",
            "deviceId": lock.id,
            "source": entry,
            # 与摄像头同一个待机源：停止推送 → STANDBY 无画面提示
            "standby": standby,
            "context": self._lock_context,
            "port": self.stream_ports.get(lock.channel) or 0,
            "fps": self.stream_fps.get(lock.channel) or 0,
        })

    def _sun_clock(self) -> tuple[float, float, float]:
        """返回别墅当地的 (当前分钟数, 日出分钟, 日落分钟)；没定位时按本机钟点兜底。"""
        weather = (self.site.get("weather") or {}) if self.site.get("configured") else {}
        tz_name = self.site.get("timezone")
        try:
            now_local = datetime.now(ZoneInfo(str(tz_name))) if tz_name else datetime.now()
        except Exception:
            now_local = datetime.now()
        now_min = now_local.hour * 60 + now_local.minute + now_local.second / 60

        def parse(value: Any) -> float | None:
            if not value:
                return None
            dt = datetime.fromisoformat(str(value))
            return dt.hour * 60 + dt.minute + dt.second / 60

        sr = parse(weather.get("sunrise")) or _FALLBACK_SUNRISE
        ss = parse(weather.get("sunset")) or _FALLBACK_SUNSET
        return now_min, sr, ss

    def _lock_scene(self) -> str | None:
        """按当地天气选出当前场景图（绝对路径）；昼夜变化交给 _lock_lighting 调色。"""
        default = self.stream_images.get("233667")
        weather = (self.site.get("weather") or {}) if self.site.get("configured") else {}
        key = weather_category(weather.get("code")) if weather else "cloudy"
        path = self.lock_scenes.get(key)
        if not path or not os.path.exists(path):
            path = default
        return path

    def _lock_lighting(self) -> tuple[tuple[float, float, float], float, str]:
        """按当日日出日落计算调色参数与日照相位（DAWN/DAY/DUSK/NIGHT）。"""
        now_min, sr, ss = self._sun_clock()
        gain, add = _interp_light(now_min, sr, ss)
        if now_min < sr - 45 or now_min > ss + 32:
            phase = "NIGHT"
        elif sr - 45 <= now_min <= sr + 70:
            phase = "DAWN"
        elif ss - 55 <= now_min <= ss + 32:
            phase = "DUSK"
        else:
            phase = "DAY"
        return gain, add, phase

    def _lock_context(self) -> dict[str, Any]:
        """大门锁通道每帧取一次的渲染上下文。"""
        device = self.devices.get("lock.entry")
        gain, add, phase = self._lock_lighting()
        env: dict[str, Any] = {
            "faceImage": self.lock_face,
            "sceneImage": self._lock_scene(),
            "phaseLabel": phase,
            "grade": (gain[0], gain[1], gain[2], add),
            "faceDetected": bool(self._last_vision.get("detected")),
            "faceBox": self._last_vision.get("box"),
        }
        # 只有「推送画面」时才从摄像头 / 视频流取实时帧；取帧还没就绪则回落场景底图
        if device is not None and device.state.get("streaming") and self.lock_face is None:
            env["frameImage"] = self.video_source.latest()
        return {"state": device.state if device else {}, "env": env}

    def _init_devices(self) -> None:
        for spec in DEVICE_CATALOG:
            self.devices[spec.id] = Device.from_spec(spec, self._default_state(spec))
        self._apply_scene("home", silent=True, source="boot")

    @staticmethod
    def _default_state(spec: DeviceSpec) -> dict[str, Any]:
        if spec.type == "light":
            return {"power": False, "brightness": 0, "colorTemp": 4000}
        if spec.type == "sensor":
            return {
                "temperature": 26.8 if spec.room == "kitchen" else 25.2,
                "humidity": 58 if spec.room == "bath" else 48,
                "comfort": "舒适",
            }
        if spec.type == "ac":
            return {"power": False, "mode": "cool", "targetTemp": 26, "fan": "auto", "indoorTemp": 27.5}
        if spec.type == "camera":
            # 推流是独立能力：默认关闭，等 stream 指令打开
            return {"power": True, "pan": 0, "streaming": False, "motion": False, "armed": True}
        if spec.type == "lock":
            # facePresent：镜头前是否有人脸画面（由上传接口驱动，画面从草坪道路切到人脸）
            return {"locked": True, "lastPerson": None, "lastResult": "idle",
                    "streaming": False, "battery": 86, "facePresent": False}
        return {}

    # ---------------- 连接管理 ----------------

    def attach(self, client: Client) -> None:
        self.clients.add(client)
        client.push(json_dumps(envelope("hello", {
            "role": "gateway",
            "name": GATEWAY_NAME,
            "protocol": GATEWAY_PROTOCOL,
        })))
        self.push_snapshot(client)
        self._push_event(client, {
            "level": "info",
            "source": "gateway",
            "message": "控制端已接入，已下发全量设备影子",
        })

    def detach(self, client: Client) -> None:
        self.clients.discard(client)
        if client.pump is not None:
            client.pump.cancel()
            client.pump = None

    # ---------------- 报文处理 ----------------

    async def handle(self, client: Client, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            self._send(client, envelope("error", {"code": "BAD_JSON", "message": "报文不是合法 JSON"}))
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            self._send(client, envelope("error", {"code": "BAD_ENVELOPE", "message": "缺少 type 字段"}))
            return

        msg_type = msg["type"]
        msg_id = msg.get("id") or f"srv-{self.seq}"
        self.seq += 1

        if msg_type == "command":
            self._on_command(client, msg, msg_id)
        elif msg_type == "scene":
            self._on_scene(client, msg, msg_id)
        elif msg_type == "ping":
            self._send(client, envelope("pong", {"echo": msg.get("payload")}, id=msg_id))
        elif msg_type == "snapshot":
            self.push_snapshot(client)
        elif msg_type == "setup":
            await self._on_setup(client, msg, msg_id)
        elif msg_type == "setupAuth":
            self._on_setup_auth(client, msg, msg_id)
        else:
            self._send(client, envelope("error", {
                "code": "UNKNOWN_TYPE",
                "message": f"未识别的消息类型：{msg_type}",
            }, id=msg_id))

    def _on_command(self, client: Client, msg: dict, msg_id: str) -> None:
        payload = msg.get("payload") or {}
        device_id = payload.get("deviceId")
        action = payload.get("action")
        params = payload.get("params") or {}
        device = self.devices.get(device_id)
        if device is None:
            self._send(client, envelope("ack", {
                "ok": False, "deviceId": device_id, "action": action, "error": "设备不存在",
            }, id=msg_id, ref=msg.get("id")))
            return

        result = self._execute(device, action, params)
        if result["ok"]:
            self._sync_stream(device)
        stream = self.hub.info(device.channel) if device.channel else None
        self._send(client, envelope("ack", {
            "ok": result["ok"],
            "deviceId": device_id,
            "action": action,
            "error": result.get("error"),
            "state": device.state,
            "stream": stream,
        }, id=msg_id, ref=msg.get("id")))

        if result["ok"]:
            device.updated_at = now_ms()
            self.broadcast(envelope("state", {
                "deviceId": device_id,
                "type": device.type,
                "room": device.room,
                "state": device.state,
                "online": device.online,
            }))
            if result.get("event"):
                self.broadcast_event(result["event"])

    def _execute(self, device: Device, action: str, params: dict) -> dict[str, Any]:
        s = device.state
        if action == "set" and isinstance(params, dict):
            return self._apply_patch(device, params)

        if device.type == "light":
            if action == "toggle":
                return self._apply_patch(device, {"power": not s["power"], "brightness": 0 if s["power"] else (s["brightness"] or 80)})
            if action == "on":
                return self._apply_patch(device, {"power": True, "brightness": params.get("brightness") or (s["brightness"] or 80)})
            if action == "off":
                return self._apply_patch(device, {"power": False, "brightness": 0})
        elif device.type == "ac":
            if action == "toggle":
                return self._apply_patch(device, {"power": not s["power"]})
            if action == "on":
                return self._apply_patch(device, {"power": True})
            if action == "off":
                return self._apply_patch(device, {"power": False})
        elif device.type == "camera":
            if action == "pan":
                return self._apply_patch(device, {"pan": params.get("pan")})
            if action == "nudge":
                return self._apply_patch(device, {"pan": s["pan"] + (params.get("delta") or 15)})
            if action == "toggle":
                return self._apply_patch(device, {"power": not s["power"]})
            if action == "arm":
                return self._apply_patch(device, {"armed": params.get("armed") is not False})
            # 推送 / 停止画面：只切通道的实景与待机，不改设备开关
            if action == "stream":
                return self._apply_patch(device, {"streaming": params.get("on") is not False})
        elif device.type == "lock":
            if action == "lock":
                return self._lock(device, True, params)
            if action == "unlock":
                return self._lock(device, False, params)
            if action == "stream":
                return self._apply_patch(device, {"streaming": params.get("on") is not False})
            # 陌生人脸登记询问面板的两个动作（事件 / 询问广播已在方法内完成）
            if action == "enrollFace":
                return self._enroll_lock_user(str(params.get("token") or ""),
                                              str(params.get("name") or ""))
            if action == "dismissEnroll":
                return self._dismiss_enroll_prompt(str(params.get("token") or ""))

        return {"ok": False, "error": f"动作 {action} 不适用于 {device.type}"}

    def _apply_patch(self, device: Device, patch: dict) -> dict[str, Any]:
        s = device.state
        if device.type == "light":
            if "power" in patch:
                s["power"] = bool(patch["power"])
            if "brightness" in patch:
                s["brightness"] = int(clamp(float(patch["brightness"] or 0), 0, 100))
            if "colorTemp" in patch:
                s["colorTemp"] = int(clamp(float(patch["colorTemp"] or 4000), 2700, 6500))
            if not s["power"]:
                s["brightness"] = 0
            if s["power"] and s["brightness"] == 0:
                s["brightness"] = 70
        elif device.type == "ac":
            if "power" in patch:
                s["power"] = bool(patch["power"])
            if "mode" in patch and patch["mode"] in ("cool", "heat", "fan", "dry", "auto"):
                s["mode"] = patch["mode"]
            if "targetTemp" in patch:
                s["targetTemp"] = int(clamp(float(patch["targetTemp"] or 26), 16, 30))
            if "fan" in patch and patch["fan"] in ("low", "mid", "high", "auto"):
                s["fan"] = patch["fan"]
        elif device.type == "camera":
            if "power" in patch:
                s["power"] = bool(patch["power"])
            if "streaming" in patch:
                s["streaming"] = bool(patch["streaming"]) and s["power"]
            if "pan" in patch:
                s["pan"] = (float(patch["pan"]) % 360 + 360) % 360
            if "armed" in patch:
                s["armed"] = bool(patch["armed"])
        elif device.type == "sensor":
            return {"ok": False, "error": "传感器为只读遥测设备"}
        elif device.type == "lock":
            if "locked" in patch:
                return self._lock(device, bool(patch["locked"]), patch)
            if "streaming" in patch:
                s["streaming"] = bool(patch["streaming"])

        return {"ok": True, "event": {
            "level": "info", "source": device.id, "message": f"{device.name} 状态已更新",
        }}

    def _sync_stream(self, device: Device) -> None:
        """把设备影子里的 streaming 状态同步到流通道（指令与场景都从这里过一道）。"""
        if not device.channel:
            return
        s = device.state
        live = bool(s["streaming"] and s["power"]) if device.type == "camera" else bool(s["streaming"])
        self.hub.set_live(device.channel, live)
        # 大门锁配了本机摄像头 / 网络视频流时，随推流开关打开或释放设备
        if device.type == "lock" and self.video_source.is_external:
            if live:
                self.video_source.open()
            else:
                # 不在这里 join 采集线程（可能阻塞 WS 处理），守护线程自行退出
                self.video_source.close(timeout=0.0)

    def _lock(self, device: Device, locked: bool, params: dict) -> dict[str, Any]:
        device.state["locked"] = locked
        device.state["lastResult"] = "locked" if locked else "unlocked"
        if params.get("person"):
            device.state["lastPerson"] = params["person"]
        person = params.get("person")
        return {"ok": True, "event": {
            "level": "warn" if locked else "ok",
            "source": device.id,
            "message": f"{device.name} 已上锁" if locked else f"{device.name} 已开锁" + (f" · {person}" if person else ""),
        }}

    # ---------------- 人脸画面上传 ----------------

    async def set_lock_face(self, data: bytes) -> dict[str, Any]:
        """接收上传的人脸画面：校验 → 居中裁剪为通道分辨率 → 内置模型识别 → 达标开锁。"""
        if not data:
            return {"ok": False, "error": "没有收到图片数据"}
        if len(data) > MAX_FACE_BYTES:
            return {"ok": False, "error": f"图片过大（上限 {MAX_FACE_BYTES // 1024 // 1024}MB）"}
        try:
            img = await asyncio.to_thread(_decode_face_image, data)
        except Exception as err:
            return {"ok": False, "error": f"无法识别的图片文件：{err}"}

        # 内置人脸量化模型：与登记照片 camera/face.jpg 比对，距离 ≤ 阈值才是本人
        result = await asyncio.to_thread(self.face_matcher.match, img)
        self._last_vision = {
            "detected": result.detected, "box": result.box, "distance": result.distance,
        }

        self.lock_face = img
        device = self.devices.get("lock.entry")
        payload: dict[str, Any] = {
            "ok": True,
            "facePresent": True,
            "recognitionReady": self.face_matcher.ready,
            "matched": result.matched,
            "distance": round(result.distance, 3) if result.distance is not None else None,
            "threshold": result.threshold,
            "reason": result.reason,
        }
        if device is not None:
            device.state["facePresent"] = True
            if self.face_matcher.ready:
                if result.matched:
                    device.state["lastPerson"] = result.person or LOCK_RESIDENT_NAME
                    device.state["lastResult"] = "pass"
                    device.state["locked"] = False
                elif result.detected:
                    device.state["lastResult"] = "reject"
            device.updated_at = now_ms()
            payload["locked"] = device.state["locked"]
            self._broadcast_device_state(device)
            if device.channel:
                self.hub.kick(device.channel)

        if not self.face_matcher.ready:
            self.broadcast_event({
                "level": "warn", "source": "lock.entry",
                "message": f"收到人脸画面但识别模型不可用：{self.face_matcher.reason}",
            })
        elif result.matched:
            self.broadcast_event({
                "level": "ok", "source": "lock.entry",
                "message": f"人脸通过（距离 {result.distance:.3f}），{LOCK_RESIDENT_NAME}，门锁已打开",
            })
        elif result.detected:
            self.broadcast_event({
                "level": "alarm", "source": "lock.entry",
                "message": f"人脸拒绝：不是登记住户（距离 {result.distance:.3f} > 阈值 {result.threshold:.2f}），大门保持锁定",
            })
        else:
            self.broadcast_event({
                "level": "info", "source": "lock.entry",
                "message": "大门摄像头捕捉到画面，但没有检测到正脸",
            })
        return payload

    async def clear_lock_face(self) -> dict[str, Any]:
        """移除人脸画面，大门镜头恢复为按天气切换的门外画面。"""
        self.lock_face = None
        self._last_vision = {"detected": False, "box": None, "distance": None}
        device = self.devices.get("lock.entry")
        if device is not None:
            device.state["facePresent"] = False
            if device.state.get("lastResult") in ("pass", "reject"):
                device.state["lastResult"] = "idle"
            device.updated_at = now_ms()
            self._broadcast_device_state(device)
            if device.channel:
                self.hub.kick(device.channel)
        self.broadcast_event({
            "level": "info", "source": "lock.entry",
            "message": "人脸画面已移除，大门摄像头恢复门外画面",
        })
        return {"ok": True, "facePresent": False}

    # ---------------- 摄像头 / 视频流实时识别 ----------------

    async def _loop_lock_vision(self) -> None:
        """推送中且门锁着时，每秒对实时帧做一次内置人脸识别。

        识别到登记住户 → 自动开锁；连续识别到未登记人脸 → 向前端询问
        「是否登记用户」，由面板确认后把这一帧登记成新用户。
        """
        while True:
            try:
                await asyncio.sleep(1.0)
                await self._tick_lock_vision()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                print(f"[gateway] 大门锁识别任务异常：{err!r}")

    async def _tick_lock_vision(self) -> None:
        device = self.devices.get("lock.entry")
        # 识别前置条件不成立（静态画面 / 未推送 / 已开锁 / 上传人脸模式 / 模型不可用）
        # 时，结束可能挂起的登记询问，等下一次有人脸再重新走检测流程
        context_ok = (
            device is not None and self.video_source.is_external
            and bool(device.state.get("streaming") and device.state.get("locked"))
            and self.face_matcher.ready and self.lock_face is None
        )
        if not context_ok:
            self._stop_vision_episode("stop")
            return
        frame = self.video_source.latest()
        if frame is None:
            return
        now = time.time()
        if now - self._live_scan_at < 0.9:
            return
        self._live_scan_at = now

        result = await asyncio.to_thread(self.face_matcher.match, frame)
        self._last_vision = {
            "detected": result.detected, "box": result.box, "distance": result.distance,
        }
        if device.channel:
            self.hub.kick(device.channel)

        prompt = self._enroll_prompt
        if prompt is not None and now - prompt["at"] > ENROLL_PROMPT_TTL:
            self._close_enroll_prompt("expired")

        if result.matched:
            # 登记住户回家：撤销可能挂起的询问，自动开锁
            self._stop_vision_episode("matched")
            person = result.person or LOCK_RESIDENT_NAME
            device.state["lastPerson"] = person
            device.state["lastResult"] = "pass"
            device.state["locked"] = False
            device.updated_at = now_ms()
            self._broadcast_device_state(device)
            self.broadcast_event({
                "level": "ok", "source": "lock.entry",
                "message": f"摄像头识别到登记住户 {person}（距离 {result.distance:.3f}），门锁已自动打开",
            })
            return

        if not result.detected:
            # 人脸离开画面：累计空拍，空拍够了就结束本轮（解除「忽略」冷却）
            self._unknown_run = 0
            self._unknown_miss += 1
            if self._unknown_miss >= ENROLL_GONE_MISSES:
                self._enroll_cooldown = False
                if self._enroll_prompt is not None:
                    self._close_enroll_prompt("gone")
            return

        # 检测到人脸但不在登记库：连续命中去抖后询问一次，本轮不再重复打扰
        self._unknown_miss = 0
        if self._enroll_prompt is None and not self._enroll_cooldown:
            self._unknown_run += 1
            if self._unknown_run >= UNKNOWN_PROMPT_RUNS:
                self._open_enroll_prompt(frame.copy(), result)

    # ---------------- 陌生人脸登记询问 ----------------

    def _enroll_prompt_payload(self, status: str, prompt: dict[str, Any] | None = None) -> dict[str, Any]:
        prompt = prompt if prompt is not None else self._enroll_prompt
        payload: dict[str, Any] = {"status": status}
        if prompt is not None:
            payload.update({
                "token": prompt["token"],
                "distance": round(float(prompt["distance"]), 3),
                "threshold": self.face_matcher.threshold,
                "at": int(prompt["at"] * 1000),
            })
        return payload

    def _open_enroll_prompt(self, frame: Image.Image, result: Any) -> None:
        token = f"enr-{now_ms()}-{self.seq}"
        self._enroll_prompt = {
            "token": token, "at": time.time(),
            "distance": result.distance if result.distance is not None else -1.0,
            "frame": frame, "box": result.box,
        }
        self.broadcast(envelope("faceEnroll", self._enroll_prompt_payload("ask")))
        self.broadcast_snapshot()
        self.broadcast_event({
            "level": "info", "source": "lock.entry",
            "message": (
                f"检测到未登记人脸（最近距离 {result.distance:.3f} > 阈值 "
                f"{self.face_matcher.threshold:.2f}），已提示是否登记为用户"
            ),
        })

    def _close_enroll_prompt(self, status: str) -> None:
        prompt = self._enroll_prompt
        if prompt is None:
            return
        self._enroll_prompt = None
        self.broadcast(envelope("faceEnroll", self._enroll_prompt_payload(status, prompt)))
        self.broadcast_snapshot()

    def _stop_vision_episode(self, _reason: str) -> None:
        """识别上下文失效 / 住户已识别：复位计数并撤销挂起的询问。"""
        self._unknown_run = 0
        self._unknown_miss = 0
        self._enroll_cooldown = False
        if self._enroll_prompt is not None:
            self._close_enroll_prompt("canceled")

    def _enroll_lock_user(self, token: str, name: str) -> dict[str, Any]:
        prompt = self._enroll_prompt
        if prompt is None or token != prompt["token"]:
            return {"ok": False, "error": "登记请求已失效，请在画面出现提示时再登记"}
        if time.time() - prompt["at"] > ENROLL_PROMPT_TTL:
            self._close_enroll_prompt("expired")
            return {"ok": False, "error": "登记提示已过期，请让人重新站到门前"}
        ok, reason, final_name = self.face_matcher.enroll(name, prompt["frame"], prompt["box"])
        if not ok:
            return {"ok": False, "error": reason}
        self._enroll_prompt = None
        self._enroll_cooldown = True
        self._unknown_run = 0
        self.broadcast(envelope("faceEnroll", {
            **self._enroll_prompt_payload("enrolled", prompt), "name": final_name,
        }))
        self.broadcast_snapshot()
        device = self.devices.get("lock.entry")
        if device is not None and device.channel:
            self.hub.kick(device.channel)
        self.broadcast_event({
            "level": "ok", "source": "lock.entry",
            "message": f"已登记新用户「{final_name}」，该用户后续刷脸即可自动开锁",
        })
        return {"ok": True, "name": final_name, "users": self.face_matcher.users()}

    def _dismiss_enroll_prompt(self, token: str) -> dict[str, Any]:
        prompt = self._enroll_prompt
        if prompt is None or token != prompt["token"]:
            return {"ok": True}  # 幂等：提示已不在，忽略即成功
        self._enroll_cooldown = True
        self._close_enroll_prompt("dismissed")
        return {"ok": True}

    def lock_status(self) -> dict[str, Any]:
        """大门锁扩展能力的运行状态（健康检查 / 调试用）。"""
        _gain, add, phase = self._lock_lighting()
        return {
            "fps": self.stream_fps.get("233667"),
            "phase": phase,
            "grade": {"add": round(add, 2)},
            "source": self.video_source.status().as_dict(),
            "face": {
                "ready": self.face_matcher.ready,
                "reason": self.face_matcher.reason,
                "threshold": self.face_matcher.threshold,
                "users": self.face_matcher.users(),
            },
            "enroll": self._enroll_prompt_payload("ask") if self._enroll_prompt else None,
        }

    # ---------------- 画面来源运行时切换（初始化弹窗） ----------------

    def apply_lock_source(self, kind: Any, index: Any = 0, url: Any = "") -> tuple[bool, str | None]:
        """切换大门锁画面来源：image / camera / stream。

        立即生效（推流中会自动打开或释放设备），并持久化到 sidecar JSON，
        下次启动覆盖 config.toml 里的静态配置。
        """
        kind = str(kind or "image").strip().lower()
        if kind not in ("image", "camera", "stream"):
            return False, "画面来源只能是 image / camera / stream"
        try:
            index_int = int(index)
        except (TypeError, ValueError):
            return False, "摄像头序号必须是整数"
        if not (0 <= index_int <= 15):
            return False, "摄像头序号超出范围 0~15"
        url_str = str(url or "").strip()
        if kind == "stream" and not url_str:
            return False, "选择网络视频流时必须填写流地址（rtsp:// 或 http://）"
        if kind == "stream" and not (url_str.startswith("rtsp://") or url_str.startswith("http://")
                                     or url_str.startswith("https://")):
            return False, "视频流地址只支持 rtsp:// 与 http(s)://"

        device = self.devices.get("lock.entry")
        streaming = bool(device is not None and device.state.get("streaming"))
        old = self.video_source
        old.close(timeout=0.0)  # 不阻塞 WS 处理；旧采集线程守护退出
        self.video_source = VideoSource(
            kind=kind, index=index_int, url=url_str,
            width=STREAM_WIDTH, height=STREAM_HEIGHT,
        )
        if streaming and self.video_source.is_external:
            self.video_source.open()

        self._persist_lock_source(kind, index_int, url_str)

        # 让流信令立刻带上新来源，画面也马上换底图
        self._last_signal.pop("lock.entry", None)
        device = self.devices.get("lock.entry")
        if device is not None and device.channel:
            self.hub.kick(device.channel)
            self._signal_stream(device, "lock")
        self.broadcast_snapshot()
        names = {"image": "静态天气画面", "camera": f"本机摄像头 #{index_int}", "stream": "网络视频流"}
        self.broadcast_event({
            "level": "info", "source": "lock.entry",
            "message": f"大门锁画面来源已切换为：{names[kind]}",
        })
        return True, None

    def _persist_lock_source(self, kind: str, index: int, url: str) -> bool:
        if not self.lock_source_file:
            return True
        try:
            with open(self.lock_source_file, "w", encoding="utf-8") as f:
                json.dump({"kind": kind, "index": index, "url": url}, f, ensure_ascii=False, indent=2)
            return True
        except OSError as err:
            print(f"[lock] 画面来源持久化失败：{err!r}")
            return False

    def _broadcast_device_state(self, device: Device) -> None:
        self.broadcast(envelope("state", {
            "deviceId": device.id,
            "type": device.type,
            "room": device.room,
            "state": device.state,
            "online": device.online,
        }))

    def _setup_denied(self, client: Client, msg: dict, msg_id: str, error: str) -> None:
        self._send(client, envelope("ack", {"ok": False, "error": error},
                                    id=msg_id, ref=msg.get("id")))
        self.broadcast_event({"level": "warn", "source": "setup", "message": f"镜像设置被拒绝：{error}"})

    def _on_setup_auth(self, client: Client, msg: dict, msg_id: str) -> None:
        """镜像设置第一步：只校验口令，通过后前端才展开设置卡片。"""
        payload = msg.get("payload") or {}
        if str(payload.get("password") or "") != SETUP_PASSWORD:
            self._setup_denied(client, msg, msg_id, "初始化口令不正确")
            return
        self._send(client, envelope("ack", {"ok": True}, id=msg_id, ref=msg.get("id")))

    async def _on_setup(self, client: Client, msg: dict, msg_id: str) -> None:
        payload = msg.get("payload") or {}
        if str(payload.get("password") or "") != SETUP_PASSWORD:
            self._setup_denied(client, msg, msg_id, "初始化口令不正确")
            return

        # 经纬度允许整体留空（留空表示不改动定位与天气）；只填一个则报错
        raw_lat, raw_lon = payload.get("lat"), payload.get("lon")
        lat_blank = raw_lat in (None, "")
        lon_blank = raw_lon in (None, "")
        latitude = longitude = None
        if lat_blank != lon_blank:
            self._setup_denied(client, msg, msg_id, "经纬度需同时填写，或同时留空")
            return
        if not lat_blank:
            try:
                latitude, longitude = float(raw_lat), float(raw_lon)
            except (TypeError, ValueError):
                self._setup_denied(client, msg, msg_id, "经纬度超出合法范围")
                return
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                self._setup_denied(client, msg, msg_id, "经纬度超出合法范围")
                return

        # 大门锁画面来源（image / camera / stream）：与定位相互独立，先切来源
        source_patch = payload.get("lockSource")
        if isinstance(source_patch, dict):
            ok, source_err = self.apply_lock_source(
                source_patch.get("kind"), source_patch.get("index", 0), source_patch.get("url", ""))
            if not ok:
                self._setup_denied(client, msg, msg_id, source_err)
                return

        # 名称 / 详细地址：留空则保留原值
        name = str(payload.get("name") or "").strip()[:40]
        address = str(payload.get("address") or "").strip()[:120]

        location_changed = False
        weather_label = ""
        if latitude is not None:
            try:
                weather = await self.fetch_weather(latitude, longitude)
            except Exception as err:
                self._setup_denied(client, msg, msg_id, f"天气初始化失败：{err}")
                return
            self.site = {
                "configured": True,
                "name": name or str(weather.get("name") or self.site.get("name") or "")[:40],
                "address": address or self.site.get("address") or "",
                "lat": round4(latitude),
                "lon": round4(longitude),
                "timezone": weather.get("timezone"),
                "weather": {
                    "code": weather.get("code"),
                    "label": weather.get("label"),
                    "wind": weather.get("wind"),
                    "observedAt": weather.get("observedAt"),
                    # 当日日出日落（当地本地时间），大门锁据此切昼夜画面
                    "sunrise": weather.get("sunrise"),
                    "sunset": weather.get("sunset"),
                },
                "configuredAt": now_ms(),
            }
            self.outdoor = {
                "temp": weather["temp"],
                "humidity": weather["humidity"],
                "source": "forecast",
                "place": self.site["name"],
            }
            self._seed_indoor_from_outdoor()
            location_changed = True
            weather_label = weather.get("label") or ""
        else:
            if name:
                self.site["name"] = name
            if address:
                self.site["address"] = address

        self._send(client, envelope("ack", {"ok": True, "site": self.site, "outdoor": self.outdoor},
                                    id=msg_id, ref=msg.get("id")))
        self.broadcast(envelope("setup", {"site": self.site, "outdoor": self.outdoor}))
        self.broadcast_snapshot()
        if location_changed:
            self.broadcast_event({
                "level": "ok",
                "source": "setup",
                "message": (
                    f"住宅位置已写入 {self.site['name'] or '未命名地点'}"
                    f"（{self.site['lat']}, {self.site['lon']}），"
                    f"室外 {self.outdoor['temp']:.1f}°C / {round(self.outdoor['humidity'])}% · {weather_label}"
                ),
            })
        else:
            self.broadcast_event({
                "level": "ok", "source": "setup",
                "message": f"镜像设置已保存（{self.site.get('name') or '未命名地点'}，定位未改动）",
            })

    def _seed_indoor_from_outdoor(self) -> None:
        """用室外预报给室内传感器和空调回风一个合理起点，避免定位后室内还停在默认值。"""
        base = self.outdoor["temp"]
        for device in self.devices.values():
            if device.type == "ac":
                device.state["indoorTemp"] = round1(base - (1.1 if device.floor == 2 else 0.4))
            if device.type == "sensor":
                cook = 1.4 if device.room == "kitchen" else 0
                floor_drop = 1.0 if device.floor == 2 else 0.3
                device.state["temperature"] = round1(base - floor_drop + cook)
                device.state["humidity"] = round1(clamp(
                    self.outdoor["humidity"] - (4 if device.room == "bath" else 12), 30, 90))
                device.state["comfort"] = _comfort(device.state["temperature"], device.state["humidity"])
            device.updated_at = now_ms()

    def _on_scene(self, client: Client, msg: dict, msg_id: str) -> None:
        scene_id = (msg.get("payload") or {}).get("sceneId")
        if scene_id not in SCENES:
            self._send(client, envelope("ack", {"ok": False, "error": "未知场景"}, id=msg_id))
            return
        self._apply_scene(scene_id, source="user")
        self._send(client, envelope("ack", {"ok": True, "sceneId": scene_id}, id=msg_id))

    def _apply_scene(self, scene_id: str, *, silent: bool = False, source: str = "scene") -> None:
        self.scene = scene_id

        def set_state(device_id: str, patch: dict) -> None:
            d = self.devices.get(device_id)
            if d is None:
                return
            self._apply_patch(d, patch)
            d.updated_at = now_ms()

        if scene_id == "home":
            self.occupancy = "home"
            set_state("light.living", {"power": True, "brightness": 85, "colorTemp": 3800})
            set_state("light.kitchen", {"power": True, "brightness": 70, "colorTemp": 4200})
            set_state("light.bedroom", {"power": False})
            set_state("light.bath", {"power": False})
            set_state("ac.living", {"power": True, "mode": "cool", "targetTemp": 25, "fan": "auto"})
            set_state("ac.bedroom", {"power": False})
            set_state("camera.living", {"power": True, "armed": False, "pan": 20})
            set_state("lock.entry", {"locked": False})
        elif scene_id == "away":
            self.occupancy = "away"
            for did in ("light.living", "light.kitchen", "light.bedroom", "light.bath"):
                set_state(did, {"power": False})
            set_state("ac.living", {"power": False})
            set_state("ac.bedroom", {"power": False})
            set_state("camera.living", {"power": True, "armed": True, "pan": 0})
            set_state("lock.entry", {"locked": True})
        elif scene_id == "sleep":
            self.occupancy = "sleep"
            set_state("light.living", {"power": False})
            set_state("light.kitchen", {"power": False})
            set_state("light.bedroom", {"power": True, "brightness": 8, "colorTemp": 2700})
            set_state("light.bath", {"power": False})
            set_state("ac.living", {"power": False})
            set_state("ac.bedroom", {"power": True, "mode": "cool", "targetTemp": 26, "fan": "low"})
            set_state("camera.living", {"power": True, "armed": True, "pan": 180})
            set_state("lock.entry", {"locked": True})
        elif scene_id == "movie":
            self.occupancy = "home"
            set_state("light.living", {"power": True, "brightness": 12, "colorTemp": 2700})
            set_state("light.kitchen", {"power": False})
            set_state("light.bedroom", {"power": False})
            set_state("light.bath", {"power": False})
            set_state("ac.living", {"power": True, "mode": "cool", "targetTemp": 24, "fan": "low"})
            set_state("camera.living", {"power": True, "armed": False})

        # 场景只管设备开关与云台位置，不碰推流状态：流是独立会话，由 stream 指令控制
        for d in self.devices.values():
            self._sync_stream(d)

        if not silent:
            self.broadcast(envelope("scene", {
                "sceneId": scene_id,
                "name": SCENES[scene_id]["name"],
                "occupancy": self.occupancy,
                "source": source,
            }))
            self.broadcast_snapshot()
            self.broadcast_event({
                "level": "ok", "source": "scene",
                "message": f"已切换到{SCENES[scene_id]['name']}",
            })

    # ---------------- 周期任务 ----------------

    async def _loop_telemetry(self) -> None:
        while True:
            try:
                await asyncio.sleep(2.0)
                self._tick_telemetry()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                print(f"[gateway] 遥测任务异常：{err!r}")

    async def _loop_video_signal(self) -> None:
        while True:
            try:
                await asyncio.sleep(0.2)
                self._tick_video()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                print(f"[gateway] 流信令任务异常：{err!r}")

    def _tick_telemetry(self) -> None:
        hour_bias = math.sin(time.time() / 40)   # 对应原版 sin(Date.now()/40000)
        lo, hi = (-15, 45) if self.site["configured"] else (22, 36)
        self.outdoor["temp"] = round1(clamp(self.outdoor["temp"] + (random.random() - 0.48) * 0.08, lo, hi))
        self.outdoor["humidity"] = round1(clamp(self.outdoor["humidity"] + (random.random() - 0.5) * 0.35, 15, 98))

        for device in self.devices.values():
            if device.type != "ac":
                continue
            if device.state["power"]:
                target = device.state["targetTemp"]
                mode = device.state["mode"]
                pull = 0.18 if mode == "heat" else 0.02 if mode == "fan" else 0.16
                device.state["indoorTemp"] = round1(
                    device.state["indoorTemp"] + (target - device.state["indoorTemp"]) * pull
                    + (random.random() - 0.5) * 0.05
                )
            else:
                device.state["indoorTemp"] = round1(
                    device.state["indoorTemp"] + (self.outdoor["temp"] - device.state["indoorTemp"]) * 0.03
                )

        for device in self.devices.values():
            if device.type != "sensor":
                continue
            ac = self.devices.get("ac.living") if device.room == "living" else (
                self.devices.get("ac.bedroom") if device.room == "bedroom" else None)
            base = ac.state["indoorTemp"] if ac else self.outdoor["temp"] - (1.2 if device.floor == 2 else 0.4)
            cook = 1.6 if device.room == "kitchen" else 0
            bath_light = self.devices.get("light.bath")
            steam = 8 if (device.room == "bath" and bath_light and bath_light.state["power"]) else 0
            device.state["temperature"] = round1(base + cook + hour_bias * 0.2 + (random.random() - 0.5) * 0.12)
            device.state["humidity"] = round1(clamp(
                (56 if device.room == "bath" else 46) + steam
                + (self.outdoor["humidity"] - 55) * 0.15 + (random.random() - 0.5) * 0.8,
                30, 90,
            ))
            device.state["comfort"] = _comfort(device.state["temperature"], device.state["humidity"])
            device.updated_at = now_ms()
            self.broadcast(envelope("state", {
                "deviceId": device.id, "type": device.type, "room": device.room,
                "state": device.state, "online": True,
            }))

        cam = self.devices.get("camera.living")
        if cam and cam.state["armed"] and self.occupancy == "away" and random.random() < 0.04:
            cam.state["motion"] = True
            self.broadcast(envelope("state", {
                "deviceId": cam.id, "type": cam.type, "room": cam.room,
                "state": cam.state, "online": True,
            }))
            self.broadcast_event({
                "level": "alarm", "source": cam.id,
                "message": "客厅摄像头检测到移动（布防中）",
            })
            asyncio.create_task(self._clear_motion_after(cam.id, 2.4))

    async def _clear_motion_after(self, device_id: str, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        cam = self.devices.get(device_id)
        if cam is None:
            return
        cam.state["motion"] = False
        self.broadcast(envelope("state", {
            "deviceId": cam.id, "type": cam.type, "room": cam.room,
            "state": cam.state, "online": True,
        }))

    def _tick_video(self) -> None:
        self._signal_stream(self.devices.get("camera.living"), "camera")
        self._signal_stream(self.devices.get("lock.entry"), "lock")

    def _signal_stream(self, device: Device | None, kind: str) -> None:
        """
        流信令：只在状态真的变化时推一条。

        这里没有任何像素 —— 画面请去 /?stream=<通道名> 取。
        之所以保留这条消息，是因为客户端仍需要知道「通道名 / 是否在推 / 云台角度」，
        才能决定什么时候去订阅流、以及往哪个角度调云台。
        """
        if device is None or not device.channel:
            return
        s = device.state
        if kind == "camera":
            key = f"{s['streaming']}|{s['pan']}|{s['armed']}|{s['motion']}|{s['power']}"
        else:
            src = self.video_source.status()
            key = (f"{s['streaming']}|{s['locked']}|{s['lastPerson']}|{s['lastResult']}"
                   f"|{s.get('facePresent')}|{src.kind}|{src.opened}|{src.error}")
        if self._last_signal.get(device.id) == key:
            return
        self._last_signal[device.id] = key

        info = self.hub.info(device.channel)
        self.broadcast(envelope("video", {
            "deviceId": device.id,
            "kind": kind,
            "channel": device.channel,
            "url": (info or {}).get("url") or f"/?stream={device.channel}",
            "port": (info or {}).get("port"),
            "streaming": bool(s["streaming"]),
            "live": bool((info or {}).get("live")),
            "viewers": (info or {}).get("viewers", 0),
            "fps": (info or {}).get("fps", self.hub.fps),
            "idleFps": self.hub.idle_fps,
            "pan": s.get("pan"),
            "armed": s.get("armed"),
            "motion": s.get("motion"),
            "locked": s.get("locked"),
            "person": s.get("lastPerson"),
            "result": s.get("lastResult"),
            "facePresent": bool(s.get("facePresent")),
            "source": self.video_source.status().as_dict() if kind == "lock" else None,
            "phase": self._lock_lighting()[2] if kind == "lock" else None,
            "occupancy": self.occupancy,
            "frame": now_ms(),
        }))

    # ---------------- 快照与广播 ----------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "scene": self.scene,
            "occupancy": self.occupancy,
            "outdoor": self.outdoor,
            "site": self.site,
            "devices": [
                {
                    "id": d.id, "type": d.type, "name": d.name, "room": d.room, "floor": d.floor,
                    "capabilities": list(d.capabilities),
                    "channel": d.channel or None,
                    "stream": self.hub.info(d.channel) if d.channel else None,
                    "online": d.online,
                    "updatedAt": d.updated_at,
                    "state": d.state,
                }
                for d in self.devices.values()
            ],
            "streams": self.hub.list(),
            "lock": {
                "source": self.video_source.status().as_dict(),
                "phase": self._lock_lighting()[2],
                "users": self.face_matcher.users(),
                "enroll": self._enroll_prompt_payload("ask") if self._enroll_prompt else None,
            },
            "scenes": SCENES,
            "recentEvents": self.events[:12],
        }

    def push_snapshot(self, client: Client) -> None:
        self._send(client, envelope("snapshot", self.snapshot()))

    def broadcast_snapshot(self) -> None:
        self.broadcast(envelope("snapshot", self.snapshot()))

    def broadcast_event(self, event: dict[str, Any]) -> None:
        full = {"id": f"evt-{self.seq}", "ts": now_ms(), **event}
        self.seq += 1
        self.events.insert(0, full)
        del self.events[80:]
        self.broadcast(envelope("event", full))

    def _push_event(self, client: Client, event: dict[str, Any]) -> None:
        self._send(client, envelope("event", {"id": f"evt-{self.seq}", "ts": now_ms(), **event}))
        self.seq += 1

    def broadcast(self, msg: dict[str, Any] | str) -> None:
        data = msg if isinstance(msg, str) else json_dumps(msg)
        for client in list(self.clients):
            client.push(data)

    def _send(self, client: Client, msg: dict[str, Any]) -> None:
        client.push(json_dumps(msg))

    # ---------------- 天气 ----------------

    async def fetch_weather(self, lat: float, lon: float) -> dict[str, Any]:
        """
        用公开预报接口取当前位置的实况，作为室外温度初值。
        教学点：数字孪生的「环境边界条件」应来自外部系统，而不是写死在网关里。
        """
        if self._http is None:
            raise RuntimeError("网关尚未启动")
        params = {
            "latitude": str(lat),
            "longitude": str(lon),
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
            "daily": "sunrise,sunset",
            "forecast_days": "1",
            "timezone": "auto",
        }
        res = await self._http.get("https://api.open-meteo.com/v1/forecast", params=params)
        if res.status_code != 200:
            raise RuntimeError(f"预报服务返回 {res.status_code}")
        data = res.json()
        cur = data.get("current") or {}
        temp = cur.get("temperature_2m")
        if temp is None:
            raise RuntimeError("预报数据缺少温度")
        code = cur.get("weather_code")
        daily = data.get("daily") or {}
        sunrise_list = daily.get("sunrise") or []
        sunset_list = daily.get("sunset") or []
        return {
            "temp": round1(float(temp)),
            "humidity": round1(float(cur.get("relative_humidity_2m") or 50)),
            "code": code,
            "label": WMO.get(code, f"天气码 {code}"),
            "wind": round1(float(cur.get("wind_speed_10m") or 0)),
            "timezone": data.get("timezone"),
            "observedAt": cur.get("time"),
            "isDay": cur.get("is_day"),
            "sunrise": sunrise_list[0] if sunrise_list else None,
            "sunset": sunset_list[0] if sunset_list else None,
            "name": "",
        }


def _comfort(temperature: float, humidity: float) -> str:
    if 23 <= temperature <= 27 and 40 <= humidity <= 60:
        return "舒适"
    if temperature > 28 or humidity > 70:
        return "偏闷"
    return "一般"


def _decode_face_image(data: bytes) -> Image.Image:
    """把上传的任意图片解码、转正、cover 裁剪成通道分辨率（512x320）。"""
    img = Image.open(io.BytesIO(data))
    img.load()
    # EXIF 方向：手机拍的照片常带旋转标记，不转正人脸会横着
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    img = img.convert("RGB")
    w, h = img.size
    if w < 64 or h < 64:
        raise ValueError("图片最短边不足 64px")
    return _cover_crop(img, STREAM_WIDTH, STREAM_HEIGHT)


def json_dumps(obj: Any) -> str:
    """紧凑 JSON。ensure_ascii=False 让中文在报文里保持可读（教学演示友好）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def known_device(device_id: str) -> DeviceSpec | None:
    return device_by_id(device_id)


WMO: dict[int, str] = {
    0: "晴", 1: "大部晴朗", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴",
}
