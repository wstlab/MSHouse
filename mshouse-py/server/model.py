"""
MSHouse 镜像家居 · 物模型与协议常量（Mirror Space 智能家居教学场景）

教学分层：设备定义与协议格式集中在这里，网关与渲染层都只依赖本文件。

**本文件是唯一真源**：前端使用的 shared/model.js 由 scripts/export_model.py
从这里的定义生成，避免「同一份物模型维护两份」的经典漂移问题。
新增设备时：补一条 DEVICE_CATALOG，再在前端注册一个渲染器即可。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 版本号：Python 版从 1.0 起算
PROTOCOL_VERSION = "1.0"

# 握手标识
GATEWAY_NAME = "MSHouse Gateway"
GATEWAY_PROTOCOL = "mshouse/1.0"

# 初始化设置口令。教学演示用，真实项目应换成服务端哈希校验。
SETUP_PASSWORD = "villa"


class MSG:
    """报文类型常量（用类当命名空间，等价于 JS 里的 const MSG = {...}）。"""

    HELLO = "hello"
    SNAPSHOT = "snapshot"
    COMMAND = "command"
    ACK = "ack"
    STATE = "state"
    EVENT = "event"
    SCENE = "scene"
    VIDEO = "video"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"
    SETUP = "setup"
    SETUP_AUTH = "setupAuth"
    FACE_ENROLL = "faceEnroll"


@dataclass(frozen=True)
class Room:
    """房间。frozen=True 表示不可变，防止运行期被误改。"""

    id: str
    name: str
    floor: int
    zone: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "floor": self.floor, "zone": self.zone}


@dataclass(frozen=True)
class DeviceSpec:
    """设备规格（物模型里的静态部分，不含运行期 state）。"""

    id: str
    type: str
    name: str
    room: str
    floor: int
    capabilities: tuple[str, ...]
    # 视频流通道名：画面通过 HTTP 流推送，地址形如 /?stream=233666
    channel: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "room": self.room,
            "floor": self.floor,
            "capabilities": list(self.capabilities),
        }
        if self.channel is not None:
            d["channel"] = self.channel
        return d


ROOMS: tuple[Room, ...] = (
    Room("living", "客厅", 1, "公共区"),
    Room("kitchen", "厨房", 1, "公共区"),
    Room("bedroom", "主卧", 2, "私密区"),
    Room("bath", "卫生间", 2, "私密区"),
    Room("entry", "门厅", 1, "安防区"),
)


DEVICE_CATALOG: tuple[DeviceSpec, ...] = (
    DeviceSpec("light.living", "light", "客厅主灯", "living", 1, ("switch", "brightness", "colorTemp")),
    DeviceSpec("light.kitchen", "light", "厨房灯", "kitchen", 1, ("switch", "brightness", "colorTemp")),
    DeviceSpec("light.bedroom", "light", "卧室灯", "bedroom", 2, ("switch", "brightness", "colorTemp")),
    DeviceSpec("light.bath", "light", "卫生间灯", "bath", 2, ("switch", "brightness", "colorTemp")),
    DeviceSpec("sensor.living", "sensor", "客厅温湿度", "living", 1, ("telemetry",)),
    DeviceSpec("sensor.kitchen", "sensor", "厨房温湿度", "kitchen", 1, ("telemetry",)),
    DeviceSpec("sensor.bedroom", "sensor", "卧室温湿度", "bedroom", 2, ("telemetry",)),
    DeviceSpec("sensor.bath", "sensor", "卫生间温湿度", "bath", 2, ("telemetry",)),
    DeviceSpec("ac.living", "ac", "客厅空调", "living", 1, ("switch", "mode", "targetTemp", "fan")),
    DeviceSpec("ac.bedroom", "ac", "卧室空调", "bedroom", 2, ("switch", "mode", "targetTemp", "fan")),
    DeviceSpec("camera.living", "camera", "客厅云台摄像头", "living", 1, ("switch", "pan", "stream"), "233666"),
    DeviceSpec("lock.entry", "lock", "大门人脸锁", "entry", 1, ("lock", "stream"), "233667"),
)


SCENES: dict[str, dict[str, str]] = {
    "home": {"id": "home", "name": "回家模式", "hint": "开玄关与客厅灯，客厅空调舒适，摄像头待命"},
    "away": {"id": "away", "name": "离家模式", "hint": "全屋关灯关空调，上锁，摄像头布防"},
    "sleep": {"id": "sleep", "name": "睡眠模式", "hint": "只留卧室夜灯，卧室空调静音，大门上锁"},
    "movie": {"id": "movie", "name": "观影模式", "hint": "客厅调暗，厨房关闭，空调低风"},
}


def now_ms() -> int:
    """当前毫秒时间戳（与 JS Date.now() 对齐，前端用它排序事件）。"""
    return int(time.time() * 1000)


def envelope(type_: str, payload: Any = None, **extra: Any) -> dict[str, Any]:
    """协议信封。所有出站报文都过这里，保证 v / type / ts / payload 四件套齐全。"""
    msg: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": type_, "ts": now_ms()}
    msg.update(extra)
    msg["payload"] = {} if payload is None else payload
    return msg


def device_by_id(device_id: str) -> DeviceSpec | None:
    for d in DEVICE_CATALOG:
        if d.id == device_id:
            return d
    return None


def device_by_channel(channel: str) -> DeviceSpec | None:
    """按视频流通道名反查设备。"""
    key = str(channel)
    for d in DEVICE_CATALOG:
        if d.channel == key:
            return d
    return None


def room_by_id(room_id: str) -> Room | None:
    for r in ROOMS:
        if r.id == room_id:
            return r
    return None
