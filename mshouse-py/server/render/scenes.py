"""
服务端画面合成：把设备状态画成「监控画面」（Python 版）。

两路画面：
  LivingRoomSource —— 客厅云台。用软渲染把客厅的家具几何真正透视投影出来，
                      所以转云台、开灯、布防，画面都会跟着变。
  EntrySource      —— 大门人脸锁。门外视角的 2D 合成（人脸 + 识别框 + OSD）。

分层：本文件认识「设备状态」，不认识 HTTP / WebSocket。
      它只吐出一个 Raster，编码与分发交给 ../stream.py。

几何与前端三维场景（public/js/scene.js）用的是同一套房间尺寸与家具坐标，
方便课堂上对照讲解「同一份物模型，两种呈现」。

与 Node 版的差异：原本逐像素的循环（门厅径向渐变、扫描线）已改为 numpy
向量化，否则在 Python 里单帧要几十毫秒。
"""

from __future__ import annotations

import math
import time
from functools import lru_cache

import numpy as np

from .raster import Camera, Face, Light, Raster, box_faces, draw_scene, kelvin, quad

# ------------------------------------------------------------------ #
# 客厅尺寸与家具（与 scene.js 对齐，单位：米）
# ------------------------------------------------------------------ #
X0, X1 = -6.0, 1.5          # 客厅西墙 / 客厅与厨房的分隔墙
Z0, Z1 = -4.5, 4.5          # 北墙 / 南墙（前墙，带入户门）
CEIL = 3.0
# 云台光心。高度取 2.72m（贴近天花板）：装机位置越高、视线越陡，
# 越能越过电视与茶几看到沙发 —— 真实云台装墙角也是这个道理。
CAM_POS = (-0.6, 2.72, -4.08)
CAM_TILT = 0.3              # 下俯角（弧度），约 17°
FOV = math.radians(58)

C = {
    "floor": (124, 90, 60),
    "wall": (206, 198, 182),
    "wallSide": (192, 188, 180),
    "ceil": (230, 226, 218),
    "rug": (70, 82, 94),
    "fabric": (104, 116, 108),
    "fabric2": (126, 134, 122),
    "wood": (140, 100, 64),
    "dark": (26, 31, 36),
    "green": (86, 138, 90),
    "metal": (148, 154, 158),
    "white": (224, 224, 220),
    "door": (98, 68, 44),
    "frame": (168, 158, 142),
}

SKY_DAY = (156, 194, 216)
SKY_NIGHT = (20, 30, 46)

# OSD 只能画 ASCII，中文名在这里转写一次
NAME_ASCII = {
    "林晓": "LIN XIAO",
    "陈舟": "CHEN ZHOU",
    "未登记访客": "UNKNOWN GUEST",
}

# 绘制层级：0 壳体 → 1 贴附物 → 2 家具（详见 raster.draw_scene）
L_FIX = 1
L_FURN = 2


def clock_of(ts: int) -> str:
    lt = time.localtime(ts / 1000)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"


def ease_pan(smooth: dict, target: float, dt: float) -> float:
    """云台角做指数缓动，避免指令一到位画面瞬移。"""
    if not smooth["inited"]:
        smooth["pan"] = target
        smooth["inited"] = True
    delta = ((target - smooth["pan"]) % 360 + 540) % 360 - 180
    smooth["pan"] = (smooth["pan"] + delta * min(1.0, dt * 5) + 360) % 360
    return smooth["pan"]


def norm3(v) -> tuple[float, float, float]:
    m = math.hypot(v[0], v[1], v[2]) or 1.0
    return (v[0] / m, v[1] / m, v[2] / m)


