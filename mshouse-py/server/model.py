"""
MSHouse 镜像家居 · 物模型与协议常量（Mirror Space 智能家居教学场景）

教学分层：设备定义与协议格式集中在这里，网关与渲染层都只依赖本文件。

**本文件是唯一真源**：前端使用的 shared/model.js 由 scripts/export_model.py
从这里的定义生成，避免「同一份物模型维护两份」的经典漂移问题。
新增设备时：补一条 DEVICE_CATALOG，再在前端注册一个渲染器即可。

v2.0 起，本文件除「设备有哪些字段」外，还负责声明**设备行为参数**：
上报阈值（对应 Zigbee 的 ReportableChange / MinReportingInterval /
MaxReportingInterval）、执行延迟、心跳周期、字段取值约束。
这些参数以前散落在 gateway.py 里硬编码，现在集中于此，
才能做到「改一处、前后端与文档同时生效」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 版本号
# 1.0 → 2.0 的破坏性变更：
#   · state 中不再包含 comfort（派生量移入 derived 命名空间）
#   · ack.error 由字符串升级为结构化对象（过渡期双写 errorInfo）
#   · state 报文新增 stateKind（delta / full / confirmed）
# 主版本不一致的客户端会被网关以 PROTOCOL_MISMATCH 拒绝，
# 避免「新旧客户端各读一半字段」产生难查的显示错乱。
PROTOCOL_VERSION = "2.0"

# 握手标识
GATEWAY_NAME = "MSHouse Gateway"
GATEWAY_PROTOCOL = "mshouse/2.0"

# 初始化口令：不再以明文出现在源码与前端资源中。
# 首次启动时若未配置 security.setup_password_hash，网关会生成随机口令
# 并打印到服务端日志（只打印一次），由使用者保存。
# 这里只保留哈希算法参数，供 config.py 与 gateway.py 共用。
SETUP_PASSWORD_ALGO = "pbkdf2_sha256"
SETUP_PASSWORD_ITERATIONS = 200_000


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
    # v2.0 新增
    RESYNC = "resync"   # 客户端发现序号跳号后请求全量对齐
    FAULT = "fault"     # 设备级故障上报
    OTA = "ota"         # 固件升级状态机


class STATE_KIND:
    """state 报文的语义（对齐真实设备影子的 reported 语义）。"""

    DELTA = "delta"          # 仅包含本次变化的字段
    FULL = "full"            # 全量上报（重连、保活、恢复在线）
    CONFIRMED = "confirmed"  # 命令执行完成后的确认上报


class ERROR_CODES:
    """
    结构化错误码。前后端共用，便于客户端按 code 分支处理，
    而不是去匹配中文错误文案（文案随时可能改）。
    """

    BAD_JSON = "BAD_JSON"
    BAD_ENVELOPE = "BAD_ENVELOPE"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    ACTION_NOT_SUPPORTED = "ACTION_NOT_SUPPORTED"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    INVALID_PARAM = "INVALID_PARAM"
    READ_ONLY_DEVICE = "READ_ONLY_DEVICE"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    DEVICE_TIMEOUT = "DEVICE_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    SETUP_DENIED = "SETUP_DENIED"
    INTERNAL = "INTERNAL"


class FAULT_CODES:
    """典型设备故障码（教学演示用，命名参考真实设备的故障上报语义）。"""

    LAMP_BURNOUT = "LAMP_BURNOUT"          # 灯珠损坏
    DRIVER_OVERHEAT = "DRIVER_OVERHEAT"    # 驱动过温
    FILTER_DIRTY = "FILTER_DIRTY"          # 空调滤网脏堵
    COMPRESSOR_ERROR = "COMPRESSOR_ERROR"  # 压缩机故障
    LOW_BATTERY = "LOW_BATTERY"            # 电量低
    JAMMED = "JAMMED"                      # 锁舌卡阻
    LENS_OBSCURED = "LENS_OBSCURED"        # 镜头遮挡
    SIGNAL_WEAK = "SIGNAL_WEAK"            # 无线信号弱


# 故障码 → (严重级别, 中文说明)
FAULT_LABELS: dict[str, tuple[str, str]] = {
    FAULT_CODES.LAMP_BURNOUT: ("error", "灯珠损坏，无法点亮"),
    FAULT_CODES.DRIVER_OVERHEAT: ("warn", "驱动过温，亮度已受限"),
    FAULT_CODES.FILTER_DIRTY: ("warn", "滤网脏堵，制冷效率下降"),
    FAULT_CODES.COMPRESSOR_ERROR: ("error", "压缩机故障，空调无法制冷"),
    FAULT_CODES.LOW_BATTERY: ("warn", "电量低，请更换电池"),
    FAULT_CODES.JAMMED: ("error", "锁舌卡阻，请检查门体"),
    FAULT_CODES.LENS_OBSCURED: ("warn", "镜头遮挡，画面不可用"),
    FAULT_CODES.SIGNAL_WEAK: ("warn", "无线信号弱，上报可能丢失"),
}


class COMFORT:
    """THI 温湿指数分级（机器可读枚举，中文映射放在前端）。"""

    COOL = "cool"
    COMFORTABLE = "comfortable"
    WARM = "warm"
    HOT = "hot"


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
    # 上报参数覆盖（None 表示用 REPORTING[type] 的类型默认值）
    reporting: dict[str, dict[str, float]] | None = None

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


# ------------------------------------------------------------------ #
# 字段约束表
#
# 这里是「写操作准入」的唯一依据：gateway 不再散落 float()/int() 转换，
# 而是拿字段名来查表。好处是非法参数在进入设备逻辑之前就被拦下，
# 且错误信息可以精确到字段（真实设备返回的也是这种逐字段的拒绝原因）。
# ------------------------------------------------------------------ #

# 字段 → (类型, 下界, 上界)。int 类型会做取整校验（不接受 12.5）。
FIELD_LIMITS: dict[str, tuple[str, float, float]] = {
    "brightness": ("int", 0, 100),
    "colorTemp": ("int", 2700, 6500),
    "targetTemp": ("int", 16, 30),
    "indoorTemp": ("float", -20, 60),
    "pan": ("float", 0, 360),
    "delta": ("float", -180, 180),   # camera nudge 的步进量
}

# 字段 → 需要的能力位。未列出的字段（如 motion/facePresent）为内部量，不可写。
FIELD_CAPABILITY: dict[str, str] = {
    "power": "switch",
    "brightness": "brightness",
    "colorTemp": "colorTemp",
    "mode": "mode",
    "targetTemp": "targetTemp",
    "fan": "fan",
    "pan": "pan",
    "streaming": "stream",
    "locked": "lock",
}

# 枚举字段 → 允许取值
FIELD_ENUMS: dict[str, tuple[str, ...]] = {
    "mode": ("cool", "heat", "fan", "dry", "auto"),
    "fan": ("low", "mid", "high", "auto"),
}

# 布尔字段：只接受真正的 bool。
# 这里刻意不做「字符串 'true' → True」的宽松转换 —— 真实设备收到
# 类型不对的指令应当报错，而不是猜用户意图（猜错会直接改变物理世界状态）。
FIELD_BOOLS: tuple[str, ...] = ("power", "streaming", "locked")


# ------------------------------------------------------------------ #
# 上报参数表（对应真实无线传感设备的三个上报参数）
#
#   reportableChange  变化多少才上报（Zigbee: ReportableChange）
#   minInterval       两次上报的最小间隔秒数（MinReportingInterval）
#   maxInterval       最长静默多久必须保活上报一次（MaxReportingInterval）
#
# 数值参考市售温湿度传感器（Aqara / Sonoff 一类）的出厂配置：
# 稳态卧室里真实传感器可能十几分钟才上报一次，而不是每 2 秒一次。
# ------------------------------------------------------------------ #

REPORTING: dict[str, dict[str, dict[str, float]]] = {
    "sensor": {
        "temperature": {"reportableChange": 0.2, "minInterval": 10, "maxInterval": 600},
        "humidity": {"reportableChange": 1.0, "minInterval": 10, "maxInterval": 600},
        "battery": {"reportableChange": 1.0, "minInterval": 300, "maxInterval": 3600},
        "rssi": {"reportableChange": 3.0, "minInterval": 60, "maxInterval": 1800},
    },
    "ac": {
        "indoorTemp": {"reportableChange": 0.3, "minInterval": 15, "maxInterval": 900},
    },
    "lock": {
        "battery": {"reportableChange": 1.0, "minInterval": 300, "maxInterval": 3600},
    },
}

# 室外环境量的上报阈值（网关级，不属于任何设备）
OUTDOOR_REPORTING: dict[str, dict[str, float]] = {
    "temp": {"reportableChange": 0.1, "minInterval": 10, "maxInterval": 600},
    "humidity": {"reportableChange": 1.0, "minInterval": 10, "maxInterval": 600},
}

# 采样周期（秒）。采样频率与上报频率**解耦**：
# 内部每秒算一次模型，但只有满足 REPORTING 条件才发报文 ——
# 真实设备也是这么做的（传感器内部一直在量，但不会一直在发）。
TELEMETRY_SAMPLE_INTERVAL = 1.0


# ------------------------------------------------------------------ #
# 设备行为参数
# ------------------------------------------------------------------ #

DEVICE_BEHAVIOR: dict[str, dict[str, Any]] = {
    # execLatencyMs：从收到命令到设备真正执行完的模拟延迟。
    # 真实设备的这个延迟是存在的（继电器吸合、压缩机启动、锁舌动作），
    # 命令「立即生效」是仿真特有的假象。
    "light": {"execLatencyMs": 120, "heartbeatInterval": 30, "offlineThreshold": 3},
    "ac": {"execLatencyMs": 800, "heartbeatInterval": 30, "offlineThreshold": 3},
    "camera": {"execLatencyMs": 300, "heartbeatInterval": 30, "offlineThreshold": 3},
    "lock": {"execLatencyMs": 1500, "heartbeatInterval": 60, "offlineThreshold": 3},
    "sensor": {"execLatencyMs": 0, "heartbeatInterval": 60, "offlineThreshold": 3},
}

# 器件特征：分辨率与精度。上报值会被量化到分辨率，
# 精度用于文档标注（也用于生成符合器件规格的测量误差）。
DEVICE_ACCURACY: dict[str, dict[str, dict[str, float]]] = {
    "sensor": {
        "temperature": {"resolution": 0.1, "accuracy": 0.3},
        "humidity": {"resolution": 1.0, "accuracy": 3.0},
    },
}

# 电池模型：每 tick 的放电量与低电量阈值
BATTERY: dict[str, float] = {
    "drainPerDay": 0.1,      # 每天下降 0.1%（纽扣电池 + 低频上报）
    "lowThreshold": 15.0,    # 低于该值触发 LOW_BATTERY
}

# 云台步进与转速
PAN: dict[str, float] = {
    "step": 1.0,             # 上报与指令的角度步进（度）
    "speed": 45.0,           # 转动速度（度/秒）
    "presets": (0.0, 90.0, 180.0, 270.0),
}

# 移动侦测保持时间（秒）。真实 PIR 触发后会保持一段时间，
# 不会像早期版本那样 2.4 秒就复位。
MOTION_HOLD_SECONDS = 30.0

# 离家模式下「有人经过」的平均间隔（秒）。
# 旧版是每 2 秒抛一次 4% 的骰子 —— 那不是侦测，是随机数发生器。
# 真实场景里移动侦测的触发取决于有没有人经过，本身是随机的，
# 但触发频率应符合「几分钟一次」这个量级，而不是「几十秒一次」。
MOTION_MEAN_INTERVAL = 300.0

# 场景名 → 是否属于「联动完成后需校验」的场景
SCENE_VERIFY_TIMEOUT_MS = 8000


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


def reporting_for(spec: DeviceSpec, field_name: str) -> dict[str, float] | None:
    """取某设备某字段的上报参数：先看设备级覆盖，再回落到类型默认。"""
    if spec.reporting and field_name in spec.reporting:
        return spec.reporting[field_name]
    return REPORTING.get(spec.type, {}).get(field_name)


def behavior_for(device_type: str) -> dict[str, Any]:
    """取设备类型的行为参数，缺失时给保守默认值。"""
    return DEVICE_BEHAVIOR.get(device_type, {
        "execLatencyMs": 200, "heartbeatInterval": 30, "offlineThreshold": 3,
    })


def quantize(device_type: str, field_name: str, value: float) -> float:
    """
    把测量值量化到器件分辨率。

    真实传感器的上报值永远是分辨率的整数倍（温度 0.1°C、湿度 1%），
    上报 47.23% 这种数是仿真特有的。这里做一次量化，
    让报文里的数字本身就能看出器件规格。
    """
    spec = DEVICE_ACCURACY.get(device_type, {}).get(field_name)
    if not spec:
        return value
    step = spec["resolution"]
    if step <= 0:
        return value
    return round(round(value / step) * step, 3)
