"""
服务端软件光栅化器（Python 版）

与原 Node 版的关键区别：**不再逐像素写**
  原版 Raster.px() 是一个像素一个像素地写 Buffer，这在 Python 里会慢 20 倍以上
  （实测全屏暗角：纯 Python 循环 78.6 ms，向量化 0.40 ms）。

所以这里把像素操作整体交给 Pillow（C 实现）：
  · 多边形 / 椭圆 / 直线 / 矩形 → ImageDraw（实测比手写扫描线快 57 倍）
  · 半透明绘制                 → ImageDraw 的 "RGBA" 模式，由 Pillow 做混合
  · 噪点 / 暗角                → ImageChops.add / multiply（C 实现）
  · 点阵文字                   → 预生成字形掩膜 + Image.paste
  · JPEG 编码                  → Pillow（实测比 jpeg-js 快 30 倍）

踩过的坑：**不要试图让 numpy 数组和 PIL.Image 共享同一块内存**。
Pillow 12 的 Image.fromarray() 会拷贝数据，np.asarray(img) 又是只读的，
两边各写各的会导致「画了半天，读出来还是空白」。所以这里只保留一块画布
（PIL.Image），numpy 仅在需要时以只读视图或整图粘贴的方式介入。

算法本身（透视投影 + 背面剔除 + 画家算法 + 朗伯光照）与原版保持一致，
几何与光照参数可直接对照 scenes.py。

分层：本文件只认识「点 / 面 / 颜色 / 字体」，不认识任何设备与协议。
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from PIL import Image, ImageChops, ImageDraw

Color = Sequence[int]
Vec3 = Sequence[float]

# ------------------------------------------------------------------ #
# 5x7 点阵字体（监控 OSD 用，只含 ASCII）
# 每个字符 7 行、每行 5 位，高位在左。
# ------------------------------------------------------------------ #
GLYPHS: dict[str, tuple[int, ...]] = {
    " ": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "D": (0x1E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1E),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "J": (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11),
    "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
    "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F),
    "3": (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    ":": (0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x00),
    ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x06, 0x06),
    ",": (0x00, 0x00, 0x00, 0x00, 0x06, 0x06, 0x0C),
    "/": (0x01, 0x02, 0x02, 0x04, 0x08, 0x08, 0x10),
    "-": (0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00),
    "+": (0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00),
    "#": (0x0A, 0x0A, 0x1F, 0x0A, 0x1F, 0x0A, 0x0A),
    "(": (0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02),
    ")": (0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08),
    "!": (0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04),
    "?": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04),
    "*": (0x00, 0x0A, 0x04, 0x1F, 0x04, 0x0A, 0x00),
    "%": (0x11, 0x01, 0x02, 0x04, 0x08, 0x10, 0x11),
    "\u00b0": (0x0C, 0x12, 0x12, 0x0C, 0x00, 0x00, 0x00),
    '"': (0x0A, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x00),
    "'": (0x04, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00),
    "{": (0x02, 0x04, 0x04, 0x08, 0x04, 0x04, 0x02),
    "}": (0x08, 0x04, 0x04, 0x02, 0x04, 0x04, 0x08),
    "[": (0x0E, 0x08, 0x08, 0x08, 0x08, 0x08, 0x0E),
    "]": (0x0E, 0x02, 0x02, 0x02, 0x02, 0x02, 0x0E),
    "=": (0x00, 0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00),
    "_": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F),
    "<": (0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02),
    ">": (0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08),
    ";": (0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x08),
}


def _clamp255(v: float) -> float:
    return 0.0 if v < 0 else 255.0 if v > 255 else v


# ------------------------------------------------------------------ #
# 像素缓冲
# ------------------------------------------------------------------ #
class Raster:
    """
    一块 RGB 画布，内部是 PIL.Image。

    所有绘制都走 Pillow：不透明用普通模式，半透明用 "RGBA" 模式由 Pillow 混合。
    需要 numpy 的地方（噪点、暗角、整图粘贴）通过 ImageChops 或整图转换完成，
    **不假设两者共享内存**（Pillow 12 的 fromarray 会拷贝）。
    """

    # 暗角图按 (宽, 高, 强度) 缓存 —— 强度只有几个取值，缓存后零成本
    _vign_cache: dict[tuple[int, int, float], Image.Image] = {}
    # 字形掩膜按 (字符, 缩放) 缓存
    _glyph_cache: dict[tuple[str, int], Image.Image] = {}

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self._img = Image.new("RGB", (width, height), (255, 255, 255))
        self._draw = ImageDraw.Draw(self._img)
        self._draw_a = ImageDraw.Draw(self._img, "RGBA")

    # ---------------- numpy 接口（只读视图 / 整图写入） ----------------

    @property
    def data(self) -> np.ndarray:
        """只读 numpy 视图，用于统计与诊断。不要试图写它。"""
        return np.asarray(self._img)

    def paste_array(self, arr: np.ndarray) -> None:
        """把一整张 numpy 图 (h, w, 3) 贴成画布内容。"""
        self._img.paste(Image.fromarray(np.ascontiguousarray(arr, dtype=np.uint8)), (0, 0))

    # ---------------- 基础绘制 ----------------

    @staticmethod
    def _rgba(c: Color, a: float) -> tuple[int, int, int, int]:
        alpha = int(round(max(0.0, min(1.0, a)) * 255))
        return (int(c[0]), int(c[1]), int(c[2]), alpha)

    def px(self, x: float, y: float, c: Color, a: float = 1.0) -> None:
        """写单个像素。仅用于零散点，批量请用 fill_rect / fill_poly。"""
        xi, yi = int(round(x)), int(round(y))
        if xi < 0 or yi < 0 or xi >= self.width or yi >= self.height:
            return
        if a >= 1:
            self._draw.point((xi, yi), fill=(int(c[0]), int(c[1]), int(c[2])))
        else:
            self._draw_a.point((xi, yi), fill=self._rgba(c, a))

    def fill_rect(self, x: float, y: float, w: float, h: float, c: Color, a: float = 1.0) -> None:
        x0 = max(0, int(round(x)))
        y0 = max(0, int(round(y)))
        x1 = min(self.width - 1, int(round(x + w)) - 1)
        y1 = min(self.height - 1, int(round(y + h)) - 1)
        if x1 < x0 or y1 < y0:
            return
        if a >= 1:
            self._draw.rectangle([x0, y0, x1, y1], fill=(int(c[0]), int(c[1]), int(c[2])))
        else:
            self._draw_a.rectangle([x0, y0, x1, y1], fill=self._rgba(c, a))

    def fill_poly(self, pts: Sequence[Sequence[float]], c: Color, a: float = 1.0) -> None:
        """填充凸多边形（屏幕坐标）。透视投影保持凸性，所以凸算法够用。"""
        if len(pts) < 3:
            return
        xy = [(float(p[0]), float(p[1])) for p in pts]
        if a >= 1:
            self._draw.polygon(xy, fill=(int(c[0]), int(c[1]), int(c[2])))
        else:
            self._draw_a.polygon(xy, fill=self._rgba(c, a))

    def ellipse(self, cx: float, cy: float, rx: float, ry: float, c: Color,
                a: float = 1.0, seg: int = 28) -> None:
        box = [cx - rx, cy - ry, cx + rx, cy + ry]
        if a >= 1:
            self._draw.ellipse(box, fill=(int(c[0]), int(c[1]), int(c[2])))
        else:
            self._draw_a.ellipse(box, fill=self._rgba(c, a))

    def line(self, x0: float, y0: float, x1: float, y1: float, c: Color,
             width: int = 1, a: float = 1.0) -> None:
        w = max(1, int(round(width)))
        if a >= 1:
            self._draw.line([(x0, y0), (x1, y1)], fill=(int(c[0]), int(c[1]), int(c[2])), width=w)
        else:
            self._draw_a.line([(x0, y0), (x1, y1)], fill=self._rgba(c, a), width=w)

    def v_gradient(self, x: float, y: float, w: float, h: float,
                   top: Color, bottom: Color, a: float = 1.0) -> None:
        """竖直渐变（用于天空、暗角等），从 y0 到 y1 在两个颜色间插值。"""
        hh = int(round(h))
        if hh <= 0:
            return
        for i in range(hh):
            t = 0.0 if hh <= 1 else i / (hh - 1)
            col = (
                top[0] + (bottom[0] - top[0]) * t,
                top[1] + (bottom[1] - top[1]) * t,
                top[2] + (bottom[2] - top[2]) * t,
            )
            self.fill_rect(x, y + i, w, 1, col, a)

    # ---------------- 文字 ----------------

    @staticmethod
    def text_width(text: str, scale: int = 2, tracking: int = 1) -> int:
        return len(text) * (5 + tracking) * scale - tracking * scale

    @classmethod
    def _glyph_mask(cls, ch: str, scale: int) -> Image.Image:
        """取字形掩膜（放大到 scale 倍），带缓存。"""
        key = (ch, scale)
        mask = cls._glyph_cache.get(key)
        if mask is not None:
            return mask
        bits_rows = GLYPHS.get(ch, GLYPHS["?"])
        block = np.zeros((7, 5), dtype=bool)
        for row in range(7):
            bits = bits_rows[row]
            if not bits:
                continue
            for col in range(5):
                if bits & (1 << (4 - col)):
                    block[row, col] = True
        big = np.kron(block, np.ones((scale, scale), dtype=bool))
        mask = Image.fromarray((big * 255).astype(np.uint8), "L")
        cls._glyph_cache[key] = mask
        return mask

    def text(self, text: str, x: float, y: float, c: Color, scale: int = 2,
             a: float = 1.0, tracking: int = 1) -> None:
        """5x7 点阵文字。字形掩膜带缓存，每字符一次 paste。"""
        cx = int(round(x))
        cy = int(round(y))
        rgb = (int(c[0]), int(c[1]), int(c[2]))
        for ch in str(text).upper():
            mask = self._glyph_mask(ch, scale)
            if mask.getbbox() is not None:
                if a >= 1:
                    self._img.paste(rgb, (cx, cy), mask)
                else:
                    box = (cx, cy, cx + mask.width, cy + mask.height)
                    solid = Image.new("RGB", mask.size, rgb)
                    region = self._img.crop(box)
                    self._img.paste(Image.blend(region, solid, max(0.0, min(1.0, a))), (cx, cy), mask)
            cx += (5 + tracking) * scale

    # ---------------- 全屏滤镜 ----------------

    def noise(self, amount: float, step: int = 7) -> None:
        """叠加传感器噪点：监控画面在低照度下的典型特征。

        做法：生成一张以 128 为中性值的噪声图，再用 ImageChops.add 的 offset
        把它整体减回去 —— 等价于「原图 + 噪声」，由 C 实现完成加法与截断。
        """
        cols = np.arange(0, self.width, step)
        if cols.size == 0:
            return
        buf = np.full((self.height, self.width, 3), 128, dtype=np.uint8)
        n = ((np.random.rand(self.height, cols.size, 1) - 0.5) * amount).astype(np.int16)
        buf[:, cols, :] = np.clip(128 + n, 0, 255).astype(np.uint8)
        out = ImageChops.add(self._img, Image.fromarray(buf), 1.0, -128)
        self._img.paste(out)

    def vignette(self, strength: float = 0.5) -> None:
        """暗角：越靠边越暗，让画面更像镜头而不是渲染图。"""
        key = (self.width, self.height, round(strength, 3))
        vimg = Raster._vign_cache.get(key)
        if vimg is None:
            cx, cy = self.width / 2, self.height / 2
            maxd = math.hypot(cx, cy)
            yy, xx = np.mgrid[0:self.height, 0:self.width]
            d = np.hypot(xx - cx, yy - cy) / maxd
            k = np.clip(1.0 - strength * d * d, 0.0, 1.0)
            gray = (k * 255).astype(np.uint8)
            vimg = Image.fromarray(np.repeat(gray[:, :, None], 3, axis=2))
            Raster._vign_cache[key] = vimg
        self._img.paste(ImageChops.multiply(self._img, vimg))

    # ---------------- 输出 ----------------

    def to_jpeg(self, quality: int = 72) -> bytes:
        buf = io.BytesIO()
        self._img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()


# ------------------------------------------------------------------ #
# 相机：世界坐标 → 相机坐标 → 屏幕坐标
# 约定：pan=0 看向 +Z，pan 增大绕 Y 轴转向 +X（与三维场景里的云台一致）。
# ------------------------------------------------------------------ #
def _cross(a: Vec3, b: Vec3) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


class Camera:
    def __init__(self, pos: Vec3, pan: float, tilt: float, fov_y: float,
                 width: int, height: int, near: float = 0.06) -> None:
        self.pos = (float(pos[0]), float(pos[1]), float(pos[2]))
        self.near = near
        self.width = width
        self.height = height
        th = math.radians(pan)
        cp, sp = math.cos(tilt), math.sin(tilt)
        self.fwd = (math.sin(th) * cp, -sp, math.cos(th) * cp)
        self.right = (math.cos(th), 0.0, -math.sin(th))
        self.up = _cross(self.fwd, self.right)
        self.f = height / 2 / math.tan(fov_y / 2)
        self.cx = width / 2
        self.cy = height / 2

    def to_camera(self, p: Vec3) -> tuple[float, float, float]:
        """世界点 → 相机空间（x 右、y 上、z 前）。"""
        dx = p[0] - self.pos[0]
        dy = p[1] - self.pos[1]
        dz = p[2] - self.pos[2]
        r, u, f = self.right, self.up, self.fwd
        return (
            dx * r[0] + dy * r[1] + dz * r[2],
            dx * u[0] + dy * u[1] + dz * u[2],
            dx * f[0] + dy * f[1] + dz * f[2],
        )

    def project(self, p: Sequence[float]) -> tuple[float, float, float]:
        """相机空间 → 屏幕坐标。"""
        k = self.f / p[2]
        return (self.cx + p[0] * k, self.cy - p[1] * k, p[2])

    def project_world(self, p: Vec3) -> tuple[float, float, float] | None:
        """世界点直接投到屏幕，返回 None 表示在近平面之后。"""
        c = self.to_camera(p)
        if c[2] < self.near:
            return None
        return self.project(c)

    def faces_camera(self, normal: Vec3) -> bool:
        """面是否朝向相机（用于背面剔除）。"""
        return normal[0] * self.fwd[0] + normal[1] * self.fwd[1] + normal[2] * self.fwd[2] < 0


def clip_near(poly: list[tuple[float, float, float]], near: float) -> list[tuple[float, float, float]]:
    """Sutherland–Hodgman 单平面裁剪：把多边形切到 z >= near 一侧。"""
    out: list[tuple[float, float, float]] = []
    n = len(poly)
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        ain, bin_ = a[2] >= near, b[2] >= near
        if ain:
            out.append(a)
        if ain != bin_:
            t = (near - a[2]) / (b[2] - a[2])
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, near))
    return out


# ------------------------------------------------------------------ #
# 光照
# ------------------------------------------------------------------ #
@dataclass
class Light:
    dir: Vec3
    color: tuple[float, float, float]
    intensity: float


def shade(base: Color, normal: Vec3, lights: Sequence[Light], ambient: float) -> tuple[int, int, int]:
    """朗伯光照：环境项 + 各光源法线方向贡献。"""
    r = base[0] * ambient
    g = base[1] * ambient
    b = base[2] * ambient
    for L in lights:
        if L.intensity <= 0:
            continue
        d = normal[0] * L.dir[0] + normal[1] * L.dir[1] + normal[2] * L.dir[2]
        if d <= 0:
            continue
        k = d * L.intensity
        r += base[0] * L.color[0] * k
        g += base[1] * L.color[1] * k
        b += base[2] * L.color[2] * k
    return (int(_clamp255(r)), int(_clamp255(g)), int(_clamp255(b)))


# ------------------------------------------------------------------ #
# 面绘制：剔除 → 裁剪 → 投影 → 着色 → 填充
# ------------------------------------------------------------------ #
@dataclass
class Face:
    pts: list[tuple[float, float, float]]
    normal: tuple[float, float, float] | None
    color: tuple[int, int, int]
    order: int = 0
    alpha: float = 1.0
    flat: bool = False


def draw_face(raster: Raster, camera: Camera, face: Face,
              lights: Sequence[Light], ambient: float) -> None:
    if face.normal is not None and not camera.faces_camera(face.normal):
        return
    poly = [camera.to_camera(p) for p in face.pts]
    if all(p[2] < camera.near for p in poly):
        return
    if any(p[2] < camera.near for p in poly):
        poly = clip_near(poly, camera.near)
        if len(poly) < 3:
            return
    scr = [camera.project(p) for p in poly]
    color = face.color if face.flat else shade(face.color, face.normal, lights, ambient)
    raster.fill_poly(scr, color, face.alpha)


def draw_scene(raster: Raster, camera: Camera, faces: Sequence[Face],
               lights: Sequence[Light], ambient: float) -> None:
    """
    画家算法绘制。

    排序键是「层级 order → 面心到相机的距离」两个字段：
      order 用来处理「贴附关系」—— 窗贴在墙上、地毯铺在地板上、家具立在地毯上。
      这些情况光靠距离排序会出错（一块大地毯的面心可能比站在它上面的沙发更近，
      于是地毯被后画、把沙发盖掉），所以贴附物显式给更大的 order。
      order 相同的面之间再按距离从远到近画。

    约定：0 = 房间壳体（地板/墙/天花板），1 = 贴附物（窗/门/地毯/灯盘），2 = 家具。
    """
    cam = camera.pos
    decorated = []
    for f in faces:
        n = len(f.pts)
        cx = sum(p[0] for p in f.pts) / n
        cy = sum(p[1] for p in f.pts) / n
        cz = sum(p[2] for p in f.pts) / n
        d = (cx - cam[0]) ** 2 + (cy - cam[1]) ** 2 + (cz - cam[2]) ** 2
        decorated.append((f.order, -d, f))
    decorated.sort(key=lambda t: (t[0], t[1]))
    for _, _, f in decorated:
        draw_face(raster, camera, f, lights, ambient)


# ------------------------------------------------------------------ #
# 几何小工具
# ------------------------------------------------------------------ #
def box_faces(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float,
              color: tuple[int, int, int], order: int = 0, alpha: float = 1.0,
              flat: bool = False) -> list[Face]:
    """由两个对角点生成一个长方体的 6 个面（法线朝外）。"""
    v = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]

    def mk(idx: Sequence[int], normal: tuple[float, float, float]) -> Face:
        return Face(pts=[v[i] for i in idx], normal=normal, color=color,
                    order=order, alpha=alpha, flat=flat)

    return [
        mk((4, 5, 6, 7), (0, 0, 1)),      # 前 (+z)
        mk((1, 0, 3, 2), (0, 0, -1)),     # 后 (-z)
        mk((5, 1, 2, 6), (1, 0, 0)),      # 右 (+x)
        mk((0, 4, 7, 3), (-1, 0, 0)),     # 左 (-x)
        mk((3, 7, 6, 2), (0, 1, 0)),      # 上 (+y)
        mk((0, 1, 5, 4), (0, -1, 0)),     # 下 (-y)
    ]


def quad(pts: Sequence[Sequence[float]], normal: Vec3, color: tuple[int, int, int],
         order: int = 0, alpha: float = 1.0, flat: bool = False) -> Face:
    """单个矩形面（顺序需按逆时针给出，法线自行指定）。"""
    return Face(pts=[(float(p[0]), float(p[1]), float(p[2])) for p in pts],
                normal=(float(normal[0]), float(normal[1]), float(normal[2])),
                color=color, order=order, alpha=alpha, flat=flat)


def kelvin(k: float) -> tuple[float, float, float]:
    """色温(K) → 归一化 RGB，和前端保持同一套近似。"""
    t = min(12000.0, max(1000.0, k)) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.47 * math.log(t) - 161.12
    else:
        r = 329.7 * (t - 60) ** -0.1332
        g = 288.12 * (t - 60) ** -0.0755
    if t >= 66:
        b = 255.0
    elif t <= 19:
        b = 0.0
    else:
        b = 138.52 * math.log(t - 10) - 305.04
    return (
        max(0.0, min(255.0, r)) / 255.0,
        max(0.0, min(255.0, g)) / 255.0,
        max(0.0, min(255.0, b)) / 255.0,
    )
