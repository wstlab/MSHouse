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
from pathlib import Path

import numpy as np
from PIL import Image

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
    "登记住户": "RESIDENT",
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
               now: int | None = None, dt: float = 0.125, fps: int = 8,
               channel: str | None = None, name: str | None = None) -> Raster:
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


def _cover_crop(img: Image.Image, width: int, height: int) -> Image.Image:
    """把任意比例的图片等比缩放后居中 cover 裁剪到目标分辨率。"""
    w, h = img.size
    scale = max(width / w, height / h)
    nw = max(width, int(round(w * scale)))
    nh = max(height, int(round(h * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    x = (nw - width) // 2
    y = (nh - height) // 2
    return img.crop((x, y, x + width, y + height))


class EntrySource:
    """
    大门人脸锁：门外视角。

    默认画面优先取 default_image 指向的图片文件（config.toml 里配置，
    教学素材是 camera/lock.jpg 拍的草坪与道路）；文件缺失时才用程序合成
    的草坪道路兜底。当网关上有「上传的人脸画面」时（env.faceImage 传入一张
    已裁好的 PIL 图），画面切换为该人脸，并叠加识别框与 OSD。
    """

    _bg_cache: dict[tuple[object, int, int], np.ndarray] = {}

    def __init__(self, width: int = 512, height: int = 320,
                 channel: str = "", label: str = "ENTRY LOCK",
                 default_image: str | Path | None = None) -> None:
        self.width = width
        self.height = height
        self.channel = channel
        self.label = label
        self.default_image = Path(default_image) if default_image else None
        # 缺失文件只告警一次，避免每帧刷日志
        self._missing_warned: set[str] = set()

    # ------------------------------------------------------------------ #
    # 外景：优先读指定场景图片（晴/雨/雪/雾/夜），读不到再程序合成兜底
    # ------------------------------------------------------------------ #
    def _background(self, image_path: str | Path | None = None) -> np.ndarray:
        path = Path(image_path) if image_path else self.default_image
        key = (str(path) if path else None, self.width, self.height)
        bg = EntrySource._bg_cache.get(key)
        if bg is not None:
            return bg

        if path is not None:
            loaded = self._background_from_file(path)
            if loaded is not None:
                EntrySource._bg_cache[key] = loaded
                return loaded

        EntrySource._bg_cache[key] = self._background_procedural()
        return EntrySource._bg_cache[key]

    def _background_from_file(self, path: Path) -> np.ndarray | None:
        try:
            with Image.open(path) as img:
                img.load()
                img = img.convert("RGB")
                if img.size != (self.width, self.height):
                    img = _cover_crop(img, self.width, self.height)
                return np.asarray(img).copy()
        except FileNotFoundError:
            if str(path) not in self._missing_warned:
                self._missing_warned.add(str(path))
                print(f"[render] 大门锁画面 {path} 不存在，改用程序合成画面")
        except Exception as err:
            if str(path) not in self._missing_warned:
                self._missing_warned.add(str(path))
                print(f"[render] 大门锁画面 {path} 读取失败（{err!r}），改用程序合成画面")
        return None

    def _background_procedural(self) -> np.ndarray:
        """程序合成的门外草坪道路：文件素材缺失时的兜底，按分辨率缓存。"""
        w, h = self.width, self.height
        r = Raster(w, h)
        horizon = int(h * 0.42)
        cx = w / 2

        # 天空：竖直渐变 + 两朵云
        r.v_gradient(0, 0, w, horizon + 1, (122, 174, 214), (212, 230, 236))
        for ex, ey, rx, ry in ((96, 40, 34, 10), (126, 32, 20, 7), (372, 50, 42, 11), (408, 42, 22, 8)):
            r.ellipse(ex, ey, rx, ry, (244, 248, 248), 0.75)

        # 远处树线：一条暗带 + 一串树冠
        r.fill_rect(0, horizon - 8, w, 12, (60, 100, 62))
        for i, x in enumerate(range(-20, w + 30, 34)):
            r.ellipse(x, horizon - 12, 26, 18, (66, 112, 66) if i % 2 else (76, 124, 72))

        # 草坪：先铺底色与剪草条纹，再撒草叶纹理
        r.fill_rect(0, horizon, w, h - horizon, (96, 142, 78))
        bands = 9
        for i in range(bands):
            y0 = horizon + (h - horizon) * i / bands
            y1 = horizon + (h - horizon) * (i + 1) / bands
            if i % 2 == 0:
                r.fill_rect(0, y0, w, y1 - y0 + 1, (88, 132, 72))
        rng = np.random.default_rng(42)
        for _ in range(320):
            gx = float(rng.integers(0, w))
            gy = float(rng.integers(horizon, h))
            r.line(gx, gy, gx - 1.0, gy - float(rng.integers(2, 6)), (78, 118, 62), 1)

        # 通向院门的道路：路缘 + 沥青面 + 透视虚线
        near_half, far_half = 118.0, 13.0
        r.fill_poly([(cx - near_half - 5, h), (cx + near_half + 5, h),
                     (cx + far_half + 2, horizon), (cx - far_half - 2, horizon)], (118, 112, 102))
        r.fill_poly([(cx - near_half, h), (cx + near_half, h),
                     (cx + far_half, horizon), (cx - far_half, horizon)], (104, 106, 108))
        for edge in (-1, 1):
            r.line(cx + edge * far_half, horizon, cx + edge * near_half, h, (226, 222, 196), 1, 0.55)
        for i in range(9):
            t = 0.05 + i * 0.115
            y = horizon + (h - horizon - 6) * t
            half = far_half + (near_half - far_half) * t
            dl = 3 + 18 * t
            dw = max(1, int(round(1 + 2.4 * t)))
            r.fill_rect(cx - dw / 2, y, dw, dl, (216, 210, 168), 0.8)

        # 院门立柱与两侧围栏
        for side in (-1, 1):
            px = cx + side * (far_half + 7)
            r.fill_rect(px - 6, horizon - 36, 12, 36, (156, 102, 74))
            r.fill_rect(px - 8, horizon - 40, 16, 5, (184, 130, 94))
            rail_x0, rail_x1 = (px + 8, w - 4) if side > 0 else (4, px - 8)
            for ry in (horizon - 24, horizon - 10):
                r.line(rail_x0, ry, rail_x1, ry, (232, 230, 214), 2, 0.7)
            posts = np.arange(rail_x0, rail_x1, 26)
            for post_x in posts:
                r.fill_rect(post_x - 1, horizon - 30, 2, 24, (122, 112, 96), 0.9)

        # 两侧的树与近处灌木丛，把道路夹在中间
        for tx in (66, w - 66):
            r.fill_rect(tx - 4, horizon - 34, 8, 40, (96, 70, 50))
            r.ellipse(tx, horizon - 48, 32, 26, (60, 106, 64))
            r.ellipse(tx - 18, horizon - 38, 18, 14, (70, 118, 70))
            r.ellipse(tx + 18, horizon - 40, 18, 15, (74, 122, 72))
        for bx0, bx1, by in ((30, 118, h - 18), (w - 118, w - 30, h - 18), (118, 176, h - 6), (w - 176, w - 118, h - 6)):
            for x in np.linspace(bx0, bx1, 4):
                r.ellipse(float(x), by, 34, 26, (54, 96, 58))
                r.ellipse(float(x) + 14, by - 8, 22, 16, (66, 110, 62))

        # 路边小花
        for t in (0.34, 0.5, 0.66, 0.84):
            y = horizon + (h - horizon) * t
            half = far_half + (near_half - far_half) * t
            for side in (-1, 1):
                r.ellipse(cx + side * (half + 12), y, 2.2, 2.2,
                          (226, 196, 84) if int(t * 100) % 2 else (228, 122, 112))

        return np.asarray(r.data).copy()

    def render(self, *, state: dict | None = None, env: dict | None = None,
               now: int | None = None, dt: float = 0.125, fps: int = 8,
               channel: str | None = None, name: str | None = None) -> Raster:
        state = state or {}
        env = env or {}
        now = now if now is not None else int(time.time() * 1000)
        raster = Raster(self.width, self.height)
        w, h = self.width, self.height

        # 画面优先级：上传的人脸 > 摄像头/视频流实时帧 > 按天气选的场景底图
        face_img = env.get("faceImage")
        frame_img = env.get("frameImage")
        face_mode = face_img is not None
        live_mode = frame_img is not None and not face_mode
        if face_mode:
            raster.paste_array(np.asarray(face_img))
        elif live_mode:
            raster.paste_array(np.asarray(frame_img))
        else:
            raster.paste_array(self._background(env.get("sceneImage")))

        # 按当地日照做光线处理：场景底图与摄像头实时帧都调色；
        # 上传的人脸画面保持原色（保证识别框里的人脸清晰可辨）
        grade = env.get("grade")
        if not face_mode and grade and len(grade) == 4:
            raster.color_grade((grade[0], grade[1], grade[2]), float(grade[3]))

        last_person = state.get("lastPerson")
        who = NAME_ASCII.get(last_person, "VISITOR") if last_person else None
        passed = state.get("lastResult") == "pass"
        rejected = state.get("lastResult") == "reject"

        box = (125, 206, 160) if passed else (239, 111, 108) if rejected else (226, 177, 90)

        # 人脸 / 实时帧：识别框 + 姓名。识别模型给了真实人脸框就按框画，
        # 否则在画面中央画一个取景准星
        cx = w / 2
        if face_mode or live_mode:
            real_box = env.get("faceBox")
            if real_box and len(real_box) == 4:
                bx, by, bw, bh = (float(v) for v in real_box)
            else:
                fy = h * 0.46
                bx, by, bw, bh = cx - 74, fy - 84, 148, 192
            seg = 22

            def corner(x: float, y: float, sx: int, sy: int) -> None:
                raster.line(x, y, x + sx * seg, y, box, 3)
                raster.line(x, y, x, y + sy * seg, box, 3)

            corner(bx, by, 1, 1)
            corner(bx + bw, by, -1, 1)
            corner(bx, by + bh, 1, -1)
            corner(bx + bw, by + bh, -1, -1)
            if who:
                raster.text(who, cx - Raster.text_width(who, 2) / 2, by - 16, box, 2)

        # 顶部 / 底部信息条（两种模式共用）
        bar_h = 26
        ink = (243, 239, 230)
        raster.fill_rect(0, 0, w, 22, (0, 0, 0), 0.45)
        raster.text(self.label, 8, 7, ink, 2)
        locked = bool(state.get("locked"))
        mid = "LOCKED" if locked else "UNLOCKED"
        mid_x = 8 + Raster.text_width(self.label, 2) + 14
        raster.text(mid, mid_x, 7,
                    (239, 111, 108) if locked else (125, 206, 160), 2)
        # 日照相位标签（DAWN / DAY / DUSK / NIGHT），夹在锁状态和时钟之间
        phase_label = str(env.get("phaseLabel") or "").strip()
        if phase_label:
            raster.text(phase_label, mid_x + Raster.text_width(mid, 2) + 14, 7,
                        (180, 210, 230), 2)
        stamp = clock_of(now)
        raster.text(stamp, w - Raster.text_width(stamp, 2) - 8, 7, ink, 2)

        raster.fill_rect(0, h - bar_h, w, bar_h, (0, 0, 0), 0.5)
        raster.text(f"CH {self.channel}", 8, h - bar_h + 8, ink, 2)
        face_detected = bool(env.get("faceDetected"))
        if passed:
            result = "FACE PASS"
        elif rejected:
            result = "FACE REJECT"
        elif face_detected:
            result = "FACE DETECTED"
        elif face_mode or live_mode:
            result = "SCANNING..."
        else:
            result = "NO FACE"
        raster.text(result, w - Raster.text_width(result, 2) - 8, h - bar_h + 8, box, 2)

        raster.noise(12 if (face_mode or live_mode) else 9, 5)
        raster.vignette(0.55 if (face_mode or live_mode) else 0.42)
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
               name: str = "", fps: int = 8,
               state: dict | None = None, env: dict | None = None,
               dt: float = 0.0) -> Raster:
        # state/env/dt 由统一帧循环传入；待机画面不关心设备状态，显式忽略
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
