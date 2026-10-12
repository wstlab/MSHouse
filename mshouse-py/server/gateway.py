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

from . import derived, security, validate
from .media import VideoSource
from .model import (
    BATTERY,
    DEVICE_CATALOG,
    ERROR_CODES,
    FAULT_CODES,
    FAULT_LABELS,
    GATEWAY_NAME,
    GATEWAY_PROTOCOL,
    MOTION_HOLD_SECONDS,
    MOTION_MEAN_INTERVAL,
    MSG,
    OUTDOOR_REPORTING,
    PAN,
    PROTOCOL_VERSION,
    REPORTING,
    SCENES,
    STATE_KIND,
    TELEMETRY_SAMPLE_INTERVAL,
    DeviceSpec,
    behavior_for,
    device_by_id,
    envelope,
    now_ms,
    quantize,
    reporting_for,
)
from .render.scenes import EntrySource, LivingRoomSource, StandbySource, _cover_crop
from .stream import StreamHub
from .vision import FaceMatcher

# 内置识别通过时显示的人名（与登记照片 camera/face.jpg 对应）
LOCK_RESIDENT_NAME = "登记住户"

# 未认证连接允许存活的宽限期（秒）：连上后必须在这段时间内完成认证。
# 没有这条，任何人都能靠占位连接把网关的连接数吃满。
AUTH_GRACE_SECONDS = 5.0

# 服务端心跳：每 HEARTBEAT_INTERVAL 秒下发一次 ping，
# 连续 HEARTBEAT_MAX_MISSES 次没收到 pong 就判定客户端失联。
# 真实 IoT 平台靠这个清理僵尸连接，否则流通道会被死连接一直占着。
HEARTBEAT_INTERVAL = 20.0
HEARTBEAT_MAX_MISSES = 3

# 出站队列上限
OUTBOX_MAXSIZE = 256

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
    # reported：设备实际上报的状态（真实值）。v2.0 起改名为 reported，
    # 因为「设备当前是什么状态」与「我们希望它是什么状态」是两件事，
    # 混用一个 state 字段就没法表达「命令已下发、设备还没执行完」。
    reported: dict[str, Any] = field(default_factory=dict)
    # desired：应用下发的期望状态（待设备执行）。与 reported 的差集就是 delta。
    desired: dict[str, Any] = field(default_factory=dict)
    online: bool = True
    updated_at: int = 0
    # —— 链路状态 ——
    last_seen_at: int = 0
    missed_heartbeats: int = 0
    # 上次主动探测该设备的时间（离线判定用）
    last_probe_at: int = 0
    # 字段 → 上次上报的值 / 时间戳（变化上报判定用，不对外暴露）
    last_reported: dict[str, Any] = field(default_factory=dict)
    last_report_ts: dict[str, int] = field(default_factory=dict)
    # 待执行命令的定时器（模拟设备执行延迟）
    pending: list[asyncio.Task] = field(default_factory=list)
    # 当前故障：{故障码: {severity, message, since}}
    faults: dict[str, dict[str, Any]] = field(default_factory=dict)
    firmware: str = "1.0.0"
    # 环境量的缓慢漂移分量（开窗、人数变化这类慢过程）。
    # 单独存一份是为了让「真实值」与「上报值」分开：
    # 上报值 = 真实值 + 远小于上报阈值的测量噪声。
    drift: dict[str, float] = field(default_factory=dict)

    @property
    def state(self) -> dict[str, Any]:
        """
        兼容别名。

        v2.0 起 state 的正式名称是 reported。保留这个 property 是为了让
        渲染层、流信令等大量既有调用点不用全改，同时语义上明确
        「读到的是设备上报值」。新代码请直接写 reported。
        """
        return self.reported

    @classmethod
    def from_spec(cls, spec: DeviceSpec, reported: dict[str, Any]) -> "Device":
        return cls(
            id=spec.id, type=spec.type, name=spec.name, room=spec.room,
            floor=spec.floor, capabilities=spec.capabilities, channel=spec.channel,
            reported=reported, updated_at=now_ms(), last_seen_at=now_ms(),
        )

    def behavior(self) -> dict[str, Any]:
        return behavior_for(self.type)

    def delta(self) -> dict[str, Any]:
        """desired 与 reported 的差集。空字典表示设备已达成期望状态。"""
        return {k: v for k, v in self.desired.items() if self.reported.get(k) != v}

    def seed_reported(self) -> None:
        """把当前 reported 记为「已上报」，避免启动瞬间把所有字段当成变化量广播。"""
        self.last_reported = dict(self.reported)
        stamp = now_ms()
        self.last_report_ts = {k: stamp for k in self.reported}


class Client:
    """一个 WebSocket 连接。出站报文走队列，避免同步逻辑被迫 async 化。"""

    __slots__ = (
        "ws", "outbox", "pump", "authed", "auth_deadline", "out_seq",
        "rate_tokens", "rate_ts", "last_pong", "dropped", "coalesced",
        "pending_state", "resync_needed",
    )

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=OUTBOX_MAXSIZE)
        self.pump: asyncio.Task | None = None
        # 认证状态：连上不等于能用，必须先过令牌校验
        self.authed = False
        self.auth_deadline = time.monotonic() + AUTH_GRACE_SECONDS
        # 出站序号：客户端发现跳号时可请求 resync
        self.out_seq = 0
        # 命令限流（令牌桶）。初值 None 表示「尚未初始化」，
        # 首次调用时才按速率填满 —— 见 take_token 的说明。
        self.rate_tokens: float | None = None
        self.rate_ts = time.monotonic()
        # 心跳
        self.last_pong = time.monotonic()
        # 统计（供 /health 观察背压）
        self.dropped = 0
        self.coalesced = 0
        # 同设备同类报文的合并缓存：key → 最新一条报文
        self.pending_state: dict[str, str] = {}
        self.resync_needed = False

    def next_seq(self) -> int:
        self.out_seq += 1
        return self.out_seq

    def take_token(self, rate: float, burst: float | None = None) -> bool:
        """
        令牌桶限流。返回 False 表示应当拒绝本次请求。

        桶容量默认等于速率，即允许 1 秒的突发 —— 突发一小批命令是正常操作
        （比如场景切换会连发好几条），但持续高速刷命令不是。

        首次调用时把桶填满：否则连接刚建立的那一瞬间几乎什么都发不出去，
        那是限流的 bug，不是限流的效果。
        """
        now = time.monotonic()
        capacity = burst if burst is not None else rate
        if self.rate_tokens is None:
            self.rate_tokens = capacity
            self.rate_ts = now
        self.rate_tokens = min(capacity, self.rate_tokens + (now - self.rate_ts) * rate)
        self.rate_ts = now
        if self.rate_tokens >= 1.0:
            self.rate_tokens -= 1.0
            return True
        return False

    def push(self, data: str, *, coalesce_key: str | None = None) -> None:
        """
        非阻塞投递。

        与旧版的区别：队列满时**不再静默丢弃最旧报文**。静默丢包会让慢客户端
        的状态永久停在某个旧值上，而且它自己不知道 —— 排查这类问题非常痛苦。
        现在的策略是：
          ① 同类报文（同一设备的 state）先做合并，只保留最新值；
          ② 仍然放不下就标记 resync_needed，由心跳周期通知客户端全量对齐。
        """
        if coalesce_key is not None:
            # 若队列里已有同 key 的报文，就地替换（先出后进，保证顺序不变）
            previous = self.pending_state.get(coalesce_key)
            if previous is not None:
                try:
                    self.outbox.put_nowait(data)
                    self.coalesced += 1
                    self.pending_state[coalesce_key] = data
                    return
                except asyncio.QueueFull:
                    pass
            self.pending_state[coalesce_key] = data

        try:
            self.outbox.put_nowait(data)
            return
        except asyncio.QueueFull:
            pass

        # 队列满：合并失败，退化为「丢最旧 + 请求 resync」
        try:
            self.outbox.get_nowait()
            self.dropped += 1
            self.resync_needed = True
        except asyncio.QueueEmpty:
            pass
        try:
            self.outbox.put_nowait(data)
        except asyncio.QueueFull:
            self.dropped += 1
            self.resync_needed = True

    def drain_coalesced(self) -> None:
        """报文真正写出后清理合并缓存（由发送协程调用）。"""
        self.pending_state.clear()

    async def run(self) -> None:
        """发送协程：把队列里的报文写进 WebSocket。"""
        while True:
            data = await self.outbox.get()
            await self.ws.send_text(data)