# ------------------------------------------------------------------ #
# 客厅几何
# ------------------------------------------------------------------ #
@lru_cache(maxsize=8)
def build_living_faces(night: bool, lamp_on: bool) -> tuple[Face, ...]:
    """构建客厅所有面。结果按 (night, lamp_on) 缓存 —— 一帧几百个面对象，
    每帧重建在 Python 里开销可观，而这两个入参只有 4 种组合。"""
    faces: list[Face] = []
    sky = SKY_NIGHT if night else SKY_DAY

    # 壳体：地面 / 天花板 / 四面墙（法线朝房间内）
    faces.append(quad([(X0, 0, Z0), (X1, 0, Z0), (X1, 0, Z1), (X0, 0, Z1)], (0, 1, 0), C["floor"]))
    faces.append(quad([(X0, CEIL, Z0), (X0, CEIL, Z1), (X1, CEIL, Z1), (X1, CEIL, Z0)], (0, -1, 0), C["ceil"]))
    faces.append(quad([(X0, 0, Z0), (X1, 0, Z0), (X1, CEIL, Z0), (X0, CEIL, Z0)], (0, 0, 1), C["wall"]))
    faces.append(quad([(X0, 0, Z1), (X0, CEIL, Z1), (X1, CEIL, Z1), (X1, 0, Z1)], (0, 0, -1), C["wall"]))
    faces.append(quad([(X0, 0, Z0), (X0, CEIL, Z0), (X0, CEIL, Z1), (X0, 0, Z1)], (1, 0, 0), C["wallSide"]))
    faces.append(quad([(X1, 0, Z0), (X1, 0, Z1), (X1, CEIL, Z1), (X1, CEIL, Z0)], (-1, 0, 0), C["wallSide"]))

    # 贴附物：南墙的窗与入户门、西墙的窗、东墙的门洞、吸顶灯盘、地毯
    # （略微内缩，避免与墙面同深度导致前后关系不确定）
    faces.append(quad([(-1.0, 0.95, Z1 - 0.04), (1.1, 0.95, Z1 - 0.04), (1.1, 2.35, Z1 - 0.04), (-1.0, 2.35, Z1 - 0.04)],
                      (0, 0, -1), sky, order=L_FIX, flat=True))
    faces.append(quad([(-4.02, 0, Z1 - 0.05), (-2.78, 0, Z1 - 0.05), (-2.78, 2.15, Z1 - 0.05), (-4.02, 2.15, Z1 - 0.05)],
                      (0, 0, -1), C["door"], order=L_FIX))
    faces.append(quad([(X0 + 0.04, 0.95, -2.1), (X0 + 0.04, 2.35, -2.1), (X0 + 0.04, 2.35, 0.5), (X0 + 0.04, 0.95, 0.5)],
                      (1, 0, 0), sky, order=L_FIX, flat=True))
    faces.append(quad([(X1 - 0.04, 0, -1.7), (X1 - 0.04, 0, -0.2), (X1 - 0.04, 2.2, -0.2), (X1 - 0.04, 2.2, -1.7)],
                      (-1, 0, 0), (16, 18, 20) if night else (52, 54, 56), order=L_FIX, flat=True))
    faces.append(quad([(-3.4, CEIL - 0.06, 0.0), (-2.4, CEIL - 0.06, 0.0), (-2.4, CEIL - 0.06, 1.0), (-3.4, CEIL - 0.06, 1.0)],
                      (0, -1, 0), (255, 246, 226) if lamp_on else (196, 192, 184),
                      order=L_FIX, flat=lamp_on))
    faces.append(quad([(-4.9, 0.06, -0.9), (-0.9, 0.06, -0.9), (-0.9, 0.06, 1.9), (-4.9, 0.06, 1.9)],
                      (0, 1, 0), C["rug"], order=L_FIX))

    # 家具：电视柜 / 电视 / 沙发 / 茶几 / 边几 / 绿植 / 楼梯
    faces += box_faces(-4.2, -1.6, 0.07, 0.49, -1.31, -0.89, C["wood"], order=L_FURN)
    faces += box_faces(-3.75, -2.05, 0.745, 1.695, -1.13, -1.07, C["dark"], order=L_FURN)
    faces += box_faces(-4.5, -1.3, 0.07, 0.47, 1.375, 2.325, C["fabric"], order=L_FURN)
    faces += box_faces(-4.5, -1.3, 0.445, 0.995, 2.15, 2.37, C["fabric"], order=L_FURN)
    faces += box_faces(-4.51, -4.29, 0.37, 0.87, 1.375, 2.325, C["fabric"], order=L_FURN)
    faces += box_faces(-1.51, -1.29, 0.37, 0.87, 1.375, 2.325, C["fabric"], order=L_FURN)
    for i in (-1, 0, 1):
        cx = -2.9 + i * 1.0
        faces += box_faces(cx - 0.31, cx + 0.31, 0.47, 0.77, 1.85, 2.35, C["fabric2"], order=L_FURN)
    faces += box_faces(-3.65, -2.15, 0.395, 0.485, 0.1, 0.9, C["wood"], order=L_FURN)
    for dx, dz in ((-0.66, -0.32), (0.66, -0.32), (-0.66, 0.32), (0.66, 0.32)):
        faces += box_faces(-2.9 + dx - 0.035, -2.9 + dx + 0.035, 0, 0.395,
                           0.5 + dz - 0.035, 0.5 + dz + 0.035, C["metal"], order=L_FURN)
    faces += box_faces(-0.31, 0.11, 0.07, 0.49, 2.39, 2.81, C["wood"], order=L_FURN)
    faces += box_faces(-0.44, 0.24, 0.55, 1.12, 2.26, 2.94, C["green"], order=L_FURN)

    steps, run = 8, 3.2 / 8
    rise = 3.22 / steps
    for i in range(steps):
        z = Z0 + 0.15 + i * run
        faces += box_faces(-5.9, -4.5, 0, (i + 1) * rise, z, z + run, C["wallSide"], order=L_FURN)

    return tuple(faces)


