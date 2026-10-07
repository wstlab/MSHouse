#!/usr/bin/env python3
"""
MSHouse ↔ Home Assistant 桥接器（MQTT Discovery）

    MSHouse 网关 (WebSocket)  ←→  本模块  ←→  MQTT Broker  ←→  Home Assistant

设计要点
--------
1. **零侵入**：完全不改 server/ 下的任何代码。本模块只是一个普通的 WebSocket
   客户端，和 public/demo.html 地位相同 —— 这恰好验证了「网关协议是公开契约」
   这个设计：谁都能当客户端，HA 只是另一个客户端而已。
2. **双向桥**：网关推来的 snapshot / state 转成 MQTT 消息；MQTT 命令转成网关的
   command 报文。
3. **MQTT Discovery**：启动时向 homeassistant/<component>/<object_id>/config
   发 retained 配置，HA 里的实体自动出现，**不需要在 HA 里写任何 YAML**
   （摄像头是唯一例外，见文末说明）。
4. **名称翻译**：MSHouse 与 HA 在少数枚举值上叫法不同（空调的 fan / mid），
   桥接层负责翻译，不要求任何一侧妥协。
5. **可用性**：用 MQTT 遗嘱（LWT）标记桥在线状态。桥一挂，HA 里实体立刻变
   「不可用」，不会拿着过期数据做自动化。

用法
----
    # 1) 装依赖
    pip install paho-mqtt websockets

    # 2) 跑起来（MQTT broker 地址按你的实际情况改）
    python integrations/ha_bridge.py --mqtt-host 192.168.1.10

    # 3) 在 HA 里添加 MQTT 集成（指向同一个 broker），实体自动出现

环境变量也可以配置，便于塞进 systemd / Docker：
    MSHOUSE_WS / MQTT_HOST / MQTT_PORT / MQTT_USERNAME / MQTT_PASSWORD
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import ssl
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import paho.mqtt.client as mqtt
import websockets

# ------------------------------------------------------------------ #
# 物模型：直接复用项目里的唯一真源，避免房间名/场景名维护两份。
# 若本文件被单独拷走部署，则退回到内置副本。
# ------------------------------------------------------------------ #
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from server.model import DEVICE_CATALOG, ROOMS, SCENES  # noqa: E402

    ROOM_NAME: dict[str, str] = {r.id: r.name for r in ROOMS}
    SCENE_DEF: dict[str, dict[str, str]] = dict(SCENES)
    DEVICES_SRC = list(DEVICE_CATALOG)
except Exception:  # pragma: no cover - 独立部署兜底
    ROOM_NAME = {"living": "客厅", "kitchen": "厨房", "bedroom": "主卧", "bath": "卫生间", "entry": "门厅"}
    SCENE_DEF = {
        "home": {"id": "home", "name": "回家模式", "hint": ""},
        "away": {"id": "away", "name": "离家模式", "hint": ""},
        "sleep": {"id": "sleep", "name": "睡眠模式", "hint": ""},
        "movie": {"id": "movie", "name": "观影模式", "hint": ""},
    }
    DEVICES_SRC = []

LOG = logging.getLogger("ha_bridge")

# ------------------------------------------------------------------ #
# 名称翻译表
#
# 这里是整个桥接器最容易踩坑的地方：MSHouse 的空调模式用 `fan`、风速用 `mid`，
# 而 HA 的标准枚举是 `fan_only` 和 `medium`。写错的话 HA 里点一下会「看起来成功、
# 实际没生效」—— 因为网关对不认识的枚举值是静默忽略的。
# ------------------------------------------------------------------ #
AC_MODE_MS_TO_HA = {"cool": "cool", "heat": "heat", "dry": "dry", "fan": "fan_only", "auto": "auto"}
AC_MODE_HA_TO_MS = {v: k for k, v in AC_MODE_MS_TO_HA.items()}
AC_FAN_MS_TO_HA = {"low": "low", "mid": "medium", "high": "high", "auto": "auto"}
AC_FAN_HA_TO_MS = {v: k for k, v in AC_FAN_MS_TO_HA.items()}

GW_DEVICE = {
    "identifiers": ["mshouse_gateway"],
    "name": "MSHouse 网关",
    "manufacturer": "Mirror Space",
    "model": "MSHouse Gateway v1.0",
}


# ============================================================ #
# 实体定义
# ============================================================ #
@dataclass
class Entity:
    """一个 HA 实体 ↔ MSHouse 设备（或网关）之间的映射。"""

    component: str  # light / climate / sensor / switch / number / binary_sensor / lock / button
    object_id: str  # discovery 里的 object_id，同时作为 unique_id
    config: dict[str, Any]  # discovery 配置负载
    state_topic: str | None = None  # 需要桥接器主动发布状态时填
    render: Callable[[dict[str, Any]], Any] | None = None  # 设备状态 → MQTT 负载
    # 命令主题 → 把 MQTT 负载翻译成网关报文的 {action, params}
    routes: dict[str, Callable[[str], dict[str, Any] | None]] = field(default_factory=dict)


def _oid(device_id: str, suffix: str = "") -> str:
    base = "mshouse_" + device_id.replace(".", "_")
    return f"{base}_{suffix}" if suffix else base


def _device_block(spec: Any) -> dict[str, Any]:
    """HA 设备块：让同一台设备的多个实体在 HA 里归成一张卡片。"""
    block: dict[str, Any] = {
        "identifiers": [_oid(spec.id)],
        "name": spec.name,
        "manufacturer": "Mirror Space",
        "model": "MSHouse 虚拟设备",
    }
    area = ROOM_NAME.get(spec.room)
    if area:
        # HA 2023.1+：自动把实体归到对应区域，省掉手工分配
        block["suggested_area"] = area
    return block


# ---------------- 各类型的状态渲染 + 命令翻译 ---------------- #

def _render_light(s: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"state": "ON" if s.get("power") else "OFF"}
    if s.get("power"):
        out["brightness"] = int(s.get("brightness") or 0)
        out["color_temp_kelvin"] = int(s.get("colorTemp") or 4000)
    return out


def _route_light(payload: str) -> dict[str, Any] | None:
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            data = {"state": str(data)}
    except json.JSONDecodeError:
        data = {"state": payload.strip().upper()}

    state = str(data.get("state", "")).upper()
    patch: dict[str, Any] = {}
    if "brightness" in data and data["brightness"] is not None:
        patch["brightness"] = int(float(data["brightness"]))
    if "color_temp_kelvin" in data and data["color_temp_kelvin"] is not None:
        patch["colorTemp"] = int(float(data["color_temp_kelvin"]))

    if state == "OFF":
        return {"action": "off", "params": {}}
    if state == "ON" or patch:
        return {"action": "set", "params": {"power": True, **patch}}
    return None


def _render_ac(s: dict[str, Any]) -> dict[str, Any]:
    power = bool(s.get("power"))
    mode = AC_MODE_MS_TO_HA.get(str(s.get("mode")), "cool")
    return {
        "mode": mode if power else "off",
        "target_temp": float(s.get("targetTemp") or 26),
        "current_temp": float(s.get("indoorTemp") or 0),
        "fan": AC_FAN_MS_TO_HA.get(str(s.get("fan")), "auto"),
    }


def _render_sensor(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "temperature": round(float(s.get("temperature") or 0), 1),
        "humidity": round(float(s.get("humidity") or 0), 1),
        "comfort": s.get("comfort") or "未知",
    }


def _render_camera(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "streaming": bool(s.get("streaming")),
        "power": bool(s.get("power")),
        "pan": int(round(float(s.get("pan") or 0))) % 360,
        "motion": bool(s.get("motion")),
        "armed": bool(s.get("armed")),
    }


def _render_lock(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "locked": bool(s.get("locked")),
        "battery": int(s.get("battery") or 0),
        "last_person": s.get("lastPerson") or "无记录",
        "last_result": s.get("lastResult") or "",
        "streaming": bool(s.get("streaming")),
    }


def _route_lock(payload: str) -> dict[str, Any] | None:
    cmd = payload.strip().upper()
    if cmd == "LOCK":
        return {"action": "lock", "params": {}}
    if cmd == "UNLOCK":
        return {"action": "unlock", "params": {}}
    return None


def _route_face(payload: str) -> dict[str, Any] | None:
    if payload.strip().upper() == "PRESS":
        return {"action": "face", "params": {}}
    return None


def _route_scene(scene_id: str) -> Callable[[str], dict[str, Any] | None]:
    def _r(payload: str) -> dict[str, Any] | None:
        if payload.strip().upper() == "PRESS":
            return {"__scene__": scene_id}
        return None

    return _r


def build_entities(prefix: str, avail: str) -> list[Entity]:
    """按物模型生成全部 HA 实体。"""
    ents: list[Entity] = []
    common = {
        "availability_topic": avail,
        "payload_available": "online",
        "payload_not_available": "offline",
    }

    for spec in DEVICES_SRC:
        dev = _device_block(spec)
        oid = _oid(spec.id)
        st = f"{prefix}/{spec.id}/state"
        base = {**common, "device": dev}

        # ---------------- 灯 ----------------
        if spec.type == "light":
            ents.append(Entity(
                component="light",
                object_id=oid,
                state_topic=st,
                render=_render_light,
                routes={f"{prefix}/{spec.id}/set": _route_light},
                config={
                    "name": spec.name,
                    "unique_id": oid,
                    "schema": "json",
                    "state_topic": st,
                    "command_topic": f"{prefix}/{spec.id}/set",
                    "brightness": True,
                    "brightness_scale": 100,
                    # HA 2024.3+ 用开尔文；更早的版本请换成
                    # min_mireds: 154 / max_mireds: 370
                    "color_temp_kelvin": True,
                    "min_color_temp_kelvin": 2700,
                    "max_color_temp_kelvin": 6500,
                    "icon": "mdi:lightbulb",
                    **base,
                },
            ))

        # ---------------- 温湿度传感器 ----------------
        elif spec.type == "sensor":
            for suffix, name, tmpl, extra in (
                ("temperature", "温度", "{{ value_json.temperature }}",
                 {"unit_of_measurement": "°C", "device_class": "temperature",
                  "state_class": "measurement", "icon": "mdi:thermometer"}),
                ("humidity", "湿度", "{{ value_json.humidity }}",
                 {"unit_of_measurement": "%", "device_class": "humidity",
                  "state_class": "measurement", "icon": "mdi:water-percent"}),
                ("comfort", "舒适度", "{{ value_json.comfort }}",
                 {"icon": "mdi:emoticon-happy-outline"}),
            ):
                ents.append(Entity(
                    component="sensor",
                    object_id=_oid(spec.id, suffix),
                    state_topic=st,
                    render=_render_sensor,
                    config={
                        "name": f"{spec.name} {name}",
                        "unique_id": _oid(spec.id, suffix),
                        "state_topic": st,
                        "value_template": tmpl,
                        **extra,
                        **base,
                    },
                ))

        # ---------------- 空调 ----------------
        elif spec.type == "ac":
            ents.append(Entity(
                component="climate",
                object_id=oid,
                state_topic=st,
                render=_render_ac,
                routes={
                    f"{prefix}/{spec.id}/mode/set": _make_ac_mode_route(),
                    f"{prefix}/{spec.id}/temp/set": _make_ac_temp_route(),
                    f"{prefix}/{spec.id}/fan/set": _make_ac_fan_route(),
                    f"{prefix}/{spec.id}/power/set": _make_power_route(),
                },
                config={
                    "name": spec.name,
                    "unique_id": oid,
                    "modes": ["off", "cool", "heat", "dry", "fan_only", "auto"],
                    "mode_command_topic": f"{prefix}/{spec.id}/mode/set",
                    "mode_state_topic": st,
                    "mode_state_template": "{{ value_json.mode }}",
                    "temperature_command_topic": f"{prefix}/{spec.id}/temp/set",
                    "temperature_state_topic": st,
                    "temperature_state_template": "{{ value_json.target_temp }}",
                    "current_temperature_topic": st,
                    "current_temperature_template": "{{ value_json.current_temp }}",
                    "temperature_unit": "C",
                    "min_temp": 16,
                    "max_temp": 30,
                    "temp_step": 1,
                    "fan_modes": ["auto", "low", "medium", "high"],
                    "fan_mode_command_topic": f"{prefix}/{spec.id}/fan/set",
                    "fan_mode_state_topic": st,
                    "fan_mode_state_template": "{{ value_json.fan }}",
                    "power_command_topic": f"{prefix}/{spec.id}/power/set",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "icon": "mdi:air-conditioner",
                    **base,
                },
            ))

        # ---------------- 摄像头 ----------------
        # 注意：HA 的 MQTT 集成不支持 camera 实体，画面走 YAML（见 --print-camera-yaml）。
        # 这里暴露的是「能通过 MQTT 控制的那部分」：推流开关、云台角度、移动侦测。
        elif spec.type == "camera":
            ents.append(Entity(
                component="switch",
                object_id=_oid(spec.id, "stream"),
                state_topic=st,
                render=_render_camera,
                routes={f"{prefix}/{spec.id}/stream/set": _make_stream_route()},
                config={
                    "name": f"{spec.name} 推流",
                    "unique_id": _oid(spec.id, "stream"),
                    "state_topic": st,
                    "value_template": "{{ 'ON' if value_json.streaming else 'OFF' }}",
                    "command_topic": f"{prefix}/{spec.id}/stream/set",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "icon": "mdi:video",
                    **base,
                },
            ))
            ents.append(Entity(
                component="number",
                object_id=_oid(spec.id, "pan"),
                state_topic=st,
                render=_render_camera,
                routes={f"{prefix}/{spec.id}/pan/set": _make_pan_route()},
                config={
                    "name": f"{spec.name} 云台角度",
                    "unique_id": _oid(spec.id, "pan"),
                    "state_topic": st,
                    "value_template": "{{ value_json.pan }}",
                    "command_topic": f"{prefix}/{spec.id}/pan/set",
                    "min": 0,
                    "max": 355,
                    "step": 5,
                    "unit_of_measurement": "°",
                    "mode": "slider",
                    "icon": "mdi:rotate-3d-variant",
                    **base,
                },
            ))
            ents.append(Entity(
                component="binary_sensor",
                object_id=_oid(spec.id, "motion"),
                state_topic=st,
                render=_render_camera,
                config={
                    "name": f"{spec.name} 移动侦测",
                    "unique_id": _oid(spec.id, "motion"),
                    "state_topic": st,
                    "value_template": "{{ 'ON' if value_json.motion else 'OFF' }}",
                    "device_class": "motion",
                    **base,
                },
            ))

        # ---------------- 门锁 ----------------
        elif spec.type == "lock":
            ents.append(Entity(
                component="lock",
                object_id=oid,
                state_topic=st,
                render=_render_lock,
                routes={f"{prefix}/{spec.id}/set": _route_lock},
                config={
                    "name": spec.name,
                    "unique_id": oid,
                    "state_topic": st,
                    "value_template": "{{ 'LOCKED' if value_json.locked else 'UNLOCKED' }}",
                    "command_topic": f"{prefix}/{spec.id}/set",
                    "payload_lock": "LOCK",
                    "payload_unlock": "UNLOCK",
                    "state_locked": "LOCKED",
                    "state_unlocked": "UNLOCKED",
                    "optimistic": False,
                    "icon": "mdi:door-closed-lock",
                    **base,
                },
            ))
            ents.append(Entity(
                component="button",
                object_id=_oid(spec.id, "face"),
                routes={f"{prefix}/{spec.id}/face/set": _route_face},
                config={
                    "name": f"{spec.name} 人脸开锁",
                    "unique_id": _oid(spec.id, "face"),
                    "command_topic": f"{prefix}/{spec.id}/face/set",
                    "payload_press": "PRESS",
                    "icon": "mdi:face-recognition",
                    **base,
                },
            ))
            ents.append(Entity(
                component="sensor",
                object_id=_oid(spec.id, "battery"),
                state_topic=st,
                render=_render_lock,
                config={
                    "name": f"{spec.name} 电量",
                    "unique_id": _oid(spec.id, "battery"),
                    "state_topic": st,
                    "value_template": "{{ value_json.battery }}",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                    "state_class": "measurement",
                    "entity_category": "diagnostic",
                    **base,
                },
            ))
            ents.append(Entity(
                component="sensor",
                object_id=_oid(spec.id, "visitor"),
                state_topic=st,
                render=_render_lock,
                config={
                    "name": f"{spec.name} 最近访客",
                    "unique_id": _oid(spec.id, "visitor"),
                    "state_topic": st,
                    "value_template": "{{ value_json.last_person }}",
                    "icon": "mdi:account-question-outline",
                    "entity_category": "diagnostic",
                    **base,
                },
            ))

    # ---------------- 场景按钮 ----------------
    for sid, sc in SCENE_DEF.items():
        ents.append(Entity(
            component="button",
            object_id=f"mshouse_scene_{sid}",
            routes={f"{prefix}/scene/{sid}/set": _route_scene(sid)},
            config={
                "name": sc["name"],
                "unique_id": f"mshouse_scene_{sid}",
                "command_topic": f"{prefix}/scene/{sid}/set",
                "payload_press": "PRESS",
                "icon": "mdi:palette-swatch",
                **common,
                "device": GW_DEVICE,
            },
        ))

    # ---------------- 网关级实体 ----------------
    gw_state = f"{prefix}/gateway/state"
    gw_common = {**common, "device": GW_DEVICE}
    for suffix, name, tmpl, extra in (
        ("scene", "当前场景", "{{ value_json.scene_name }}", {"icon": "mdi:home-automation"}),
        ("occupancy", "在家状态", "{{ value_json.occupancy_name }}", {"icon": "mdi:account-group"}),
        ("outdoor_temp", "室外温度", "{{ value_json.outdoor_temp }}",
         {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"}),
        ("outdoor_humidity", "室外湿度", "{{ value_json.outdoor_humidity }}",
         {"unit_of_measurement": "%", "device_class": "humidity", "state_class": "measurement"}),
        ("place", "所在地", "{{ value_json.place }}", {"icon": "mdi:map-marker"}),
    ):
        ents.append(Entity(
            component="sensor",
            object_id=f"mshouse_gateway_{suffix}",
            state_topic=gw_state,
            config={
                "name": f"MSHouse {name}",
                "unique_id": f"mshouse_gateway_{suffix}",
                "state_topic": gw_state,
                "value_template": tmpl,
                **extra,
                **gw_common,
            },
        ))

    return ents


# ---------------- 空调命令路由工厂 ---------------- #

def _make_ac_mode_route() -> Callable[[str], dict[str, Any] | None]:
    def _r(payload: str) -> dict[str, Any] | None:
        mode = payload.strip().lower()
        if mode == "off":
            return {"action": "off", "params": {}}
        ms = AC_MODE_HA_TO_MS.get(mode)
        if ms is None:
            LOG.warning("未知的空调模式：%s", mode)
            return None
        return {"action": "set", "params": {"power": True, "mode": ms}}

    return _r


def _make_ac_temp_route() -> Callable[[str], dict[str, Any] | None]:
    def _r(payload: str) -> dict[str, Any] | None:
        try:
            t = float(payload.strip())
        except ValueError:
            return None
        return {"action": "set", "params": {"power": True, "targetTemp": t}}

    return _r


def _make_ac_fan_route() -> Callable[[str], dict[str, Any] | None]:
    def _r(payload: str) -> dict[str, Any] | None:
        fan = payload.strip().lower()
        ms = AC_FAN_HA_TO_MS.get(fan)
        if ms is None:
            LOG.warning("未知的风速：%s", fan)
            return None
        return {"action": "set", "params": {"fan": ms}}

    return _r


def _make_power_route() -> Callable[[str], dict[str, Any] | None]:
    def _r(payload: str) -> dict[str, Any] | None:
        cmd = payload.strip().upper()
        if cmd == "ON":
            return {"action": "on", "params": {}}
        if cmd == "OFF":
            return {"action": "off", "params": {}}
        return None

    return _r


def _make_stream_route() -> Callable[[str], dict[str, Any] | None]:
    def _r(payload: str) -> dict[str, Any] | None:
        cmd = payload.strip().upper()
        if cmd in ("ON", "OFF"):
            return {"action": "stream", "params": {"on": cmd == "ON"}}
        return None

    return _r


def _make_pan_route() -> Callable[[str], dict[str, Any] | None]:
    def _r(payload: str) -> dict[str, Any] | None:
        try:
            pan = float(payload.strip())
        except ValueError:
            return None
        return {"action": "pan", "params": {"pan": pan}}

    return _r


# ============================================================ #
# 桥接器
# ============================================================ #
class HaBridge:
    def __init__(
        self,
        ws_url: str,
        mqtt_host: str,
        mqtt_port: int = 1883,
        mqtt_username: str | None = None,
        mqtt_password: str | None = None,
        mqtt_tls: bool = False,
        prefix: str = "mshouse",
        discovery_prefix: str = "homeassistant",
    ) -> None:
        self.ws_url = ws_url
        self.prefix = prefix
        self.discovery_prefix = discovery_prefix
        self.avail_topic = f"{prefix}/bridge/status"
        self.event_topic = f"{prefix}/event"
        self.gateway_topic = f"{prefix}/gateway/state"

        self.devices: dict[str, dict[str, Any]] = {}
        self.gateway_meta: dict[str, Any] = {}
        self.entities: list[Entity] = []
        self.routes: dict[str, Callable[[str], dict[str, Any] | None]] = {}
        self._discovery_published = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._seq = 0
        self._stop = asyncio.Event()

        self._mqtt = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="mshouse-ha-bridge"
        )
        if mqtt_username:
            self._mqtt.username_pw_set(mqtt_username, mqtt_password)
        if mqtt_tls:
            self._mqtt.tls_set(cert_reqs=ssl.CERT_NONE)
            self._mqtt.tls_insecure_set(True)
        self._mqtt.on_connect = self._on_mqtt_connect
        self._mqtt.on_disconnect = self._on_mqtt_disconnect
        self._mqtt.on_message = self._on_mqtt_message
        # 遗嘱：桥异常退出时 broker 代为广播 offline
        self._mqtt.will_set(self.avail_topic, "offline", qos=1, retain=True)
        self._mqtt.connect_async(mqtt_host, mqtt_port, keepalive=30)

    # ---------------- MQTT 回调（paho 网络线程） ---------------- #

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            LOG.error("MQTT 连接失败：%s", reason_code)
            return
        LOG.info("已连接 MQTT broker")
        client.subscribe(f"{self.prefix}/#", qos=1)

    def _on_mqtt_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        LOG.warning("MQTT 断开：%s", reason_code)

    def _on_mqtt_message(self, client, userdata, message) -> None:
        topic = message.topic
        # 只处理命令主题，桥自己发的状态/事件主题一律忽略，避免回环。
        # 命令主题格式固定为 <prefix>/<deviceId>/set 或 <prefix>/<deviceId>/<sub>/set，
        # 所以设备 id 直接从主题第二段取，翻译函数不必关心设备是谁。
        if not topic.endswith("/set"):
            return
        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != self.prefix:
            return
        handler = self.routes.get(topic)
        if handler is None:
            return
        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            return
        try:
            cmd = handler(payload)
        except Exception as exc:  # 单条命令出错不该拖垮桥
            LOG.exception("命令翻译失败 %s：%s", topic, exc)
            return
        if not cmd:
            return
        if "__scene__" not in cmd:
            cmd["deviceId"] = parts[1]
        if self._loop is None or self._ws is None:
            LOG.warning("网关未连接，丢弃命令：%s", topic)
            return
        # 从 MQTT 线程投递到 asyncio 事件循环
        asyncio.run_coroutine_threadsafe(self._dispatch(cmd), self._loop)

    async def _dispatch(self, cmd: dict[str, Any]) -> None:
        scene_id = cmd.pop("__scene__", None)
        if scene_id is not None:
            msg = self._envelope("scene", {"sceneId": scene_id})
        else:
            device_id = cmd.get("deviceId")
            if not device_id:
                return
            msg = self._envelope("command", {
                "deviceId": device_id,
                "action": cmd.get("action"),
                "params": cmd.get("params") or {},
            })
        await self._ws_send(msg)

    # ---------------- 网关侧 ---------------- #

    def _envelope(self, type_: str, payload: Any) -> dict[str, Any]:
        self._seq += 1
        return {"v": "1.0", "type": type_, "id": f"ha-{self._seq}", "payload": payload}

    async def _ws_send(self, msg: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception as exc:
            LOG.warning("发送到网关失败：%s", exc)

    async def _pump(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            payload = msg.get("payload") or {}

            if kind == "snapshot":
                self._on_snapshot(payload)
            elif kind == "state":
                self._on_state(payload)
            elif kind == "ack":
                # ack 里带回设备最新状态，直接复用同一条通路
                if payload.get("deviceId") and isinstance(payload.get("state"), dict):
                    self._on_state({
                        "deviceId": payload["deviceId"],
                        "type": payload.get("type"),
                        "state": payload["state"],
                        "online": True,
                    })
            elif kind == "event":
                self._publish(self.event_topic, payload, retain=False)
            elif kind == "video":
                # 流信令（是否在推、订阅数）——并进对应设备的状态里
                dev_id = payload.get("deviceId")
                if dev_id and dev_id in self.devices:
                    self.devices[dev_id].setdefault("state", {})
                    self._publish_device(dev_id)

    def _on_snapshot(self, payload: dict[str, Any]) -> None:
        for dev in payload.get("devices") or []:
            self.devices[dev["id"]] = dev

        self.gateway_meta = {
            "scene": payload.get("scene") or "away",
            "occupancy": payload.get("occupancy") or "away",
            "outdoor": payload.get("outdoor") or {},
            "site": payload.get("site") or {},
        }

        if not self._discovery_published:
            self.entities = build_entities(self.prefix, self.avail_topic)
            for ent in self.entities:
                self.routes.update(ent.routes)
            self._publish_discovery()
            self._discovery_published = True
            LOG.info("已发布 %d 个 HA 实体的 Discovery 配置", len(self.entities))

        for dev_id in self.devices:
            self._publish_device(dev_id)
        self._publish_gateway()
        self._set_online(True)

    def _on_state(self, payload: dict[str, Any]) -> None:
        dev_id = payload.get("deviceId")
        if not dev_id:
            return
        dev = self.devices.get(dev_id)
        if dev is None:
            dev = {"id": dev_id, "type": payload.get("type"), "state": {}}
            self.devices[dev_id] = dev
        if isinstance(payload.get("state"), dict):
            dev.setdefault("state", {}).update(payload["state"])
        if "online" in payload:
            dev["online"] = payload["online"]
        self._publish_device(dev_id)

    # ---------------- 发布 ---------------- #

    def _publish(self, topic: str, payload: Any, retain: bool = True) -> None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        else:
            body = str(payload)
        self._mqtt.publish(topic, body, qos=1, retain=retain)

    def _set_online(self, online: bool) -> None:
        self._publish(self.avail_topic, "online" if online else "offline", retain=True)

    def _publish_discovery(self) -> None:
        for ent in self.entities:
            topic = f"{self.discovery_prefix}/{ent.component}/{ent.object_id}/config"
            self._publish(topic, ent.config, retain=True)

    def _publish_device(self, dev_id: str) -> None:
        dev = self.devices.get(dev_id)
        if dev is None:
            return
        state = dev.get("state") or {}
        topic = f"{self.prefix}/{dev_id}/state"
        for ent in self.entities:
            if ent.state_topic == topic and ent.render is not None:
                self._publish(topic, ent.render(state), retain=True)
                return

    def _publish_gateway(self) -> None:
        meta = self.gateway_meta
        outdoor = meta.get("outdoor") or {}
        site = meta.get("site") or {}
        scene_id = meta.get("scene") or ""
        occ = meta.get("occupancy") or ""
        scene_name = (SCENE_DEF.get(scene_id) or {}).get("name") or scene_id
        occ_name = {"home": "有人", "away": "无人", "sleep": "睡眠"}.get(occ, occ)
        self._publish(self.gateway_topic, {
            "scene": scene_id,
            "scene_name": scene_name,
            "occupancy": occ,
            "occupancy_name": occ_name,
            "outdoor_temp": round(float(outdoor.get("temp") or 0), 1),
            "outdoor_humidity": round(float(outdoor.get("humidity") or 0), 1),
            "place": site.get("name") or "未设置",
        }, retain=True)

    # ---------------- 主循环 ---------------- #

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._mqtt.loop_start()
        backoff = 3
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.ws_url, max_size=None, ping_interval=20, ping_timeout=20
                ) as ws:
                    self._ws = ws
                    LOG.info("已连接 MSHouse 网关：%s", self.ws_url)
                    self._set_online(True)
                    backoff = 3
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("网关连接中断（%s），%d 秒后重连", exc, backoff)
            finally:
                self._ws = None
                self._set_online(False)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30)

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        self._publish(self.avail_topic, "offline", retain=True)
        try:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
        except Exception:
            pass


# ============================================================ #
# 摄像头 YAML 片段（HA 的 MQTT 集成不支持 camera，只能走配置）
# ============================================================ #
def camera_yaml(base_url: str, prefix: str = "mshouse") -> str:
    return f"""# HA 的 MQTT 集成不支持 camera 实体，画面用 YAML 直连 MJPEG。