class Gateway:
    def __init__(self, stream_ports: dict[str, int] | None = None,
                 stream_images: dict[str, str] | None = None,
                 stream_fps: dict[str, int] | None = None,
                 lock_options: dict[str, Any] | None = None,
                 security_config: Any = None,
                 device_config: Any = None) -> None:
        self.clients: set[Client] = set()
        # —— 鉴权与仿真配置（v2.0）——
        self.security_config = security_config
        self.device_config = device_config
        self.token: str = str(getattr(security_config, "token", "") or "")
        self.allow_anonymous: bool = bool(getattr(security_config, "allow_anonymous", False))
        self.setup_password_hash: str = str(
            getattr(security_config, "setup_password_hash", "") or "")
        self.command_rate_limit: float = float(
            getattr(device_config, "command_rate_limit", 10.0) or 10.0)
        # 同设备同字段的抖动合并窗口（毫秒）：连续拖动亮度滑块时
        # 只有最后一次会被真正执行，避免把 30 条中间值全打到设备上
        self.coalesce_window_ms: int = int(
            getattr(device_config, "coalesce_window_ms", 200) or 0)
        # 待合并的写指令：device_id → {"patch": {...}, "handle": Task}
        self._coalesce_pending: dict[str, dict[str, Any]] = {}
        # 配置里声明「保持离线」的设备（演示在线检测用）
        self._simulated_offline: set[str] = set(
            getattr(device_config, "simulate_offline", ()) or ())
        # 最近一次执行的命令时间（限流统计用）
        self._rate_limited = 0
        self._commands_total = 0
        # 默认「回家模式」：每次首页访问前端也会重新下发一次 home 场景
        self.scene = "home"
        self.occupancy = "home"
        self.outdoor: dict[str, Any] = {"temp": 28.4, "humidity": 62, "source": "default"}
        # 室外量的变化上报状态
        self._outdoor_reported: dict[str, Any] = dict(self.outdoor)
        self._outdoor_report_ts: dict[str, int] = {}
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
        self.hub = StreamHub(
            fps=8, quality=72,
            allowed_origins=tuple(getattr(security_config, "allowed_origins", ()) or ()),
        )
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
        self._http = httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "mshouse/2.0 (teaching)"})
        self._restore_state()
        self._apply_simulated_conditions()
        self._tasks.append(asyncio.create_task(self._loop_telemetry()))
        self._tasks.append(asyncio.create_task(self._loop_video_signal()))
        self._tasks.append(asyncio.create_task(self._loop_lock_vision()))
        self._tasks.append(asyncio.create_task(self._loop_heartbeat()))
        self._tasks.append(asyncio.create_task(self._loop_persistence()))
        print(f"[lock] 人脸模型：{self.face_matcher.reason}，阈值 {self.face_matcher.threshold}")

    async def dispose(self) -> None:
        self._save_state()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        for dev in self.devices.values():
            for t in dev.pending:
                t.cancel()
            dev.pending.clear()
        self.hub.dispose()
        self.video_source.close(timeout=2.0)
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---------------- 状态持久化与仿真注入 ----------------

    def _state_file(self) -> str | None:
        rel = getattr(self.device_config, "state_persist", None)
        if not rel:
            return None
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return rel if os.path.isabs(rel) else os.path.join(root, rel)

    def _restore_state(self) -> None:
        """
        从磁盘恢复设备上报值。

        真实网关重启后设备状态是「问出来的」——重新向设备查询一次。
        这里没有物理设备，所以退而求其次：把上次的 reported 存盘再读回。
        注意只恢复 reported，不恢复 desired —— 重启后应当重新对账，
        而不是把上次没执行完的期望当成既成事实。
        """
        path = self._state_file()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception as err:
            print(f"[gateway] 状态恢复失败（忽略）：{err!r}")
            return
        restored = 0
        for dev_id, values in (saved.get("devices") or {}).items():
            dev = self.devices.get(dev_id)
            if dev is None or not isinstance(values, dict):
                continue
            for key, value in values.items():
                if key in dev.reported:
                    dev.reported[key] = value
            dev.seed_reported()
            restored += 1
        if restored:
            print(f"[gateway] 已从 {path} 恢复 {restored} 台设备的上报状态")

    def _save_state(self) -> None:
        path = self._state_file()
        if not path:
            return
        try:
            payload = {
                "savedAt": now_ms(),
                "devices": {d.id: d.reported for d in self.devices.values()},
            }
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as err:
            print(f"[gateway] 状态保存失败（忽略）：{err!r}")

    async def _loop_persistence(self) -> None:
        """周期落盘。真实网关也有类似的「定期把影子刷到存储」的动作。"""
        if not self._state_file():
            return
        while True:
            try:
                await asyncio.sleep(30.0)
                self._save_state()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                print(f"[gateway] 状态落盘任务异常：{err!r}")

    def _apply_simulated_conditions(self) -> None:
        """应用配置里的仿真条件：指定设备离线、注入故障。"""
        for dev_id in getattr(self.device_config, "simulate_offline", ()) or ():
            dev = self.devices.get(dev_id)
            if dev is None:
                print(f"[gateway] 警告：simulate_offline 里的 {dev_id} 不存在")
                continue
            dev.online = False
            dev.missed_heartbeats = 99
            print(f"[gateway] 仿真：{dev_id} 保持离线")

        faults = getattr(self.device_config, "simulate_fault", None) or {}
        for dev_id, code in faults.items():
            dev = self.devices.get(dev_id)
            if dev is None:
                print(f"[gateway] 警告：simulate_fault 里的 {dev_id} 不存在")
                continue
            self.raise_fault(dev_id, code)

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
        # 用室外模型把室内初值摆到「稳态」附近。
        # 没有这一步，`_default_state` 里的 25.2°C 会和实际目标值差好几度，
        # 传感器会花好几分钟缓慢爬过去 —— 而这段时间它每 10 秒就上报一次，
        # 看起来像「上报没生效」。真实设备出厂标定过，不会有这种长瞬态。
        self._seed_indoor_from_outdoor()
        # 启动瞬间把当前值记为「已上报」。
        # 没有这一步，第一个遥测周期会把所有字段都当成「变化」全量推一遍 ——
        # 而设备刚上电时本来就该安静。
        for dev in self.devices.values():
            dev.seed_reported()

    @staticmethod
    def _default_state(spec: DeviceSpec) -> dict[str, Any]:
        if spec.type == "light":
            return {"power": False, "brightness": 0, "colorTemp": 4000}
        if spec.type == "sensor":
            # v2.0：不再有 comfort 字段 —— 那是应用层派生量，已移到 derived 命名空间。
            # battery / rssi 是真实无线传感器必然会上报的器件状态。
            return {
                "temperature": 26.8 if spec.room == "kitchen" else 25.2,
                "humidity": 58 if spec.room == "bath" else 48,
                "battery": 92,
                "rssi": -52,
            }
        if spec.type == "ac":
            return {"power": False, "mode": "cool", "targetTemp": 26, "fan": "auto", "indoorTemp": 27.5}
        if spec.type == "camera":
            # 推流是独立能力：默认关闭，等 stream 指令打开
            return {"power": True, "pan": 0, "streaming": False, "motion": False, "armed": True}
        if spec.type == "lock":
            # facePresent：镜头前是否有人脸画面（由上传接口驱动，画面从草坪道路切到人脸）
            return {"locked": True, "lastPerson": None, "lastResult": "idle",
                    "streaming": False, "battery": 86, "facePresent": False, "rssi": -61}
        return {}

    # ---------------- 连接管理 ----------------

    def attach(self, client: Client) -> None:
        """
        接入一个连接。

        注意这里不再无条件下发快照：未认证的连接只收到一条 hello
        （内含 authRequired 标记），必须先完成令牌校验才能看到设备影子。
        否则任何人连上来就能读全屋设备状态 —— 这本身就是信息泄漏。
        """
        self.clients.add(client)
        client.push(json_dumps(envelope("hello", {
            "role": "gateway",
            "name": GATEWAY_NAME,
            "protocol": GATEWAY_PROTOCOL,
            "version": PROTOCOL_VERSION,
            "authRequired": not self.allow_anonymous,
            "authed": client.authed,
            "heartbeatInterval": HEARTBEAT_INTERVAL,
        })))
        if not client.authed:
            return
        self._push_authed_greeting(client)

    def _push_authed_greeting(self, client: Client) -> None:
        """认证通过后的欢迎流程：下发全量影子 + 一条事件。"""
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
            self._send_error(client, ERROR_CODES.BAD_JSON, "报文不是合法 JSON")
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            self._send_error(client, ERROR_CODES.BAD_ENVELOPE, "缺少 type 字段")
            return

        msg_type = msg["type"]
        msg_id = msg.get("id") or f"srv-{self.seq}"
        self.seq += 1

        # —— 第一关：认证 ——
        # 未认证的连接只允许两件事：补交令牌（hello）、探活（ping）。
        # 这不是「礼貌性」的限制：设备影子包含「家里有没有人」「门锁着没有」，
        # 让任何人连上就能读到，本身就是信息泄漏。
        if not client.authed and not self.allow_anonymous:
            if msg_type == "hello":
                self._on_client_hello(client, msg, msg_id)
                return
            if msg_type == "ping":
                self._send(client, envelope("pong", {"echo": msg.get("payload")}, id=msg_id))
                return
            self._send_error(
                client, ERROR_CODES.UNAUTHORIZED,
                "连接尚未认证。请在子协议头、查询串或首帧 hello 中提供令牌", msg_id)
            self._close_later(client, 4401, "unauthorized")
            return

        # —— 第二关：协议版本 ——
        # 主版本不一致时直接拒绝。放行会让新旧客户端各读一半字段，
        # 表现为「界面上某些值永远不动」，比直接报错难查得多。
        version = msg.get("v")
        if isinstance(version, str) and version:
            if version.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
                self._send_error(
                    client, ERROR_CODES.PROTOCOL_MISMATCH,
                    f"协议主版本不匹配：服务端 {PROTOCOL_VERSION}，客户端 {version}。"
                    f"请更新客户端或改用匹配版本", msg_id)
                self._close_later(client, 4426, "protocol mismatch")
                return

        if msg_type == "command":
            self._on_command(client, msg, msg_id)
        elif msg_type == "scene":
            self._on_scene(client, msg, msg_id)
        elif msg_type == "ping":
            self._send(client, envelope("pong", {"echo": msg.get("payload")}, id=msg_id))
        elif msg_type == "pong":
            # 客户端回应了心跳 —— 这是判断对端还活着的主要依据。
            # 超时判定只看 last_pong 距今多久（见 _heartbeat_loop），无需计数器。
            client.last_pong = time.monotonic()
        elif msg_type == "snapshot":
            self.push_snapshot(client)
        elif msg_type == "resync":
            self._on_resync(client, msg, msg_id)
        elif msg_type == "ota":
            await self._on_ota(client, msg, msg_id)
        elif msg_type == "setup":
            await self._on_setup(client, msg, msg_id)
        elif msg_type == "setupAuth":
            self._on_setup_auth(client, msg, msg_id)
        else:
            self._send_error(client, ERROR_CODES.UNKNOWN_TYPE,
                             f"未识别的消息类型：{msg_type}", msg_id)

    # ---------------- 认证辅助 ----------------

    def check_access_token(self, token: str) -> bool:
        """供 main.py 在握手阶段调用（从子协议头 / 查询串提取到的令牌）。"""
        if self.allow_anonymous:
            return True
        return security.equals(token, self.token)

    def _on_client_hello(self, client: Client, msg: dict, msg_id: str) -> None:
        """首帧认证：连接建立时没带令牌的客户端，可以用 hello 补交。"""
        payload = msg.get("payload") or {}
        token = str(payload.get("token") or "")
        if not self.check_access_token(token):
            self._send_error(client, ERROR_CODES.UNAUTHORIZED, "令牌无效", msg_id)
            self._close_later(client, 4401, "unauthorized")
            return
        client.authed = True
        self._send(client, envelope("ack", {"ok": True, "authed": True}, id=msg_id))
        self._push_authed_greeting(client)

    def _close_later(self, client: Client, code: int, reason: str) -> None:
        """
        稍后关闭连接。

        为什么不直接 close：那样客户端只会看到「连接断了」，
        完全不知道是鉴权失败还是网络抖动。先把错误报文发出去、稍等一瞬
        再关，客户端才有机会把原因显示给用户。
        """
        async def closer() -> None:
            await asyncio.sleep(0.05)
            try:
                await client.ws.close(code=code, reason=reason)
            except Exception:
                pass
        asyncio.create_task(closer())

    def _on_resync(self, client: Client, msg: dict, msg_id: str) -> None:
        """
        序号对齐请求。

        客户端发现出站 seq 跳号（说明中间有报文被丢弃）后发这个请求，
        服务端直接回一份全量快照 —— 与真实 IoT 平台的做法一致：
        增量丢了就用全量对齐，不做无限历史重放（那需要服务端保存全部历史，
        而历史越长、重放越慢，反而不如一次全量来得干脆）。
        """
        payload = msg.get("payload") or {}
        client.resync_needed = False
        self._send(client, envelope("ack", {
            "ok": True, "resync": True,
            "since": payload.get("since"),
            "currentSeq": client.out_seq,
        }, id=msg_id))
        self.push_snapshot(client)

    def _send_error(self, client: Client, code: str, message: str,
                    msg_id: str | None = None,
                    details: list[dict[str, str]] | None = None) -> None:
        self._send(client, envelope(
            "error", validate.error_payload(code, message, details), id=msg_id))

    async def _on_ota(self, client: Client, msg: dict, msg_id: str) -> None:
        """
        固件升级（OTA）。

        真实设备的升级不是一个请求就完事的，而是一条状态机：
        下载 → 校验 → 安装 → 重启 → 生效，每一步都可能失败、都可能超时。
        这里实现同样的状态机（教学场景下进度是模拟的，不做真实下载）。

        为什么值得做：没有 OTA 能力的设备，出厂后就没法修 bug ——
        这在真实产品里是硬需求，也是判断「是不是真设备」的一条标准。
        """
        payload = msg.get("payload") or {}
        target = str(payload.get("target") or "")
        version = str(payload.get("version") or "").strip()

        device = self.devices.get(target)
        if device is None:
            self._send_ack(client, msg_id, msg.get("id"), {
                "ok": False,
                "errorInfo": validate.error_payload(
                    ERROR_CODES.DEVICE_NOT_FOUND, f"设备不存在：{target}"),
            })
            return
        if not version:
            self._send_ack(client, msg_id, msg.get("id"), {
                "ok": False,
                "errorInfo": validate.error_payload(
                    ERROR_CODES.INVALID_PARAM, "缺少目标固件版本号 version"),
            })
            return
        if not device.online:
            self._send_ack(client, msg_id, msg.get("id"), {
                "ok": False,
                "errorInfo": validate.error_payload(
                    ERROR_CODES.DEVICE_OFFLINE, f"{device.name} 当前离线，无法升级"),
            })
            return

        self._send_ack(client, msg_id, msg.get("id"), {
            "ok": True, "status": "ACCEPTED",
            "deviceId": target, "version": version,
            "firmware": device.firmware,
        })

        async def runner() -> None:
            stages = (
                ("downloading", 0.15), ("downloading", 0.55),
                ("verifying", 0.75), ("applying", 0.9),
                ("rebooting", 0.95), ("success", 1.0),
            )
            for state_name, progress in stages:
                await asyncio.sleep(0.8)
                self.broadcast(envelope("ota", {
                    "deviceId": target,
                    "version": version,
                    "state": state_name,
                    "progress": progress,
                }))
            device.firmware = version
            device.updated_at = now_ms()
            self._broadcast_state(device, STATE_KIND.FULL)
            self.broadcast_event({
                "level": "ok", "source": target,
                "message": f"{device.name} 固件已升级到 {version}",
            })

        asyncio.create_task(runner())

    # ---------------- 命令 ----------------

    def _on_command(self, client: Client, msg: dict, msg_id: str) -> None:
        payload = msg.get("payload") or {}
        device_id = payload.get("deviceId")
        action = payload.get("action")
        params = payload.get("params") or {}
        command_id = msg.get("id")

        # 限流。真实网关一定会限制单连接的命令速率 ——
        # 否则一个写错的 for 循环就能把设备刷到不停开关。
        if not client.take_token(self.command_rate_limit):
            self._rate_limited += 1
            self._send_ack(client, msg_id, command_id, {
                "ok": False, "deviceId": device_id, "action": action,
                "errorInfo": validate.error_payload(
                    ERROR_CODES.RATE_LIMITED,
                    f"命令过于频繁（上限 {self.command_rate_limit:g} 条/秒），请稍后重试"),
            })
            return

        device = self.devices.get(device_id)
        if device is None:
            self._send_ack(client, msg_id, command_id, {
                "ok": False, "deviceId": device_id, "action": action,
                "errorInfo": validate.error_payload(
                    ERROR_CODES.DEVICE_NOT_FOUND, f"设备不存在：{device_id}"),
            })
            return

        self._commands_total += 1

        # 离线设备不假装成功。真实网关会告知上层「设备不可达」，
        # 而不是回一个 ok 让调用方以为灯已经开了。
        if not device.online:
            self._send_ack(client, msg_id, command_id, {
                "ok": False, "deviceId": device_id, "action": action,
                "errorInfo": validate.error_payload(
                    ERROR_CODES.DEVICE_OFFLINE,
                    f"{device.name} 当前离线，命令未下发"),
                "desired": dict(device.desired),
            })
            return

        result = self._resolve_action(device, action, params)
        if not result["ok"]:
            self._send_ack(client, msg_id, command_id, {
                "ok": False, "deviceId": device_id, "action": action,
                "errorInfo": result["error"],
            })
            return

        # 立即动作：人脸登记询问面板的两个按钮。
        # 它们不是设备状态变更，而是网关自身的一次性交互，不参与执行延迟。
        if result.get("immediate"):
            outcome = result["run"]()
            self._send_ack(client, msg_id, command_id, {
                "ok": bool(outcome.get("ok")),
                "deviceId": device_id, "action": action,
                "errorInfo": (None if outcome.get("ok") else validate.error_payload(
                    ERROR_CODES.INVALID_PARAM, str(outcome.get("error") or "操作失败"))),
                "state": dict(device.reported),
            })
            return

        patch = result["patch"]
        # 写入期望态。注意这里**还没有**改 reported ——
        # 「命令已接受」与「设备已执行」是两件事，混为一谈就无法表达
        # 命令在路上、设备没响应、执行失败这些真实存在的中间态。
        device.desired.update(patch)
        delta = device.delta()

        self._send_ack(client, msg_id, command_id, {
            "ok": True,
            "status": "ACCEPTED",
            "deviceId": device_id,
            "action": action,
            "desired": dict(device.desired),
            "delta": delta,
            "stream": self.hub.info(device.channel) if device.channel else None,
        })
        self._schedule_execution(device, command_id, result.get("event"))

    def _send_ack(self, client: Client, msg_id: str, ref: str | None, body: dict[str, Any]) -> None:
        """
        构造并下发 ack。

        兼容处理：errorInfo 是结构化对象（新客户端按 code 分支），
        error 是同一内容的纯字符串（老客户端直接显示）。
        两者并存到下一个主版本；届时移除 error。
        """
        info = body.get("errorInfo")
        if info:
            body["error"] = info.get("message") or ""
        self._send(client, envelope("ack", body, id=msg_id, ref=ref))

    def _schedule_execution(self, device: Device, command_id: str | None,
                            event: dict[str, Any] | None = None) -> None:
        """
        调度「设备执行」：延迟若干毫秒后把 desired 合并进 reported。

        两个延迟来源取较大者：
          · execLatencyMs —— 模拟器件动作耗时（继电器吸合、锁舌弹出、压缩机启动）
          · coalesce_window_ms —— 抖动合并窗口

        抖动合并的意义：连续拖动亮度滑块会产生几十条命令，
        真实设备只会执行最终值。这里取消上一次未执行的定时器、重新计时，
        效果就是「只有最后一次生效」，而不是把 30 个中间值全打一遍。
        """
        previous = self._coalesce_pending.get(device.id)
        if previous and previous.get("handle"):
            previous["handle"].cancel()

        latency = float(device.behavior()["execLatencyMs"]) / 1000.0
        window = self.coalesce_window_ms / 1000.0
        delay = max(latency, window)

        async def run() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                # 被更新的命令取代：这是合并的正常路径，不是错误
                return
            self._coalesce_pending.pop(device.id, None)
            if not device.online:
                return
            self._commit_desired(device, command_id, event)

        handle = asyncio.create_task(run())
        self._coalesce_pending[device.id] = {"handle": handle}
        device.pending = [t for t in device.pending if not t.done()]
        device.pending.append(handle)

    def _commit_desired(self, device: Device, command_id: str | None,
                        event: dict[str, Any] | None = None) -> None:
        """把期望态落到上报态，并按「变化了哪些字段」广播。"""
        pending = device.delta()
        if not pending:
            # 期望值与当前值相同：不产生任何报文。
            # 旧版会照发一条 state，让客户端以为状态变了。
            return

        # 云台角度不是瞬间跳变的。真实云台转动时，位置传感器会报出
        # 若干中间角度；直接写终值会让人误以为它是「瞬移」的。
        if device.type == "camera" and "pan" in pending:
            target = float(pending.pop("pan"))
            self._start_pan_move(device, target, command_id)
            if not pending:
                return

        for key, value in pending.items():
            device.reported[key] = value
        device.updated_at = now_ms()
        # 已落地的字段从期望态里移除；未落地的（如云台角度）保留
        device.desired = {k: v for k, v in device.desired.items() if k not in pending}

        extra = self._normalize(device)
        changed = {**pending, **extra}
        self._sync_stream(device)
        self._broadcast_state(device, STATE_KIND.CONFIRMED, fields=changed, ref=command_id)
        # 有语义事件就用语义事件（「大门人脸锁 已开锁」），没有才退回通用描述 ——
        # 「客厅主灯 已执行（2 个字段生效）」这种话对使用者没有任何信息量。
        self.broadcast_event(event or {
            "level": "info", "source": device.id,
            "message": f"{device.name} 已执行（{len(changed)} 个字段生效）",
        })

    def _normalize(self, device: Device) -> dict[str, Any]:
        """
        设备内部的物理约束。

        这些不是「业务规则」而是物理事实：灯关着的时候亮度必然是 0，
        摄像头关机的时候不可能还在出画面。放在这里统一处理，
        比散落在每个动作分支里可靠。
        """
        s = device.reported
        extra: dict[str, Any] = {}
        if device.type == "light":
            if not s.get("power") and s.get("brightness"):
                s["brightness"] = 0
                extra["brightness"] = 0
            elif s.get("power") and not s.get("brightness"):
                s["brightness"] = 70
                extra["brightness"] = 70
        elif device.type == "camera":
            if not s.get("power") and s.get("streaming"):
                s["streaming"] = False
                extra["streaming"] = False
        return extra

    def _start_pan_move(self, device: Device, target: float, command_id: str | None) -> None:
        """
        云台渐进转动。

        按 PAN["speed"] 度/秒推进，取 4 个中间位置上报 ——
        既能看出「它是在转的」，又不会因为每度一条报文把链路刷爆。
        """
        start = float(device.reported.get("pan") or 0.0)
        step = float(PAN["step"])
        # 走最短弧（顺时针 / 逆时针取近的那边）
        diff = (target - start + 180.0) % 360.0 - 180.0
        if abs(diff) < step:
            device.reported["pan"] = target % 360.0
            device.desired.pop("pan", None)
            device.updated_at = now_ms()
            self._broadcast_state(device, STATE_KIND.CONFIRMED,
                                  fields={"pan": device.reported["pan"]}, ref=command_id)
            return

        waypoints = 4
        points = [start + diff * (i + 1) / waypoints for i in range(waypoints)]
        duration = abs(diff) / max(float(PAN["speed"]), 1.0)
        gap = max(duration / waypoints, 0.05)

        async def mover() -> None:
            for index, angle in enumerate(points):
                await asyncio.sleep(gap)
                value = round((angle % 360.0) / step) * step % 360.0
                device.reported["pan"] = value
                device.updated_at = now_ms()
                kind = STATE_KIND.CONFIRMED if index == len(points) - 1 else STATE_KIND.DELTA
                self._broadcast_state(device, kind, fields={"pan": value},
                                      ref=command_id if index == len(points) - 1 else None)
            device.desired.pop("pan", None)
            self._sync_stream(device)

        task = asyncio.create_task(mover())
        device.pending.append(task)

    # ---------------- 动作解析（不执行，只定稿要写什么） ----------------

    def _err(self, code: str, message: str,
             details: list[dict[str, str]] | None = None) -> dict[str, Any]:
        return {"ok": False, "error": validate.error_payload(code, message, details)}

    def _resolve_action(self, device: Device, action: str, params: dict) -> dict[str, Any]:
        """
        把动作解析成一份写指令。

        为什么不直接执行：两阶段提交要求「先校验、后执行」。
        这样非法参数在写入期望态之前就被拦下，设备不会停在
        用户没预期过的中间状态上。
        """
        spec = device_by_id(device.id)
        if spec is None:
            return self._err(ERROR_CODES.DEVICE_NOT_FOUND, f"设备不存在：{device.id}")

        s = device.reported

        if action == "set":
            if device.type == "sensor":
                return self._err(ERROR_CODES.READ_ONLY_DEVICE,
                                 f"{device.name} 是只读遥测设备，不接受写操作")
            cleaned, errors = validate.validate_patch(spec, params)
            if errors:
                return self._err(validate.error_code_for(errors),
                                 f"{device.name} 拒绝了 {len(errors)} 个字段", errors)
            if not cleaned:
                return self._err(ERROR_CODES.INVALID_PARAM, "没有需要写入的字段")
            return {"ok": True, "patch": cleaned}

        if device.type == "light":
            if "switch" not in spec.capabilities:
                return self._err(ERROR_CODES.UNSUPPORTED_CAPABILITY, "该灯具不支持开关")
            if action == "toggle":
                return {"ok": True, "patch": {
                    "power": not s.get("power"),
                    "brightness": 0 if s.get("power") else (s.get("brightness") or 80),
                }}
            if action == "on":
                level = params.get("brightness") or (s.get("brightness") or 80)
                cleaned, errors = validate.validate_patch(spec, {"brightness": level})
                if errors:
                    return self._err(ERROR_CODES.INVALID_PARAM, "亮度不合法", errors)
                return {"ok": True, "patch": {"power": True, "brightness": cleaned["brightness"]}}
            if action == "off":
                return {"ok": True, "patch": {"power": False}}

        elif device.type == "ac":
            if action == "toggle":
                return {"ok": True, "patch": {"power": not s.get("power")}}
            if action == "on":
                return {"ok": True, "patch": {"power": True}}
            if action == "off":
                return {"ok": True, "patch": {"power": False}}

        elif device.type == "camera":
            if action == "pan":
                cleaned, errors = validate.validate_patch(spec, {"pan": params.get("pan")})
                if errors:
                    return self._err(ERROR_CODES.INVALID_PARAM, "云台角度不合法", errors)
                return {"ok": True, "patch": {"pan": cleaned["pan"]}}
            if action == "nudge":
                cleaned, errors = validate.validate_patch(spec, {"delta": params.get("delta", 15)})
                if errors:
                    return self._err(ERROR_CODES.INVALID_PARAM, "云台步进量不合法", errors)
                target = (float(s.get("pan") or 0.0) + cleaned["delta"]) % 360.0
                return {"ok": True, "patch": {"pan": round(target, 1)}}
            if action == "toggle":
                return {"ok": True, "patch": {"power": not s.get("power")}}
            if action == "arm":
                if "switch" not in spec.capabilities:
                    return self._err(ERROR_CODES.UNSUPPORTED_CAPABILITY, "该摄像头不支持布防")
                return {"ok": True, "patch": {"armed": params.get("armed") is not False}}
            if action == "stream":
                return {"ok": True, "patch": {"streaming": params.get("on") is not False}}

        elif device.type == "lock":
            if action == "lock":
                return self._lock(device, True, params)
            if action == "unlock":
                return self._lock(device, False, params)
            if action == "stream":
                return {"ok": True, "patch": {"streaming": params.get("on") is not False}}
            # 陌生人脸登记询问面板的两个动作：网关自身的交互，不经过设备延迟
            if action == "enrollFace":
                token = str(params.get("token") or "")
                name = str(params.get("name") or "")
                return {"ok": True, "immediate": True,
                        "run": lambda: self._enroll_lock_user(token, name)}
            if action == "dismissEnroll":
                token = str(params.get("token") or "")
                return {"ok": True, "immediate": True,
                        "run": lambda: self._dismiss_enroll_prompt(token)}

        return self._err(ERROR_CODES.ACTION_NOT_SUPPORTED,
                         f"动作 {action} 不适用于 {device.type}")

    def _apply_patch(self, device: Device, patch: dict) -> dict[str, Any]:
        """
        内部写路径（场景联动使用）。

        与用户命令的区别：场景是网关自己发起的一批变更，
        不需要走「期望态 → 延迟 → 确认」，直接改上报态即可。
        但仍要经过 _normalize 处理物理约束，并返回本次真正变化的字段。
        """
        if device.type == "sensor":
            return {"ok": False, "error": validate.error_payload(
                ERROR_CODES.READ_ONLY_DEVICE, "传感器为只读遥测设备")}

        changed: dict[str, Any] = {}
        for key, value in patch.items():
            if key == "locked":
                device.reported["locked"] = bool(value)
                device.reported["lastResult"] = "locked" if value else "unlocked"
                changed["locked"] = device.reported["locked"]
                continue
            if key not in device.reported:
                # 未声明字段：内部调用也应立即暴露问题，而不是悄悄写进去
                print(f"[gateway] 警告：场景写入 {device.id} 的未知字段 {key}")
                continue
            device.reported[key] = value
            changed[key] = value

        extra = self._normalize(device)
        changed.update(extra)
        device.updated_at = now_ms()
        return {"ok": True, "changed": changed, "event": {
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
        """
        门锁开关。

        返回 patch 而不是直接改状态 —— 锁是有物理动作时间的器件（锁舌要动），
        所以它和其它设备一样走「接受 → 执行 → 确认」的两阶段流程。
        事件也一并带出来，由提交阶段广播：这样事件描述的是「门真的锁上了」，
        而不是「网关收到了锁门请求」。
        """
        patch: dict[str, Any] = {
            "locked": locked,
            "lastResult": "locked" if locked else "unlocked",
        }
        person = params.get("person")
        if person:
            patch["lastPerson"] = str(person)
        return {
            "ok": True,
            "patch": patch,
            "event": {
                "level": "warn" if locked else "ok",
                "source": device.id,
                "message": (f"{device.name} 已上锁" if locked else f"{device.name} 已开锁")
                           + (f" · {person}" if person else ""),
            },
        }

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

    def _broadcast_state(self, device: Device, kind: str,
                         fields: dict[str, Any] | None = None,
                         ref: str | None = None) -> None:
        """
        广播一条 state 报文。

        fields 为 None 时发全量 reported，否则只发指定字段（增量语义）。
        每个客户端有独立的出站序号 seq —— 客户端据此发现自己漏收了报文，
        进而发起 resync，而不是默默显示一个过期的值。
        """
        body: dict[str, Any] = {
            "deviceId": device.id,
            "type": device.type,
            "room": device.room,
            "state": dict(device.reported) if fields is None else dict(fields),
            "stateKind": kind,
            "online": device.online,
            "updatedAt": device.updated_at,
        }
        if ref:
            body["ref"] = ref
        # 传感器的派生量只在温湿度**确实上报了**的时候附带。
        # 若这次只是电量变化，再带一份 THI 就是冗余 ——
        # 真实设备也不会因为电量变了就重算一遍环境舒适度。
        if device.type == "sensor":
            touched = fields is None or any(
                key in fields for key in ("temperature", "humidity"))
            if touched:
                body["derived"] = derived.sensor_derived(
                    float(device.reported.get("temperature") or 0.0),
                    float(device.reported.get("humidity") or 0.0),
                    at=device.updated_at,
                )
        for client in list(self.clients):
            if not client.authed:
                continue
            payload = dict(body)
            payload["seq"] = client.next_seq()
            client.push(json_dumps(envelope("state", payload)),
                        coalesce_key=f"state:{device.id}")

    def _broadcast_device_state(self, device: Device) -> None:
        """全量广播某设备状态（供既有的非命令路径调用）。"""
        self._broadcast_state(device, STATE_KIND.FULL)

    def _setup_denied(self, client: Client, msg: dict, msg_id: str, error: str) -> None:
        self._send_ack(client, msg_id, msg.get("id"), {
            "ok": False,
            "errorInfo": validate.error_payload(ERROR_CODES.SETUP_DENIED, error),
        })
        self.broadcast_event({"level": "warn", "source": "setup", "message": f"镜像设置被拒绝：{error}"})

    def _check_setup_password(self, value: Any) -> bool:
        """
        校验初始化口令。

        只比对哈希，且用常数时间比较。口令明文不落任何文件、
        也不进前端资源 —— 这是 v2.0 相对 1.0 最要紧的一处修正。
        """
        return security.verify_password(str(value or ""), self.setup_password_hash)

    def _on_setup_auth(self, client: Client, msg: dict, msg_id: str) -> None:
        """镜像设置第一步：只校验口令，通过后前端才展开设置卡片。"""
        payload = msg.get("payload") or {}
        if not self._check_setup_password(payload.get("password")):
            self._setup_denied(client, msg, msg_id, "初始化口令不正确")
            return
        self._send(client, envelope("ack", {"ok": True}, id=msg_id, ref=msg.get("id")))

    async def _on_setup(self, client: Client, msg: dict, msg_id: str) -> None:
        payload = msg.get("payload") or {}
        if not self._check_setup_password(payload.get("password")):
            self._setup_denied(client, msg, msg_id, "初始化口令不正确")
            return

        # 经纬度允许整体留空（留空表示不改动定位与天气）；只填一个则报错
        raw_lat, raw_lon = payload.get("lat"), payload.get("lon")
        lat_blank = raw_lat in (None, "")
        lon_blank = raw_lon in (None, "")
        if lat_blank != lon_blank:
            self._setup_denied(client, msg, msg_id, "经纬度需同时填写，或同时留空")
            return
        latitude, longitude, errors = validate.validate_coordinates(raw_lat, raw_lon)
        if errors:
            self._setup_denied(client, msg, msg_id, errors[0]["reason"])
            return
        if lat_blank:
            latitude = longitude = None

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
        """
        用室外预报给室内传感器和空调回风一个合理起点，
        避免定位完成后室内还停在默认值上。

        注意这里不再计算 comfort：派生量由 _broadcast_state 在发包时统一算，
        存进设备状态会导致「同一个值在多处维护」，早晚不一致。
        """
        base = self.outdoor["temp"]
        for device in self.devices.values():
            if device.type == "ac":
                device.reported["indoorTemp"] = round1(base - (1.1 if device.floor == 2 else 0.4))
            if device.type == "sensor":
                cook = 1.4 if device.room == "kitchen" else 0
                floor_drop = 1.0 if device.floor == 2 else 0.3
                device.reported["temperature"] = quantize(
                    "sensor", "temperature", round1(base - floor_drop + cook))
                device.reported["humidity"] = quantize(
                    "sensor", "humidity",
                    round1(clamp(self.outdoor["humidity"] - (4 if device.room == "bath" else 12), 30, 90)))
            device.updated_at = now_ms()
        # 定位变更相当于设备「重新测量了一次」：把当前值记为已上报，
        # 让接下来的变化上报从新基线开始算，而不是把整批变化量一次性推出去。
        for device in self.devices.values():
            device.seed_reported()

    def _on_scene(self, client: Client, msg: dict, msg_id: str) -> None:
        scene_id = (msg.get("payload") or {}).get("sceneId")
        if scene_id not in SCENES:
            self._send_ack(client, msg_id, msg.get("id"), {
                "ok": False,
                "errorInfo": validate.error_payload(
                    ERROR_CODES.INVALID_PARAM,
                    f"未知场景：{scene_id}（可用：{'/'.join(SCENES)}）"),
            })
            return
        self._apply_scene(scene_id, source="user")
        self._send(client, envelope("ack", {"ok": True, "sceneId": scene_id}, id=msg_id))

    def _apply_scene(self, scene_id: str, *, silent: bool = False, source: str = "scene") -> None:
        """
        应用场景。

        与旧版的关键区别：不再在末尾无脑 broadcast_snapshot()。
        全量快照会把十几台设备的状态一次性推给所有客户端，而场景往往
        只改了其中几台 —— 这是典型的「广播放大」。现在只对真正变化的设备
        发增量报文，没变化的设备一条都不发。

        另外，场景里的云台角度按「预置位」处理：直接定位，不走渐进转动
        （真实云台切换预置位也是快速定位，而不是慢慢转过去）。
        """
        self.scene = scene_id
        changed: dict[str, dict[str, Any]] = {}

        def set_state(device_id: str, patch: dict) -> None:
            d = self.devices.get(device_id)
            if d is None:
                return
            result = self._apply_patch(d, patch)
            if not result.get("ok"):
                return
            fields = result.get("changed") or {}
            if fields:
                changed[device_id] = {**changed.get(device_id, {}), **fields}

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

        if silent:
            return

        for device_id, fields in changed.items():
            device = self.devices.get(device_id)
            if device is not None:
                self._broadcast_state(device, STATE_KIND.CONFIRMED, fields=fields)

        self.broadcast(envelope("scene", {
            "sceneId": scene_id,
            "name": SCENES[scene_id]["name"],
            "occupancy": self.occupancy,
            "source": source,
            "changed": {k: list(v.keys()) for k, v in changed.items()},
        }))
        self.broadcast_event({
            "level": "ok", "source": "scene",
            "message": f"已切换到{SCENES[scene_id]['name']}（{len(changed)} 台设备状态更新）",
        })

    # ---------------- 周期任务 ----------------

    async def _loop_telemetry(self) -> None:
        """
        遥测采样循环。

        周期是**采样**周期，不是上报周期 —— 这两件事在真实设备里是分开的：
        传感器内部可能每秒量一次，但只在变化超过阈值时才发报文。
        旧版把两者混为一谈（每 2 秒采样并**无条件**广播），
        结果是稳态下恒定 2 条/秒的无效流量、约 28% 的内容是重复值。
        """
        while True:
            try:
                await asyncio.sleep(TELEMETRY_SAMPLE_INTERVAL)
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

    async def _loop_heartbeat(self) -> None:
        """服务端心跳：定期 ping，连续多次没回 pong 就断开该客户端。"""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                self._tick_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                print(f"[gateway] 心跳任务异常：{err!r}")

    def _tick_heartbeat(self) -> None:
        """
        心跳与失联清理。

        旧版只有客户端主动 ping、服务端从不主动发，于是「对端还活着吗」
        这个问题永远得不到答案：断网的客户端会一直占着流通道，
        直到 TCP 超时（可能十几分钟）。
        """
        now = time.monotonic()
        for client in list(self.clients):
            if not client.authed:
                # 未认证连接的宽限期由 main.py 的收包超时负责
                continue
            idle = now - client.last_pong
            if idle > HEARTBEAT_INTERVAL * HEARTBEAT_MAX_MISSES:
                print(f"[gateway] 客户端心跳超时（{idle:.0f}s 未响应），断开")
                self._close_later(client, 4408, "heartbeat timeout")
                self.detach(client)
                continue
            self._send(client, envelope("ping", {"interval": HEARTBEAT_INTERVAL}))

    # ---------------- 上报判定 ----------------

    def _pick_changed(self, current: dict[str, Any], plan: dict[str, dict[str, float]],
                      last_value: dict[str, Any], last_ts: dict[str, int]) -> dict[str, Any]:
        """
        挑出「本次该上报的字段」。

        判定规则与真实无线设备一致（对应 Zigbee 的三个上报参数）：
          ① 变化量 >= reportableChange → 上报
          ② 距上次上报 >= maxInterval  → 保活上报（即使没变化）
          两者都受 minInterval 抑制（防止抖动导致高频上报）。

        不满足条件的字段**不进入报文** —— 而不是像旧版那样把没变的旧值
        再发一遍。这一点是「真实感」和「流量」两个指标同时改善的关键。
        """
        now = now_ms()
        picked: dict[str, Any] = {}
        for name, rule in plan.items():
            if name not in current:
                continue
            since_ms = now - last_ts.get(name, 0)
            if since_ms < rule["minInterval"] * 1000:
                continue
            previous = last_value.get(name)
            changed = (previous is None
                       or abs(float(current[name]) - float(previous)) >= rule["reportableChange"])
            keepalive = since_ms >= rule["maxInterval"] * 1000
            if changed or keepalive:
                picked[name] = current[name]
                last_value[name] = current[name]
                last_ts[name] = now
        return picked

    def _tick_telemetry(self) -> None:
        self._sample_outdoor()
        self._sample_devices()
        self._flush_telemetry()
        self._maybe_motion()
        self._tick_device_heartbeat()

    def _sample_outdoor(self) -> None:
        """推进室外环境模型。它只在 snapshot / setup 里下发，不做高频广播。"""
        lo, hi = (-15, 45) if self.site["configured"] else (22, 36)
        self.outdoor["temp"] = round1(clamp(
            self.outdoor["temp"] + (random.random() - 0.48) * 0.08, lo, hi))
        self.outdoor["humidity"] = round1(clamp(
            self.outdoor["humidity"] + (random.random() - 0.5) * 0.35, 15, 98))

    def _sample_devices(self) -> None:
        """推进设备物理模型（只改内存值，是否上报由 _flush_telemetry 决定）。"""
        hour_bias = math.sin(time.time() / 40)
        for device in self.devices.values():
            if device.type == "ac":
                self._sample_ac(device)
            elif device.type == "sensor":
                self._sample_sensor(device, hour_bias)

    def _sample_ac(self, device: Device) -> None:
        """空调回风温度模型：开机时向设定温度靠拢，关机时向室外温度漂移。"""
        s = device.reported
        if s["power"]:
            target = float(s["targetTemp"])
            mode = s["mode"]
            pull = 0.18 if mode == "heat" else 0.02 if mode == "fan" else 0.16
            s["indoorTemp"] = round1(
                float(s["indoorTemp"]) + (target - float(s["indoorTemp"])) * pull
                + (random.random() - 0.5) * 0.05)
        else:
            # 关机后的自然回温很慢（房间有热容），不是一秒逼近室外。
            # 用 0.006/s 而不是 0.03/s：后者会让室温在 30 秒内就追上室外，
            # 于是传感器一路「合法地」高频上报，掩盖了上报阈值的效果。
            s["indoorTemp"] = round1(
                float(s["indoorTemp"])
                + (self.outdoor["temp"] - float(s["indoorTemp"])) * 0.006)

    def _sample_sensor(self, device: Device, hour_bias: float) -> None:
        """
        温湿度模型 + 器件特征。

        与旧版相比多了四样真实器件一定会有的东西：
          · 量化到分辨率（温度 0.1°C、湿度 1%）—— 真实传感器不会报 47.23%
          · 电量缓慢下降（不是恒定 86%）
          · 无线信号强度波动（rssi）
          · **一阶滞后 + 亚阈值噪声** —— 读数按比例逼近目标值，不直接赋值

        最后一条最要紧，也是最容易被忽略的：真实传感器的测量噪声一定
        **小于**上报阈值（ReportableChange），否则设备会一直触发上报、
        几天就把电池耗光 —— 这正是 Zigbee 设备把 ReportableChange
        设在噪声之上的原因。旧版直接赋值 + 大噪声，「变化上报」形同虚设。
        """
        s = device.reported
        ac = self.devices.get("ac.living") if device.room == "living" else (
            self.devices.get("ac.bedroom") if device.room == "bedroom" else None)
        base = (float(ac.reported["indoorTemp"]) if ac
                else self.outdoor["temp"] - (1.2 if device.floor == 2 else 0.4))
        cook = 1.6 if device.room == "kitchen" else 0
        bath_light = self.devices.get("light.bath")
        steam = 8 if (device.room == "bath" and bath_light
                      and bath_light.reported["power"]) else 0

        # 缓慢漂移：模拟开窗、人数变化等慢过程（分钟级），
        # 让「真实值」自己会走，而不是靠噪声去凑变化。
        drift = device.drift
        for key, rate, span in (("temperature", 0.10, 1.4), ("humidity", 0.9, 9.0)):
            step = (random.random() - 0.5) * rate * TELEMETRY_SAMPLE_INTERVAL / 60.0
            drift[key] = clamp(drift.get(key, 0.0) + step, -span, span)

        target_t = base + cook + hour_bias * 0.2 + drift["temperature"]
        cur_t = float(s.get("temperature") or target_t)
        # 一阶滞后：房间有热惯量。时间常数取 ~65s（0.015/s），
        # 10 秒最多走完落差的 14% —— 于是正常情况下「10 秒内变化 < 0.2°C」，
        # 上报阈值才真正起到过滤作用；而空调一开、落差拉大时又会如实上报。
        # 噪声 0.05 远小于阈值 0.2，模拟「测量噪声必须低于上报阈值」这一器件事实。
        s["temperature"] = quantize(
            "sensor", "temperature",
            cur_t + (target_t - cur_t) * 0.015 + (random.random() - 0.5) * 0.05)

        target_h = clamp((56 if device.room == "bath" else 46) + steam
                         + (self.outdoor["humidity"] - 55) * 0.15
                         + drift["humidity"], 30, 90)
        cur_h = float(s.get("humidity") or target_h)
        # 湿度噪声 0.25 < 阈值 1.0；量化到 1% 后基本不会越过阈值
        s["humidity"] = quantize(
            "sensor", "humidity",
            cur_h + (target_h - cur_h) * 0.012 + (random.random() - 0.5) * 0.25)

        # 电量按「每天 0.1%」的速率放电（纽扣电池 + 低频上报的真实量级）
        drain = BATTERY["drainPerDay"] * TELEMETRY_SAMPLE_INTERVAL / 86400.0
        s["battery"] = round(max(0.0, float(s.get("battery") or 100) - drain), 1)
        if float(s["battery"]) <= BATTERY["lowThreshold"]:
            self.raise_fault(device.id, FAULT_CODES.LOW_BATTERY)

        # 无线链路质量：小幅波动，模拟真实射频环境
        s["rssi"] = int(clamp(float(s.get("rssi") or -55)
                              + (random.random() - 0.5) * 2, -95, -30))

    def _flush_telemetry(self) -> None:
        """按上报参数筛选后广播。没有字段需要上报的设备，一条报文都不发。"""
        for device in self.devices.values():
            plan = REPORTING.get(device.type)
            if not plan:
                continue
            picked = self._pick_changed(device.reported, plan,
                                        device.last_reported, device.last_report_ts)
            if not picked:
                continue
            device.updated_at = now_ms()
            device.last_seen_at = device.updated_at
            self._broadcast_state(device, STATE_KIND.DELTA, fields=picked)

    def _maybe_motion(self) -> None:
        """
        移动侦测。

        旧版是每 2 秒抛一次 4% 的骰子、命中后保持 2.4 秒 —— 那既不是
        「侦测」（没有触发源）也不是「保持」（时间短得不像 PIR）。
        现在改为事件驱动：离家布防状态下，按「平均几分钟有人经过」的
        频率产生事件，触发后保持 MOTION_HOLD_SECONDS 秒才复位。
        """
        cam = self.devices.get("camera.living")
        if cam is None or not cam.reported.get("armed") or cam.reported.get("motion"):
            return
        if self.occupancy != "away":
            return
        # 每秒触发概率 = 1 / 平均间隔，等价于泊松过程
        probability = TELEMETRY_SAMPLE_INTERVAL / max(MOTION_MEAN_INTERVAL, 1.0)
        if random.random() >= probability:
            return

        cam.reported["motion"] = True
        cam.updated_at = now_ms()
        self._broadcast_state(cam, STATE_KIND.DELTA, fields={"motion": True})
        self.broadcast_event({
            "level": "alarm", "source": cam.id,
            "message": "客厅摄像头检测到移动（布防中）",
            "detectionType": "pir", "confidence": 0.92,
        })
        asyncio.create_task(self._clear_motion_after(cam.id, MOTION_HOLD_SECONDS))

    async def _clear_motion_after(self, device_id: str, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        cam = self.devices.get(device_id)
        if cam is None or not cam.reported.get("motion"):
            return
        cam.reported["motion"] = False
        cam.updated_at = now_ms()
        self._broadcast_state(cam, STATE_KIND.DELTA, fields={"motion": False})

    # ---------------- 在线检测 ----------------

    def _tick_device_heartbeat(self) -> None:
        """
        设备心跳探测。

        旧版把 online 硬编码为 True，全项目没有一处改它 ——
        设备影子于是退化成内存变量：设备拔了电，界面还显示在线。
        现在每个设备按自己的心跳周期被探测，连续 offlineThreshold 次
        没有回应就判定离线，并广播一次状态变化。
        """
        now = now_ms()
        for device in self.devices.values():
            behavior = device.behavior()
            period = behavior["heartbeatInterval"] * 1000
            if now - device.last_probe_at < period:
                continue
            device.last_probe_at = now

            if device.id in self._simulated_offline:
                # 仿真：该设备不回应探测
                device.missed_heartbeats += 1
            else:
                device.missed_heartbeats = 0
                device.last_seen_at = now

            limit = behavior["offlineThreshold"]
            if device.online and device.missed_heartbeats >= limit:
                self._set_online(device, False, reason="心跳超时")
            elif not device.online and device.missed_heartbeats == 0:
                self._set_online(device, True, reason="心跳恢复")

    def _set_online(self, device: Device, online: bool, *, reason: str = "") -> None:
        """切换在线状态并广播。只在状态**真的变化**时才产生报文。"""
        if device.online == online:
            return
        device.online = online
        device.updated_at = now_ms()
        self._broadcast_state(device, STATE_KIND.FULL)
        self.broadcast_event({
            "level": "ok" if online else "warn",
            "source": device.id,
            "message": (f"{device.name} 已恢复在线" if online else f"{device.name} 已离线")
                       + (f"（{reason}）" if reason else ""),
        })

    def raise_fault(self, device_id: str, code: str) -> None:
        """
        上报设备故障。

        真实设备的故障也是走事件通道推给上层平台的，
        而不是等平台来轮询 —— 轮询查不出「刚才闪过一次故障」。
        """
        device = self.devices.get(device_id)
        if device is None or code in device.faults:
            return
        severity, label = FAULT_LABELS.get(code, ("warn", code))
        info = {"code": code, "severity": severity, "message": label, "since": now_ms()}
        device.faults[code] = info
        self.broadcast(envelope(MSG.FAULT, {
            "deviceId": device_id,
            "faultCode": code,
            "severity": severity,
            "message": label,
            "since": info["since"],
        }))
        self.broadcast_event({
            "level": severity, "source": device_id, "message": f"故障：{label}",
        })

    def clear_fault(self, device_id: str, code: str | None = None) -> None:
        """清除故障（故障恢复）。"""
        device = self.devices.get(device_id)
        if device is None:
            return
        removed = [code] if code else list(device.faults)
        for key in removed:
            if device.faults.pop(key, None) is not None:
                self.broadcast_event({
                    "level": "ok", "source": device_id,
                    "message": f"故障已恢复：{FAULT_LABELS.get(key, ('', key))[1]}",
                })

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
            "devices": [self._device_snapshot(d) for d in self.devices.values()],
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

    def _device_snapshot(self, d: Device) -> dict[str, Any]:
        """
        单台设备的快照条目。

        state 里放的是**设备上报值**（reported）；传感器另附 derived。
        这里刻意不把派生量塞进 state —— 全量快照和增量报文的结构必须一致，
        否则客户端得为两条路径各写一套解析逻辑。
        """
        item: dict[str, Any] = {
            "id": d.id, "type": d.type, "name": d.name, "room": d.room, "floor": d.floor,
            "capabilities": list(d.capabilities),
            "channel": d.channel or None,
            "stream": self.hub.info(d.channel) if d.channel else None,
            "online": d.online,
            "lastSeenAt": d.last_seen_at,
            "firmware": d.firmware,
            "updatedAt": d.updated_at,
            "state": dict(d.reported),
        }
        if d.desired and d.delta():
            # 有待执行/未达成的期望值时一并给出，便于客户端显示「正在执行」
            item["desired"] = dict(d.desired)
            item["delta"] = d.delta()
        if d.faults:
            item["faults"] = list(d.faults.values())
        if d.type == "sensor":
            item["derived"] = derived.sensor_derived(
                float(d.reported.get("temperature") or 0.0),
                float(d.reported.get("humidity") or 0.0),
                at=d.updated_at,
            )
        return item

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
        """
        广播给所有**已认证**的客户端。

        未认证连接被排除在外是刻意的：设备影子、事件流、场景状态都属于
        「家里正在发生什么」，不该让一个还没出示令牌的连接读到。
        """
        data = msg if isinstance(msg, str) else json_dumps(msg)
        for client in list(self.clients):
            if not client.authed:
                continue
            client.push(data)

    def _send(self, client: Client, msg: dict[str, Any]) -> None:
        """点对点下发。不检查认证状态 —— 认证流程本身就要用这个通道。"""
        client.push(json_dumps(msg))

    def stats(self) -> dict[str, Any]:
        """运行统计，供 /health 与 /api/diagnostics 使用。"""
        return {
            "commandsTotal": self._commands_total,
            "rateLimited": self._rate_limited,
            "clients": len(self.clients),
            "authedClients": sum(1 for c in self.clients if c.authed),
            "outboxDropped": sum(c.dropped for c in self.clients),
            "outboxCoalesced": sum(c.coalesced for c in self.clients),
            "devices": {
                d.id: {
                    "online": d.online,
                    "lastSeenAt": d.last_seen_at,
                    "rssi": d.reported.get("rssi"),
                    "battery": d.reported.get("battery"),
                    "faults": list(d.faults),
                    "pendingDelta": d.delta() or None,
                }
                for d in self.devices.values()
            },
        }

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