def living_lights(night: bool, lamp: dict) -> list[Light]:
    lights: list[Light] = []
    lights.append(Light(
        dir=norm3((0.18, 0.9, 0.42)),
        color=(0.36, 0.46, 0.72) if night else (1.0, 0.97, 0.9),
        intensity=0.12 if night else 0.55,
    ))
    on = bool(lamp.get("on"))
    brightness = float(lamp.get("brightness") or 0)
    if on and brightness > 0:
        k = (brightness / 100.0) * 0.85
        col = kelvin(float(lamp.get("colorTemp") or 4000))
        lights.append(Light(dir=(0, 1, 0), color=col, intensity=k))            # 主光：照亮地面与家具
        lights.append(Light(dir=(0, -1, 0), color=col, intensity=k * 0.16))    # 天花板与墙面散射
    return lights


# ------------------------------------------------------------------ #
# OSD
# ------------------------------------------------------------------ #
def draw_osd(raster: Raster, *, label: str, pan: float, armed: bool, motion: bool,
             live: bool, channel: str, fps: int, now: int, night: bool) -> None:
    """顶部 / 底部 OSD 信息条。"""
    w, h = raster.width, raster.height
    ink = (243, 239, 230)
    bar_h = 22

    raster.fill_rect(0, 0, w, bar_h, (0, 0, 0), 0.42)
    raster.text(label, 8, 7, ink, 2)
    mid = f"{int(round(pan)):03d}\u00b0  {'ARMED' if armed else 'DISARMED'}"
    raster.text(mid, 8 + Raster.text_width(label, 2) + 14, 7,
                (226, 177, 90) if armed else (168, 178, 172), 2)

    stamp = clock_of(now)
    raster.text(stamp, w - Raster.text_width(stamp, 2) - 8, 7, ink, 2)

    raster.fill_rect(0, h - bar_h, w, bar_h, (0, 0, 0), 0.42)
    raster.text(f"CH {channel}", 8, h - bar_h + 7, ink, 2)
    stat = "STREAM ONLINE" if live else "STREAM IDLE"
    raster.text(stat, 8 + Raster.text_width(f"CH {channel}", 2) + 14, h - bar_h + 7,
                (125, 206, 160) if live else (168, 178, 172), 2)
    res = f"{raster.width}X{raster.height} {fps}FPS"
    raster.text(res, w - Raster.text_width(res, 2) - 8, h - bar_h + 7, (168, 178, 172), 2)

    # 中心准星
    cx, cy = w / 2, h / 2
    cross = (235, 232, 224)
    raster.line(cx - 22, cy, cx - 7, cy, cross, 1, 0.8)
    raster.line(cx + 7, cy, cx + 22, cy, cross, 1, 0.8)
    raster.line(cx, cy - 22, cx, cy - 7, cross, 1, 0.8)
    raster.line(cx, cy + 7, cx, cy + 22, cross, 1, 0.8)

    # 录制指示：推流时右上角红点闪烁
    if live and int(now / 600) % 2 == 0:
        raster.fill_rect(w - 46, 30, 8, 8, (239, 111, 108))
        raster.text("REC", w - 34, 29, (239, 111, 108), 2)

    # 移动侦测告警框
    if motion:
        bx, by, bw, bh = 10, 30, w - 20, h - 60
        raster.line(bx, by, bx + bw, by, (239, 111, 108), 2)
        raster.line(bx, by + bh, bx + bw, by + bh, (239, 111, 108), 2)
        raster.line(bx, by, bx, by + bh, (239, 111, 108), 2)
        raster.line(bx + bw, by, bx + bw, by + bh, (239, 111, 108), 2)
        raster.text("MOTION DETECTED", bx + 6, by + 6, (239, 111, 108), 2)

    # 夜间模式下画面偏冷、噪点更重
    raster.noise(20 if night else 9, 5)
    raster.vignette(0.62 if night else 0.45)


