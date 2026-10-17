"""
参数校验器

为什么需要单独一个模块：
早期版本的 _apply_patch 直接写 `int(clamp(float(patch["brightness"] or 0), 0, 100))`，
参数是字符串时 float() 抛 ValueError，异常一路冒到 WebSocket 处理函数，
客户端收到的是**连接被关闭**，而不是一条「brightness 不合法」的错误 ——
调试时完全看不出问题出在哪。

真实设备收到非法参数的行为是：拒绝这次写入、返回明确的错误原因、连接照常。
本模块把这件事做成可复用的能力：
  · 校验与业务逻辑分离，gateway 只负责「查表 → 通过就写 / 不通过就报错」
  · 错误信息精确到字段，与真实设备的逐字段拒绝原因一致
  · 不做「宽松转换」（"yes" 不当 True），避免猜错用户意图改错物理世界状态

校验策略是**原子**的：多个字段里只要有一个非法，整条命令拒绝、不写入任何字段。
这是为了避免设备进入「半更新」状态 —— 比如 brightness 写进去了、colorTemp 报错，
设备就停在一个用户没预期过的组合上。
"""

from __future__ import annotations

from typing import Any

from . import model as M


class ParamError(Exception):
    """
    单个字段的校验失败。仅在模块内部使用，对外统一转成错误字典。

    kind 用于区分两类失败，因为它们的含义完全不同：
      · capability —— 设备根本没有这个能力（写它本身就是用错了设备）
      · value      —— 设备有这个能力，但这次给的值不合法
    前者说明调用方理解错了物模型，后者只是这次参数写错了。
    真实设备的报错也会区分这两者，混为一谈会让调试变难。
    """

    def __init__(self, field_name: str, reason: str, kind: str = "value") -> None:
        super().__init__(reason)
        self.field = field_name
        self.reason = reason
        self.kind = kind


