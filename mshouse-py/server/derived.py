"""
派生量计算

为什么要把「舒适」从设备状态里拿出来：
早期版本把 comfort 直接写进 sensor 的 state，和 temperature / humidity 并列。
但真实温湿度传感器（Zigbee 0x0402、Matter TemperatureMeasurement）
只上报物理量，不会在自己的属性里塞一个中文语义标签 ——
「舒适」是应用层算出来的，不是设备测出来的。

而且原来的判据也站不住：`23~27°C 且 40~60%RH` 没有标准出处；
用 `h > 70 → 偏闷` 更错 —— 30°C/75% 和 18°C/75% 会被压成同一个结论，
但这两者的体感差别很大。

现在改为：
  · 物理量留在 device.state（设备原生能力）
  · 派生量放进独立的 derived 命名空间，并带算法版本号
  · 算法换成有出处的 THI 温湿指数

关于算法选择：
THI（Temperature-Humidity Index）只需要温湿度两个输入，
与现有传感器能力正好匹配，因此作为默认实现。
若日后要更高保真，可换成 ASHRAE 55 的 PMV/PPD（需要风速、辐射温度、
代谢率、服装热阻共 6 个输入）或 GB/T 33658 热舒适评价 ——
届时只需新增一个算法实现并把 ALGORITHM 版本号 +1，
历史数据仍能按旧版本号解读，不会混淆。
"""

from __future__ import annotations

from typing import Any

from . import model as M

# 算法版本号。换算法必须改这个值 —— 它随每条 derived 报文一起发出，
# 让消费端能区分「这条数据是按哪个公式算的」。
ALGORITHM = "thi-v1"


def thi(temperature: float, humidity: float) -> float:
    """
    THI 温湿指数。

    公式：THI = T - 0.55 × (1 - RH/100) × (T - 14.5)

    这个式子来自温湿指数的通行形式：干燥空气的降温效果随湿度上升而减弱，
    所以同样的气温，湿度越高体感越热。这也是为什么 30°C/75% 与
    18°C/75% 必须得到不同结论 —— 原判据做不到这一点。
    """
    return temperature - 0.55 * (1 - humidity / 100.0) * (temperature - 14.5)


def classify(value: float) -> str:
    """
    THI 分级（返回机器可读枚举，中文映射放在前端）。

    分级阈值参考通行的人居温湿指数区间：
      < 21        偏凉
      21 ~ 24     舒适
      24 ~ 27     偏热
      ≥ 27        闷热
    """
    if value < 21:
        return M.COMFORT.COOL
    if value < 24:
        return M.COMFORT.COMFORTABLE
    if value < 27:
        return M.COMFORT.WARM
    return M.COMFORT.HOT


def sensor_derived(temperature: float, humidity: float, *, at: int) -> dict[str, Any]:
    """
    构造传感器的派生量块。

    输出结构：
      {"thi": 23.4, "comfort": "comfortable", "algorithm": "thi-v1", "computedAt": ...}

    algorithm 字段是刻意保留的：派生量会随算法迭代而变，
    没有版本号的话，历史数据在换算法后会变得无法解释。
    """
    value = thi(temperature, humidity)
    return {
        "thi": round(value, 1),
        "comfort": classify(value),
        "algorithm": ALGORITHM,
        "computedAt": at,
    }