# ------------------------------------------------------------------ #
# 通道帧源
# ------------------------------------------------------------------ #
class LivingRoomSource:
    """客厅云台画面源。"""

    def __init__(self, width: int = 512, height: int = 320,
                 channel: str = "", label: str = "LIVING CAM") -> None:
        self.width = width
        self.height = height
        self.channel = channel
        self.label = label
        self._smooth = {"pan": 0.0, "inited": False}

    def render(self, *, state: dict | None = None, env: dict | None = None,
               now: int | None = None, dt: float = 0.125, fps: int = 8) -> Raster:
        state = state or {}
        env = env or {}
        now = now if now is not None else int(time.time() * 1000)
        night = bool(env.get("night"))
        lamp = env.get("lamp") or {"on": False, "brightness": 0, "colorTemp": 4000}

        pan = ease_pan(self._smooth, float(state.get("pan") or 0), dt)
        raster = Raster(self.width, self.height)
        raster.fill_rect(0, 0, self.width, self.height, (14, 18, 22) if night else (22, 26, 30))

        cam = Camera(pos=CAM_POS, pan=pan, tilt=CAM_TILT, fov_y=FOV,
                     width=self.width, height=self.height, near=0.06)
        lamp_on = bool(lamp.get("on")) and float(lamp.get("brightness") or 0) > 0
        faces = build_living_faces(night, lamp_on)
        lights = living_lights(night, lamp)
        # 环境项偏高：真实房间里墙面之间的漫反射会把垂直面也提亮，
        # 纯方向光会让墙和家具侧面全黑，反而不像监控画面。
        ambient = (0.17 if night else 0.4) + (0.06 if lamp.get("on") else 0)
        draw_scene(raster, cam, faces, lights, ambient)

        draw_osd(raster, label=self.label, pan=pan, armed=bool(state.get("armed")),
                 motion=bool(state.get("motion")), live=bool(state.get("streaming")),
                 channel=self.channel, fps=fps, now=now, night=night)
        return raster


class EntrySource:
    """大门人脸锁：门外视角。"""

    _bg_cache: dict[tuple[int, int], np.ndarray] = {}

    def __init__(self, width: int = 512, height: int = 320,
                 channel: str = "", label: str = "ENTRY LOCK") -> None:
        self.width = width
        self.height = height
        self.channel = channel
        self.label = label

    def _background(self) -> np.ndarray:
        """门外走廊：中间亮、四周暗。原版是逐像素循环，这里向量化并缓存。"""
        key = (self.width, self.height)
        bg = EntrySource._bg_cache.get(key)
        if bg is None:
            w, h = self.width, self.height
            cx, cy = w / 2, h * 0.44
            yy, xx = np.mgrid[0:h, 0:w]
            d = np.hypot(xx - cx, yy - cy) / math.hypot(cx, cy)
            t = np.minimum(1.0, d * 1.15)
            k = (1.0 - t) ** 1.6
            bg = np.empty((h, w, 3), dtype=np.uint8)
            bg[:, :, 0] = np.clip(18 + 52 * k, 0, 255).astype(np.uint8)
            bg[:, :, 1] = np.clip(24 + 62 * k, 0, 255).astype(np.uint8)
            bg[:, :, 2] = np.clip(22 + 56 * k, 0, 255).astype(np.uint8)
            EntrySource._bg_cache[key] = bg
        return bg

    def render(self, *, state: dict | None = None, env: dict | None = None,
               now: int | None = None, fps: int = 8) -> Raster:
        state = state or {}
        now = now if now is not None else int(time.time() * 1000)
        raster = Raster(self.width, self.height)
        w, h = self.width, self.height
        raster.paste_array(self._background())

        last_person = state.get("lastPerson")
        who = NAME_ASCII.get(last_person, "VISITOR") if last_person else None
        passed = state.get("lastResult") == "pass"
        rejected = state.get("lastResult") == "reject"
        cx = w / 2
        fy = h * 0.46

        if who:
            # 来人：画一个简笔人脸，让「识别」这件事看得见
            skin = (214, 192, 162)
            hair = (58, 44, 34)
            raster.ellipse(cx, fy + 58, 62, 46, (48, 60, 64))        # 肩
            raster.ellipse(cx, fy, 42, 52, skin)                      # 脸
            raster.ellipse(cx, fy - 36, 44, 28, hair)                 # 头发
            raster.fill_rect(cx - 46, fy - 32, 92, 12, hair)          # 刘海
            raster.ellipse(cx - 15, fy - 2, 5.5, 5.5, (40, 38, 36))   # 眼
            raster.ellipse(cx + 15, fy - 2, 5.5, 5.5, (40, 38, 36))
            raster.fill_rect(cx - 11, fy + 24, 22, 3, (150, 96, 88))  # 嘴
        else:
            raster.ellipse(cx, fy, 42, 52, (30, 40, 40))              # 无人时的轮廓

        # 识别框
        box = (125, 206, 160) if passed else (239, 111, 108) if rejected else (226, 177, 90)
        bx, by, bw, bh = cx - 74, fy - 84, 148, 192
        seg = 22

        def corner(x: float, y: float, sx: int, sy: int) -> None:
            raster.line(x, y, x + sx * seg, y, box, 3)
            raster.line(x, y, x, y + sy * seg, box, 3)

        corner(bx, by, 1, 1)
        corner(bx + bw, by, -1, 1)
        corner(bx, by + bh, 1, -1)
        corner(bx + bw, by + bh, -1, -1)

        # 顶部与底部信息条
        bar_h = 26
        ink = (243, 239, 230)
        raster.fill_rect(0, 0, w, 22, (0, 0, 0), 0.45)
        raster.text(self.label, 8, 7, ink, 2)
        locked = bool(state.get("locked"))
        mid = "LOCKED" if locked else "UNLOCKED"
        raster.text(mid, 8 + Raster.text_width(self.label, 2) + 14, 7,
                    (239, 111, 108) if locked else (125, 206, 160), 2)
        stamp = clock_of(now)
        raster.text(stamp, w - Raster.text_width(stamp, 2) - 8, 7, ink, 2)

        raster.fill_rect(0, h - bar_h, w, bar_h, (0, 0, 0), 0.5)
        raster.text(f"CH {self.channel}", 8, h - bar_h + 8, ink, 2)
        result = "FACE PASS" if passed else "FACE REJECT" if rejected else ("DETECTING" if who else "NO FACE")
        raster.text(result, w - Raster.text_width(result, 2) - 8, h - bar_h + 8, box, 2)
        if who:
            raster.text(who, cx - Raster.text_width(who, 2) / 2, h - bar_h - 26, box, 2)

        raster.noise(14, 5)
        raster.vignette(0.55)
        return raster