def _as_int(name: str, value: Any, lo: float, hi: float) -> int:
    # 布尔是 int 的子类，要单独排除，否则 True 会被当成 1 混进来
    if isinstance(value, bool):
        raise ParamError(name, f"期望 {lo:g}~{hi:g} 的整数，收到布尔值 {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise ParamError(name, f"期望整数，收到小数 {value!r}（不接受隐式取整）")
        value = int(value)
    if isinstance(value, str):
        # 数字字符串在 JSON 客户端里很常见（表单控件一律返回字符串），
        # 这里允许转换，但转换失败要报错而不是当成 0。
        text = value.strip()
        try:
            value = int(text)
        except ValueError:
            raise ParamError(name, f"期望 {lo:g}~{hi:g} 的整数，收到字符串 {value!r}") from None
    if not isinstance(value, int):
        raise ParamError(name, f"期望 {lo:g}~{hi:g} 的整数，收到 {type(value).__name__}")
    if not (lo <= value <= hi):
        raise ParamError(name, f"超出允许范围 {lo:g}~{hi:g}，收到 {value}")
    return value


def _as_float(name: str, value: Any, lo: float, hi: float) -> float:
    if isinstance(value, bool):
        raise ParamError(name, f"期望 {lo:g}~{hi:g} 的数值，收到布尔值 {value!r}")
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise ParamError(name, f"期望 {lo:g}~{hi:g} 的数值，收到字符串 {value!r}") from None
    if not isinstance(value, (int, float)):
        raise ParamError(name, f"期望 {lo:g}~{hi:g} 的数值，收到 {type(value).__name__}")
    value = float(value)
    # NaN / inf 能通过数值比较，必须显式挡掉，否则会污染设备状态
    if value != value or value in (float("inf"), float("-inf")):
        raise ParamError(name, f"数值非法（NaN 或无穷大）")
    if not (lo <= value <= hi):
        raise ParamError(name, f"超出允许范围 {lo:g}~{hi:g}，收到 {value}")
    return value


def _as_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    # 刻意不把 "true"/1 当 True：类型不对就报错，别猜
    raise ParamError(name, f"期望 true 或 false，收到 {type(value).__name__} {value!r}")


def _as_enum(name: str, value: Any, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str):
        raise ParamError(name, f"期望字符串枚举，收到 {type(value).__name__}")
    if value not in allowed:
        raise ParamError(name, f"取值必须是 {'/'.join(allowed)} 之一，收到 {value!r}")
    return value


def validate_field(name: str, value: Any) -> Any:
    """按物模型约束校验单个字段，返回清洗后的值。失败抛 ParamError。"""
    if name in M.FIELD_ENUMS:
        return _as_enum(name, value, M.FIELD_ENUMS[name])
    if name in M.FIELD_BOOLS:
        return _as_bool(name, value)
    limit = M.FIELD_LIMITS.get(name)
    if limit is None:
        # 未知字段：不静默忽略。真实设备收到不认识的属性会明确拒绝，
        # 静默忽略会让客户端以为写入成功了。
        raise ParamError(name, "该设备不支持此字段")
    kind, lo, hi = limit
    return _as_int(name, value, lo, hi) if kind == "int" else _as_float(name, value, lo, hi)


def check_capability(spec: M.DeviceSpec, name: str) -> None:
    """校验字段所需的能力位是否已声明。失败抛 ParamError。"""
    need = M.FIELD_CAPABILITY.get(name)
    if need is None:
        return
    if need not in spec.capabilities:
        raise ParamError(
            name,
            f"{spec.id} 未声明能力位 {need!r}，无法写入该字段"
            f"（当前能力：{'/'.join(spec.capabilities)}）",
            kind="capability",
        )


def validate_patch(spec: M.DeviceSpec, patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """
    校验一条写指令。

    返回 (清洗后的字段字典, 错误列表)：
      · 错误列表为空 → 第一个返回值可直接写入设备状态
      · 错误列表非空 → 第一个返回值无意义（原子拒绝，不写任何字段）

    错误项形如 {"field": "brightness", "reason": "超出允许范围 0~100，收到 120"}，
    与真实设备返回的逐字段拒绝原因结构一致。
    """
    if not isinstance(patch, dict):
        return {}, [{"field": "-", "reason": "params 必须是对象", "kind": "value"}]

    cleaned: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for name, value in patch.items():
        try:
            check_capability(spec, name)
            cleaned[name] = validate_field(name, value)
        except ParamError as err:
            errors.append({"field": err.field, "reason": err.reason, "kind": err.kind})
    if errors:
        return {}, errors
    return cleaned, []


def error_code_for(errors: list[dict[str, str]]) -> str:
    """
    由校验失败列表推导对外错误码。

    全部失败都是「能力位不匹配」时返回 UNSUPPORTED_CAPABILITY ——
    这是调用方理解错了物模型，与「这次参数写错了」是两种问题，
    分开报才便于定位。
    """
    if errors and all(e.get("kind") == "capability" for e in errors):
        return M.ERROR_CODES.UNSUPPORTED_CAPABILITY
    return M.ERROR_CODES.INVALID_PARAM


def validate_coordinates(lat: Any, lon: Any) -> tuple[float, float, list[dict[str, str]]]:
    """
    校验初始化设置里的经纬度。

    与设备字段分开，因为这里的失败语义不同：经纬度可以整体留空
    （表示「本次不改定位」），留空是合法输入而不是错误。
    """
    errors: list[dict[str, str]] = []

    def one(name: str, value: Any, lo: float, hi: float) -> float | None:
        if value in (None, ""):
            return None
        try:
            return _as_float(name, value, lo, hi)
        except ParamError as err:
            errors.append({"field": err.field, "reason": err.reason})
            return None

    latitude = one("lat", lat, -90.0, 90.0)
    longitude = one("lon", lon, -180.0, 180.0)
    # 只填一个视为填错：真实场景里单独的纬度没有意义
    if (latitude is None) != (longitude is None):
        errors.append({"field": "lat/lon", "reason": "经纬度必须同时填写或同时留空"})
    return latitude or 0.0, longitude or 0.0, errors


def error_payload(code: str, message: str, details: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """
    构造结构化错误对象。

    约定：所有对外的失败响应都走这里，保证 code 稳定可编程判断，
    message 面向人，details 面向具体字段（可选）。
    """
    payload: dict[str, Any] = {"code": code, "message": message}
    if details:
        payload["details"] = details
    return payload
