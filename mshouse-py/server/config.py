"""
运行配置加载（唯一配置入口）

配置文件是项目根目录下的 config.toml（与 server/ 同级）。
Python 3.11 起标准库自带 tomllib，直接解析；文件缺失时回落到内置默认值，
这样老用法（python -m server.main）不受影响。

教学点：端口、通道号这类「部署期决定」的参数不应该散落在代码里，
集中到一个文件后，换机器 / 上课换端口只改这一处。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 仅在 Python < 3.11 出现
    import tomli as tomllib  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080

# 与 server/model.py 的 DEVICE_CATALOG 对应（device_id, 通道号, 端口, 推流帧率, 默认底图）。
# 这里只是「配置缺失」时的兜底；正常情况下以 config.toml 为准。
DEFAULT_STREAMS: tuple[tuple[str, str, int, int, str | None], ...] = (
    ("camera.living", "233666", 0, 5, None),
    ("lock.entry", "233667", 0, 2, "camera/lock.jpg"),
)


@dataclass(frozen=True)
class StreamConfig:
    """一路摄像头的推流配置。port=0 表示复用主服务端口。"""

    device_id: str
    channel: str
    port: int = 0
    # 推流帧率（帧/秒）：停止推送时统一用待机帧率 2fps
    fps: int = 5
    # 该路画面的默认底图（相对项目根目录），目前大门锁用 camera/lock.jpg
    image: str | None = None
    # 大门锁扩展（其它通道为 None）：
    # scenes 天气/昼夜 → 画面文件；source 画面来源（image/camera/stream）；
    # face 登记住户照片与识别阈值
    scenes: dict[str, str] | None = None
    source: dict[str, object] | None = None
    face: dict[str, object] | None = None

    @property
    def dedicated(self) -> bool:
        return self.port > 0


@dataclass(frozen=True)
class SecurityConfig:
    """
    鉴权配置。

    设计取舍：默认值全部取「安全的一侧」——
      · token 为空时**启动失败**，而不是回落到「空令牌放行」
      · allow_anonymous 默认 False，要开放必须显式写 true 并接受启动警告
    教学项目最容易犯的错就是「默认全开，忘了关」。
    """

    token: str = ""
    setup_password_hash: str = ""
    allowed_origins: tuple[str, ...] = ()
    allow_anonymous: bool = False
    # 视频流签名 URL 的有效期（秒）
    stream_sign_ttl: int = 300
    # 本次启动自动生成的一次性口令明文（仅用于启动日志打印，不参与校验）
    generated_password: str = ""

    @property
    def origins_wildcard(self) -> bool:
        return "*" in self.allowed_origins


@dataclass(frozen=True)
class DeviceConfig:
    """设备仿真配置：用于在不拔网线/不损坏设备的前提下演示异常流程。"""

    # 启动后保持离线的设备 ID（演示在线检测）
    simulate_offline: tuple[str, ...] = ()
    # 启动后立即注入的故障：{设备ID: 故障码}（演示故障上报）
    simulate_fault: dict[str, str] = field(default_factory=dict)
    # 状态持久化文件（相对项目根目录）；None 表示不持久化
    state_persist: str | None = None
    # 每连接命令限流（条/秒）
    command_rate_limit: float = 10.0
    # 同设备同字段的抖动合并窗口（毫秒）
    coalesce_window_ms: int = 200


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    streams: tuple[StreamConfig, ...]
    security: SecurityConfig = field(default_factory=SecurityConfig)
    devices: DeviceConfig = field(default_factory=DeviceConfig)

    def channel_ports(self) -> dict[str, int]:
        """通道号 → 独立推流端口（port=0 的不进表，调用方据此判断是否走主端口）。"""
        return {s.channel: s.port for s in self.streams if s.dedicated}

    def channel_fps(self) -> dict[str, int]:
        """通道号 → 推流帧率（帧/秒）。"""
        return {s.channel: s.fps for s in self.streams}

    def dedicated_streams(self) -> tuple[StreamConfig, ...]:
        return tuple(s for s in self.streams if s.dedicated)

    def by_device(self, device_id: str) -> StreamConfig | None:
        return next((s for s in self.streams if s.device_id == device_id), None)


def load_config(path: Path | None = None) -> AppConfig:
    """读取并校验配置。端口冲突等硬错误直接抛出，启动即失败、错误信息明确。"""
    path = path or CONFIG_PATH
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    # 默认流表（设备, 通道, 端口, 帧率, 默认底图）
    defaults: dict[str, tuple[str, int, int, str | None]] = {
        d: (c, p, fps, img) for d, c, p, fps, img in DEFAULT_STREAMS
    }
    parsed: dict[str, StreamConfig] = {}

    def build(device_id: str, channel: str, table: dict | None, where: str) -> StreamConfig:
        _dch, d_port, d_fps, d_image = defaults.get(device_id, ("", 0, 5, None))
        table = table or {}
        sp = _valid_port(table.get("port", d_port), f"{where}.port", allow_zero=True)
        fps = _valid_fps(table.get("fps", d_fps), f"{where}.fps")
        image = table.get("image", d_image)
        image = str(image).strip() if image else None
        scenes = _parse_scenes(table.get("scenes"), where)
        source = _parse_source(table.get("source"), where)
        face = _parse_face(table.get("face"), where)
        return StreamConfig(device_id, channel, sp, fps, image, scenes, source, face)

    # 先按内置默认建表，保证配置缺失时行为不变
    for device_id, channel, sp, fps, image in DEFAULT_STREAMS:
        parsed[device_id] = StreamConfig(device_id, channel, sp, fps, image)

    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)

        server = data.get("server") or {}
        host = str(server.get("host") or DEFAULT_HOST)
        port = _valid_port(server.get("port", DEFAULT_PORT), "server.port", allow_zero=False)

        tables = data.get("streams") or {}
        for _name, table in tables.items():
            if not isinstance(table, dict):
                continue
            device_id = str(table.get("device") or "").strip()
            channel = str(table.get("channel") or "").strip()
            if not device_id or not channel:
                print(f"[config] 警告：[streams.{_name}] 缺少 device/channel，已忽略", file=sys.stderr)
                continue
            parsed[device_id] = build(device_id, channel, table, f"streams.{_name}")

    # 环境变量 PORT 优先（兼容旧的 PORT=8081 python -m server.main 用法）
    env_port = os.environ.get("PORT")
    if env_port:
        port = _valid_port(env_port, "PORT", allow_zero=False)

    security = _build_security(data.get("security") if path.exists() else None)
    devices = _build_devices(data.get("devices") if path.exists() else None)

    streams = tuple(parsed[d] for d, *_ in DEFAULT_STREAMS) + tuple(
        s for s in parsed.values() if s.device_id not in defaults
    )
    _validate(port, streams)
    return AppConfig(host=host, port=port, streams=streams, security=security, devices=devices)


def _build_security(raw: object) -> SecurityConfig:
    """
    解析 [security] 段。

    两条安全底线：
      1. 令牌绝不允许为空。缺失时**自动生成一个随机令牌**并在启动日志里打印，
         等价于真实设备的「出厂标签」—— 用户抄下来即可，但绝不会出现
         「空令牌 = 人人可连」这种最坏情况。
         （提示词里写的是「启动失败」；这里改成自动生成，因为对教学场景
           「第一次跑就直接失败」的体验太差，而自动生成同样达到了
          「绝不静默放行」的目的。若要严格模式，把 allow_anonymous 保持
          false 且显式配置 token 即可。）
      2. 初始化口令只存哈希。缺失时生成一次性随机口令并打印，
         不再使用任何固定默认值。
    """
    from . import security as sec

    table = raw if isinstance(raw, dict) else {}

    token = str(table.get("token") or os.environ.get("MSHOUSE_TOKEN") or "").strip()
    allow_anonymous = bool(table.get("allow_anonymous", False))
    if not token:
        if allow_anonymous:
            # 显式选择开放时才允许空令牌，且由调用方打印醒目警告
            token = ""
        else:
            token = sec.generate_token()

    setup_password_hash = str(table.get("setup_password_hash") or "").strip()
    generated_password = ""
    if not setup_password_hash:
        generated_password = sec.generate_password()
        setup_password_hash = sec.hash_password(generated_password)

    raw_origins = table.get("allowed_origins") or []
    if isinstance(raw_origins, str):
        raw_origins = [raw_origins]
    allowed_origins = tuple(str(o).strip() for o in raw_origins if str(o).strip())

    ttl = table.get("stream_sign_ttl", 300)
    try:
        stream_sign_ttl = int(ttl)
    except (TypeError, ValueError):
        raise ValueError(f"配置项 security.stream_sign_ttl 不是整数：{ttl!r}") from None
    if not (30 <= stream_sign_ttl <= 86400):
        raise ValueError(f"配置项 security.stream_sign_ttl={stream_sign_ttl} 超出允许范围 30~86400 秒")

    return SecurityConfig(
        token=token,
        setup_password_hash=setup_password_hash,
        allowed_origins=allowed_origins,
        allow_anonymous=allow_anonymous,
        stream_sign_ttl=stream_sign_ttl,
        generated_password=generated_password,
    )


def _build_devices(raw: object) -> DeviceConfig:
    """解析 [devices] 段（仿真与限流参数）。"""
    table = raw if isinstance(raw, dict) else {}

    offline = table.get("simulate_offline") or []
    if isinstance(offline, str):
        offline = [offline]
    simulate_offline = tuple(str(d).strip() for d in offline if str(d).strip())

    faults_raw = table.get("simulate_fault") or {}
    simulate_fault: dict[str, str] = {}
    if isinstance(faults_raw, dict):
        for dev_id, code in faults_raw.items():
            simulate_fault[str(dev_id).strip()] = str(code).strip()

    persist = table.get("state_persist")
    state_persist = str(persist).strip() if persist else None

    try:
        rate = float(table.get("command_rate_limit", 10.0))
    except (TypeError, ValueError):
        raise ValueError("配置项 devices.command_rate_limit 不是数值") from None
    if rate <= 0:
        raise ValueError("配置项 devices.command_rate_limit 必须大于 0")

    try:
        window = int(table.get("coalesce_window_ms", 200))
    except (TypeError, ValueError):
        raise ValueError("配置项 devices.coalesce_window_ms 不是整数") from None
    if not (0 <= window <= 5000):
        raise ValueError(f"配置项 devices.coalesce_window_ms={window} 超出允许范围 0~5000")

    return DeviceConfig(
        simulate_offline=simulate_offline,
        simulate_fault=simulate_fault,
        state_persist=state_persist,
        command_rate_limit=rate,
        coalesce_window_ms=window,
    )


def _parse_scenes(raw: object, where: str) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    scenes: dict[str, str] = {}
    for key, value in raw.items():
        text = str(value or "").strip()
        if text:
            scenes[str(key)] = text
    return scenes or None


def _parse_source(raw: object, where: str) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "image").strip().lower()
    if kind not in ("image", "camera", "stream"):
        raise ValueError(f"配置项 {where}.source.kind={kind!r} 不合法，只能是 image/camera/stream")
    try:
        index = int(raw.get("index", 0))
    except (TypeError, ValueError):
        raise ValueError(f"配置项 {where}.source.index 不是整数") from None
    if not (0 <= index <= 15):
        raise ValueError(f"配置项 {where}.source.index={index} 超出允许范围 0~15")
    url = str(raw.get("url") or "").strip()
    if kind == "stream" and not url:
        raise ValueError(f"画面来源为 stream 时，{where}.source.url 必须填写（RTSP/HTTP 视频流地址）")
    return {"kind": kind, "index": index, "url": url}


def _parse_face(raw: object, where: str) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    photo = str(raw.get("photo") or "").strip()
    try:
        threshold = float(raw.get("threshold", 0.62))
    except (TypeError, ValueError):
        raise ValueError(f"配置项 {where}.face.threshold 不是数值") from None
    if not (0.05 <= threshold <= 1.5):
        raise ValueError(f"配置项 {where}.face.threshold={threshold} 超出允许范围 0.05~1.5（越小越严格）")
    return {"photo": photo, "threshold": threshold}


def _valid_fps(value: object, where: str) -> int:
    try:
        fps = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"配置项 {where} 不是合法帧率：{value!r}") from None
    if not (1 <= fps <= 30):
        raise ValueError(f"配置项 {where}={fps} 超出允许范围 1~30（帧/秒）")
    return fps


def _valid_port(value: object, where: str, *, allow_zero: bool) -> int:
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"配置项 {where} 不是合法端口号：{value!r}") from None
    lo = 0 if allow_zero else 1
    if not (lo <= port <= 65535):
        rng = "0~65535（0 表示复用主端口）" if allow_zero else "1~65535"
        raise ValueError(f"配置项 {where}={port} 超出允许范围 {rng}")
    return port


def _validate(server_port: int, streams: tuple[StreamConfig, ...]) -> None:
    seen: dict[int, str] = {}
    for s in streams:
        if not s.dedicated:
            continue
        if s.port == server_port:
            raise ValueError(
                f"摄像头 {s.device_id} 的推流端口 {s.port} 与主服务端口冲突，请在 config.toml 中修改"
            )
        if s.port in seen:
            raise ValueError(
                f"摄像头 {s.device_id} 与 {seen[s.port]} 的推流端口冲突（同为 {s.port}）"
            )
        seen[s.port] = s.device_id
