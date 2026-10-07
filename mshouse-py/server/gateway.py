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
import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

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
from .render.scenes import EntrySource, LivingRoomSource, StandbySource
from .stream import StreamHub

FACES = (
    {"id": "resident.lin", "name": "林晓", "role": "住户"},
    {"id": "resident.chen", "name": "陈舟", "role": "住户"},
    {"id": "guest.unknown", "name": "未登记访客", "role": "访客"},
)


def clamp(n: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, n))


def round1(n: float) -> float:
    return round(n * 10) / 10


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
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self.scene = "away"
        self.occupancy = "away"
        self.outdoor: dict[str, Any] = {"temp": 28.4, "humidity": 62, "source": "default"}
        self.site: dict[str, Any] = {
            "configured": False, "name": "", "lat": None, "lon": None,
            "timezone": None, "weather": None,
        }
        self.seq = 1
        self.events: list[dict[str, Any]] = []
        self.devices: dict[str, Device] = {}
        self._last_signal: dict[str, str] = {}
        self.hub = StreamHub(fps=8, quality=72)
        self._tasks: list[asyncio.Task] = []
        self._http: httpx.AsyncClient | None = None

        self._define_streams()
        self._init_devices()

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        """启动周期任务。必须在事件循环里调用。"""
        self._http = httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "mshouse/1.0 (teaching)"})
        self._tasks.append(asyncio.create_task(self._loop_telemetry()))
        self._tasks.append(asyncio.create_task(self._loop_video_signal()))

    async def dispose(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        self.hub.dispose()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---------------- 初始化 ----------------

    def _define_streams(self) -> None:
        """
        注册视频通道。通道名来自物模型，地址形如 /?stream=233666。
        通道存在与否和「是否在推流」是两件事：通道一直在线，推流由 stream 指令控制。
        """
        standby = StandbySource(width=512, height=320)
        cam = device_by_id("camera.living")
        lock = device_by_id("lock.entry")
        assert cam and lock

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
        })

        self.hub.define(lock.channel, {
            "name": lock.name,
            "osdName": "ENTRY DOOR LOCK",
            "deviceId": lock.id,
            "source": EntrySource(channel=lock.channel, label="ENTRY LOCK"),
            "standby": standby,
            "context": lambda: {
                "state": self.devices["lock.entry"].state if "lock.entry" in self.devices else {},
                "env": {},
            },
        })

    def _init_devices(self) -> None:
        for spec in DEVICE_CATALOG:
            self.devices[spec.id] = Device.from_spec(spec, self._default_state(spec))
        self._apply_scene("away", silent=True, source="boot")

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
            return {"locked": True, "lastPerson": None, "lastResult": "idle", "streaming": False, "battery": 86}
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
            if action == "face":
                return self._face_auth(device, params)
            if action == "stream":
                return self._apply_patch(device, {"streaming": params.get("on") is not False})

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

    def _face_auth(self, device: Device, params: dict) -> dict[str, Any]:
        face = next((f for f in FACES if f["id"] == params.get("faceId")), None)
        if face is None:
            face = FACES[random.randrange(len(FACES))]
        passed = face["role"] == "住户" and params.get("forceFail") is not True
        device.state["lastPerson"] = face["name"]
        device.state["lastResult"] = "pass" if passed else "reject"
        if passed:
            device.state["locked"] = False
        event = {
            "level": "ok" if passed else "alarm",
            "source": device.id,
            "message": (
                f"人脸通过：{face['name']}（{face['role']}），门锁已打开" if passed
                else f"人脸拒绝：{face['name']}，大门保持锁定"
            ),
        }
        return {"ok": True, "event": event}

    async def _on_setup(self, client: Client, msg: dict, msg_id: str) -> None:
        payload = msg.get("payload") or {}
        if str(payload.get("password") or "") != SETUP_PASSWORD:
            self._send(client, envelope("ack", {"ok": False, "error": "初始化口令不正确"},
                                        id=msg_id, ref=msg.get("id")))
            self.broadcast_event({"level": "warn", "source": "setup", "message": "初始化设置被拒绝：口令错误"})
            return

        try:
            latitude = float(payload.get("lat"))
            longitude = float(payload.get("lon"))
        except (TypeError, ValueError):
            self._send(client, envelope("ack", {"ok": False, "error": "经纬度超出合法范围"},
                                        id=msg_id, ref=msg.get("id")))
            return
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            self._send(client, envelope("ack", {"ok": False, "error": "经纬度超出合法范围"},
                                        id=msg_id, ref=msg.get("id")))
            return

        try:
            weather = await self.fetch_weather(latitude, longitude)
        except Exception as err:
            self._send(client, envelope("ack", {"ok": False, "error": f"天气初始化失败：{err}"},
                                        id=msg_id, ref=msg.get("id")))
            return

        self.site = {
            "configured": True,
            "name": str(payload.get("name") or weather.get("name") or "")[:40],
            "lat": round1(latitude),
            "lon": round1(longitude),
            "timezone": weather.get("timezone"),
            "weather": {
                "code": weather.get("code"),
                "label": weather.get("label"),
                "wind": weather.get("wind"),
                "observedAt": weather.get("observedAt"),
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
        self._send(client, envelope("ack", {"ok": True, "site": self.site, "outdoor": self.outdoor},
                                    id=msg_id, ref=msg.get("id")))
        self.broadcast(envelope("setup", {"site": self.site, "outdoor": self.outdoor}))
        self.broadcast_snapshot()
        self.broadcast_event({
            "level": "ok",
            "source": "setup",
            "message": (
                f"别墅定位已写入 {self.site['name'] or '未命名地点'}"
                f"（{self.site['lat']}, {self.site['lon']}），"
                f"室外 {self.outdoor['temp']:.1f}°C / {round(self.outdoor['humidity'])}% · {weather['label']}"
            ),
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
            key = f"{s['streaming']}|{s['locked']}|{s['lastPerson']}|{s['lastResult']}"
        if self._last_signal.get(device.id) == key:
            return
        self._last_signal[device.id] = key

        info = self.hub.info(device.channel)
        self.broadcast(envelope("video", {
            "deviceId": device.id,
            "kind": kind,
            "channel": device.channel,
            "url": (info or {}).get("url") or f"/?stream={device.channel}",
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
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
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
        return {
            "temp": round1(float(temp)),
            "humidity": round1(float(cur.get("relative_humidity_2m") or 50)),
            "code": code,
            "label": WMO.get(code, f"天气码 {code}"),
            "wind": round1(float(cur.get("wind_speed_10m") or 0)),
            "timezone": data.get("timezone"),
            "observedAt": cur.get("time"),
            "name": "",
        }


def _comfort(temperature: float, humidity: float) -> str:
    if 23 <= temperature <= 27 and 40 <= humidity <= 60:
        return "舒适"
    if temperature > 28 or humidity > 70:
        return "偏闷"
    return "一般"


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