class StandbySource:
    """
    待机画面：通道在线但没有推流指令时推这一路。
    这样 <img src="/?stream=..."> 一直是活的连接，指令一到画面立刻切换，
    不用刷新页面 —— 和真实摄像头「未取流」的状态也一致。
    """

    def __init__(self, width: int = 512, height: int = 320) -> None:
        self.width = width
        self.height = height

    def render(self, *, now: int | None = None, channel: str = "",
               name: str = "", fps: int = 8) -> Raster:
        now = now if now is not None else int(time.time() * 1000)
        raster = Raster(self.width, self.height)
        w, h = self.width, self.height
        gold = (226, 177, 90)
        dim = (138, 150, 144)

        raster.fill_rect(0, 0, w, h, (13, 18, 17))

        # 扫描线，让待机画面也有「视频」的质感
        for y in range(0, h, 4):
            raster.fill_rect(0, y, w, 1, (255, 255, 255), 0.025)

        # 四角框
        seg = 26

        def corner(x: float, y: float, sx: int, sy: int) -> None:
            raster.line(x, y, x + sx * seg, y, gold, 2, 0.75)
            raster.line(x, y, x, y + sy * seg, gold, 2, 0.75)

        corner(10, 10, 1, 1)
        corner(w - 10, 10, -1, 1)
        corner(10, h - 10, 1, -1)
        corner(w - 10, h - 10, -1, -1)

        title = "STANDBY"
        raster.text(title, (w - Raster.text_width(title, 5)) / 2, h / 2 - 34, gold, 5)
        sub = "WAITING FOR STREAM COMMAND"
        raster.text(sub, (w - Raster.text_width(sub, 2)) / 2, h / 2 + 16, dim, 2)
        hint = 'SEND  { action: "stream", params: { on: true } }'
        raster.text(hint, (w - Raster.text_width(hint, 1)) / 2, h / 2 + 40, (96, 108, 102), 1)

        raster.text(f"CH {channel}", 20, 22, gold, 2)
        if name:
            raster.text(name, 20 + Raster.text_width(f"CH {channel}", 2) + 12, 22, dim, 2)
        stamp = clock_of(now)
        raster.text(stamp, w - Raster.text_width(stamp, 2) - 20, 22, dim, 2)
        raster.text("NO SIGNAL", w - Raster.text_width("NO SIGNAL", 2) - 20, h - 34, (96, 108, 102), 2)

        raster.vignette(0.5)
        return raster