# 加到 configuration.yaml（或 packages/ 下），然后重启 HA。
camera:
  - platform: mjpeg
    name: 客厅云台摄像头
    unique_id: mshouse_camera_living
    mjpeg_url: {base_url}/?stream=233666
    still_image_url: {base_url}/?stream=233666
  - platform: mjpeg
    name: 大门人脸锁
    unique_id: mshouse_lock_entry_cam
    mjpeg_url: {base_url}/?stream=233667
    still_image_url: {base_url}/?stream=233667
"""


# ============================================================ #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="MSHouse ↔ Home Assistant 桥接器（MQTT Discovery）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--ws", default=os.environ.get("MSHOUSE_WS", "ws://127.0.0.1:8080/ws"),
                    help="MSHouse 网关 WebSocket 地址")
    ap.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "127.0.0.1"))
    ap.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--mqtt-username", default=os.environ.get("MQTT_USERNAME"))
    ap.add_argument("--mqtt-password", default=os.environ.get("MQTT_PASSWORD"))
    ap.add_argument("--mqtt-tls", action="store_true", help="启用 TLS（自签证书不校验）")
    ap.add_argument("--prefix", default=os.environ.get("MQTT_PREFIX", "mshouse"),
                    help="MQTT 主题前缀")
    ap.add_argument("--discovery-prefix", default="homeassistant",
                    help="HA 的 MQTT Discovery 前缀（HA 默认 homeassistant）")
    ap.add_argument("--print-camera-yaml", metavar="BASE_URL", default=None,
                    help="打印摄像头 YAML 片段后退出，例如 --print-camera-yaml http://192.168.1.20:8080")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.print_camera_yaml:
        print(camera_yaml(args.print_camera_yaml.rstrip("/"), args.prefix))
        return 0

    bridge = HaBridge(
        ws_url=args.ws,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        mqtt_username=args.mqtt_username,
        mqtt_password=args.mqtt_password,
        mqtt_tls=args.mqtt_tls,
        prefix=args.prefix,
        discovery_prefix=args.discovery_prefix,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _bye(*_: Any) -> None:
        LOG.info("收到退出信号，正在下线……")
        bridge.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _bye)
        except NotImplementedError:  # pragma: no cover
            pass

    try:
        loop.run_until_complete(bridge.run())
    finally:
        bridge.shutdown()
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
