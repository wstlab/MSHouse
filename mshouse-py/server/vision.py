"""
大门锁 · 内置人脸量化模型
================================

不依赖云端、不依赖 opencv-contrib，只用 cv2 的 Haar 级联做「人脸在哪」，
再用一套确定性的特征工程把人脸「量化」成一个向量：

    图片 → Haar 检测最大人脸 → 归一化 80×80 灰度
        → HOG 梯度方向直方图（900 维）+ 低频亮度块（144 维）
        → 块内 L2 归一化、整体 L2 归一化 → 1044 维单位向量

登记照与待识别画面各算一个向量，用欧氏距离比较（取值 0~2，越小越像）：
    distance ≤ threshold  → 本人，开锁
    distance >  threshold → 不是本人，拒绝

教学点：这就是「特征向量 + 距离阈值」这一类识别系统的最小可讲版本，
工业级方案会换成深度网络的 embedding，但判定流程（注册向量、比对、阈值）完全一致。
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

FACE_SIZE = 80          # 归一化后人脸尺寸
CELLS = 10              # 80/8：HOG 网格 10×10
BINS = 9                # 梯度方向 0~180° 分 9 个 bin
LOW_FREQ = 12           # 低频亮度块 12×12
EXPAND = 0.25           # 检测框向外扩 25%，避免贴脸裁掉下巴额头
VECTOR_DIM = CELLS * CELLS * BINS + LOW_FREQ * LOW_FREQ

# 级联文件候选路径（不同安装方式目录不一样，逐一尝试）
_CASCADE_CANDIDATES = (
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml",
    cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml",
    "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
    "/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
)


@dataclass
class MatchResult:
    """一次识别的判定结果。"""

    detected: bool          # 画面里是否检测到人脸
    matched: bool           # 是否与任一登记用户匹配（检测到 + 距离达标 + 模型就绪）
    distance: float | None  # 与最近登记向量的距离（未检测到人脸时为 None）
    threshold: float
    reason: str             # 给界面 / 日志看的中文说明
    box: tuple[int, int, int, int] | None = None  # 人脸框 (x, y, w, h)，供画面标注
    person: str | None = None  # 命中的登记用户名；未命中为 None


def _load_cascade() -> cv2.CascadeClassifier | None:
    for path in _CASCADE_CANDIDATES:
        if path and Path(path).exists():
            cascade = cv2.CascadeClassifier(path)
            if not cascade.empty():
                return cascade
    return None


def _to_gray(pil_image: Image.Image) -> np.ndarray:
    gray = np.asarray(pil_image.convert("L"), dtype=np.uint8)
    # 直方图均衡：逆光 / 明暗不一时特征更稳定
    return cv2.equalizeHist(gray)


def detect_faces(gray: np.ndarray, cascade: cv2.CascadeClassifier) -> list[tuple[int, int, int, int]]:
    """返回检测到的人脸框列表 (x, y, w, h)，按面积从大到小。"""
    boxes = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48))
    boxes = [tuple(int(v) for v in b) for b in boxes]
    boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
    return boxes


def _crop_face(pil_image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    x, y, w, h = box
    W, H = pil_image.size
    pad = int(max(w, h) * EXPAND)
    left = max(0, x - pad)
    top = max(0, y - pad)
    right = min(W, x + w + pad)
    bottom = min(H, y + h + pad)
    return pil_image.crop((left, top, right, bottom))


def _hog_block(face80: np.ndarray) -> np.ndarray:
    """10×10 个 8px 格子 × 9 个梯度方向 bin，2×2 块内 L2 归一化（标准 HOG 做法）。"""
    gx = cv2.Sobel(face80, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(face80, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    ang = np.rad2deg(np.arctan2(gy, gx)) % 180.0

    cell = FACE_SIZE // CELLS
    hist = np.zeros((CELLS, CELLS, BINS), dtype=np.float32)
    for cy in range(CELLS):
        for cx in range(CELLS):
            m = mag[cy * cell:(cy + 1) * cell, cx * cell:(cx + 1) * cell].reshape(-1)
            a = ang[cy * cell:(cy + 1) * cell, cx * cell:(cx + 1) * cell].reshape(-1)
            idx = np.clip((a / (180.0 / BINS)).astype(np.int32), 0, BINS - 1)
            hist[cy, cx] = np.bincount(idx, weights=m, minlength=BINS)

    # 2×2 cell 组成一个 block，块内归一化，对光照 / 对比度变化不敏感
    blocks: list[np.ndarray] = []
    for cy in range(CELLS - 1):
        for cx in range(CELLS - 1):
            block = hist[cy:cy + 2, cx:cx + 2].reshape(-1)
            norm = float(np.linalg.norm(block)) + 1e-6
            blocks.append(block / norm)
    return np.concatenate(blocks) if blocks else hist.reshape(-1)


def embed_face(face: Image.Image) -> np.ndarray:
    """把裁好的人脸图量化为单位向量。"""
    gray = np.asarray(face.resize((FACE_SIZE, FACE_SIZE), Image.BILINEAR).convert("L"), dtype=np.uint8)
    gray = cv2.equalizeHist(gray)
    hog = _hog_block(gray)

    low = np.asarray(
        Image.fromarray(gray).resize((LOW_FREQ, LOW_FREQ), Image.BILINEAR),
        dtype=np.float32,
    ).reshape(-1)
    # 低频亮度块：去均值后 L2 归一化，只保留明暗结构，不受整体亮度影响
    low = low - low.mean()
    low /= np.linalg.norm(low) + 1e-6

    vec = np.concatenate([hog.astype(np.float32), low.astype(np.float32)])
    vec /= np.linalg.norm(vec) + 1e-6
    return vec


_ENROLL_SUFFIXES = (".jpg", ".jpeg", ".png")
# 文件名里不允许的字符（跨平台），其余（含中文）保留
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_user_stem(name: str) -> str:
    stem = _UNSAFE_NAME.sub("_", (name or "").strip()).strip(" ._")
    return stem or "user"


class FaceMatcher:
    """登记一个或多个用户的人脸，之后对任意画面做人脸比对。

    登记向量有两个来源：
      * photo_path 指向的内置登记照（教学素材 camera/face.jpg），用户名固定；
      * enroll_dir 目录下运行时「登记新用户」保存的人脸裁剪图，文件名即用户名。

    所有方法线程安全：识别在推流线程 / 上传请求线程里都可能被调用。
    """

    def __init__(self, photo_path: str | Path | None, threshold: float = 0.62,
                 enroll_dir: str | Path | None = None, primary_name: str = "登记住户"):
        self.threshold = float(threshold)
        self._lock = threading.Lock()
        self._cascade = _load_cascade()
        self._references: dict[str, np.ndarray] = {}
        self.photo_path: Path | None = Path(photo_path) if photo_path else None
        self.enroll_dir: Path | None = Path(enroll_dir) if enroll_dir else None
        self.primary_name = primary_name
        self.ready = False
        self.reason = "尚未登记人脸"
        self.reload()

    def users(self) -> list[str]:
        with self._lock:
            return list(self._references.keys())

    def reload(self) -> None:
        """（重新）读取内置登记照与登记目录，重建全部特征向量。"""
        with self._lock:
            self._references, self.reason = self._build_references()
            self.ready = bool(self._references)

    def _embed_file(self, path: Path) -> tuple[np.ndarray | None, str]:
        try:
            img = Image.open(path)
            img.load()
            img = img.convert("RGB")
        except Exception as err:
            return None, f"照片无法读取：{err}"
        gray = _to_gray(img)
        boxes = detect_faces(gray, self._cascade)
        if not boxes:
            return None, "照片里没检测到正脸"
        return embed_face(_crop_face(img, boxes[0])), "ok"

    def _build_references(self) -> tuple[dict[str, np.ndarray], str]:
        if self._cascade is None:
            return {}, "未找到 Haar 人脸级联文件"
        refs: dict[str, np.ndarray] = {}
        primary_ok = False
        if self.photo_path is not None and self.photo_path.exists():
            vec, reason = self._embed_file(self.photo_path)
            if vec is not None:
                refs[self.primary_name] = vec
                primary_ok = True
        enrolled = 0
        if self.enroll_dir is not None and self.enroll_dir.exists():
            for path in sorted(self.enroll_dir.iterdir()):
                if path.suffix.lower() not in _ENROLL_SUFFIXES or not path.is_file():
                    continue
                vec, _reason = self._embed_file(path)
                if vec is not None:
                    refs.setdefault(path.stem, vec)
                    enrolled += 1
        if refs:
            parts = []
            if primary_ok:
                parts.append(f"内置登记照 {self.photo_path.name}")
            if enrolled:
                parts.append(f"已登记用户 {enrolled} 人")
            return refs, "、".join(parts) + f"（共 {len(refs)} 人）"
        if not primary_ok and (self.photo_path is None or not self.photo_path.exists()):
            return {}, "尚未登记人脸"
        return {}, "登记照片里没检测到正脸，请换一张五官清晰的照片"

    def match(self, pil_image: Image.Image) -> MatchResult:
        """对一帧画面做人脸检测 + 与全部登记用户的距离比对，返回最接近的人。"""
        with self._lock:
            references = dict(self._references)
            threshold = self.threshold
            cascade = self._cascade

        if cascade is None:
            return MatchResult(False, False, None, threshold, "识别模型未就绪：缺少人脸级联文件")
        if not references:
            return MatchResult(False, False, None, threshold, f"识别模型未就绪：{self.reason}")

        try:
            image = pil_image.convert("RGB")
            gray = _to_gray(image)
            boxes = detect_faces(gray, cascade)
        except Exception as err:
            return MatchResult(False, False, None, threshold, f"画面解析失败：{err}")

        if not boxes:
            return MatchResult(False, False, None, threshold, "画面中未检测到人脸")

        # 画面里可能有多个人：遍历「人脸框 × 登记用户」，取距离最小的组合
        best: tuple[float, tuple[int, int, int, int], str] | None = None
        for box in boxes[:4]:
            vec = embed_face(_crop_face(image, box))
            for person, reference in references.items():
                distance = float(np.linalg.norm(vec - reference))
                if best is None or distance < best[0]:
                    best = distance, box, person
        assert best is not None
        distance, box, person = best

        if not math.isfinite(distance):
            return MatchResult(True, False, None, threshold, "特征计算异常", box)
        matched = distance <= threshold
        if matched:
            reason = f"人脸通过：{person}，相似度距离 {distance:.3f} ≤ 阈值 {threshold:.2f}"
        else:
            reason = f"未登记人脸：最近距离 {distance:.3f} > 阈值 {threshold:.2f}"
        return MatchResult(True, matched, distance, threshold, reason, box,
                           person=person if matched else None)

    def enroll(self, name: str, pil_image: Image.Image,
               box: tuple[int, int, int, int] | None = None) -> tuple[bool, str, str | None]:
        """把画面里的人脸登记为新用户：裁脸 → 存 JPEG → 向量加入登记库。

        返回 (是否成功, 说明, 最终用户名)。
        """
        if self._cascade is None:
            return False, "识别模型未就绪：缺少人脸级联文件", None
        if self.enroll_dir is None:
            return False, "网关未配置登记目录", None
        display = (name or "").strip()[:20]
        if not display:
            display = f"新住户{len(self._references) + 1}"
        with self._lock:
            # 用户名去重：已存在（含内置登记照）则加序号
            final_name = display
            seq = 2
            while final_name in self._references:
                final_name = f"{display}（{seq}）"
                seq += 1
            try:
                image = pil_image.convert("RGB")
                if box is None:
                    boxes = detect_faces(_to_gray(image), self._cascade)
                    if not boxes:
                        return False, "画面中没有检测到正脸，无法登记", None
                    box = boxes[0]
                face = _crop_face(image, box)
                self.enroll_dir.mkdir(parents=True, exist_ok=True)
                stem = safe_user_stem(final_name)
                path = self.enroll_dir / f"{stem}.jpg"
                n = 2
                while path.exists():
                    path = self.enroll_dir / f"{stem}-{n}.jpg"
                    n += 1
                face.save(path, format="JPEG", quality=90)
                self._references[final_name] = embed_face(face)
                self.ready = True
            except Exception as err:
                return False, f"登记失败：{err}", None
        return True, f"已登记用户：{final_name}", final_name
