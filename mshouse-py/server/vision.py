"""
大门锁 · SFace 人脸识别
=========================

用 OpenCV 官方 **OpenCV Zoo** 的两个模型替换早期版本里
「Haar 级联 + HOG 手工特征」的方案（那套是教学示意，实测误识率很高）：

    检测  face_detection_yunet_2023mar.onnx         YuNet，顺便给出 5 点关键点
    识别  face_recognition_sface_2021dec_int8bq.onnx SFace，INT8 块量化版

完整链路：

    画面 → YuNet 检测人脸框 + 5 点关键点
        → FaceRecognizerSF.alignCrop 做相似变换，把人脸摆正到 112×112
        → FaceRecognizerSF.feature 得到 128 维 embedding
        → L2 归一化后与登记库做**余弦相似度**比较

距离定义（沿用「距离 ≤ 阈值即本人」的既有语义，配置项不用改写法）：

    distance = 1 − cos(embedding, 登记向量)      取值 0~2，越小越像
    distance ≤ threshold → 本人，开锁

OpenCV 官方示例给出的判定标准是「同一人余弦相似度 ≥ 0.363」
（FAR = 1e-5），换算过来就是 distance ≤ 0.637 —— 这就是默认阈值。

模型缺失时不会静默退回旧方案，而是明确报告「模型未就绪」，
并在 reason 里给出修复办法（运行 scripts/fetch_models.py）。

教学点：SFace 这种「检测 → 对齐 → 提特征 → 比距离」的流程，和早期那套
手工特征方案的**判定逻辑完全一致**，差别只在特征从哪来 —— 这正是
深度学习把人脸识别从 90% 拉到 99.9% 的地方。
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

# ---------------- 模型与常量 ----------------

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
# 按顺序尝试：INT8 块量化版优先（官方推荐，体积约为 FP32 的 1/3，精度几乎无损）
RECOGNIZER_FILES = (
    "face_recognition_sface_2021dec_int8bq.onnx",
    "face_recognition_sface_2021dec.onnx",
)

FACE_SIZE = 112          # SFace 要求的输入尺寸（112×112 BGR）
FEATURE_DIM = 128        # SFace embedding 维度
DEFAULT_THRESHOLD = 0.637  # 1 − 0.363：等价于官方示例的余弦相似度 ≥ 0.363
MIN_THRESHOLD, MAX_THRESHOLD = 0.05, 1.5

DETECT_SCORE = 0.7       # YuNet 置信度阈值
DETECT_NMS = 0.3
DETECT_TOP_K = 5000
MAX_FACES = 4            # 一帧最多比对几张脸（画面里人多时够用了）

# 对齐目标点：与 cv2.FaceRecognizerSF.alignCrop 内部使用的模板**完全一致**。
# 自己合成关键点（Haar 兜底路径）时必须用它，否则对齐出来的脸会歪。
_ALIGN_TEMPLATE = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float32)

# Haar 级联候选路径（只在 YuNet 模型缺失时才用得上，纯粹是保险丝）
_CASCADE_CANDIDATES = (
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml",
    cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml",
    "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
    "/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
)

_ENROLL_SUFFIXES = (".jpg", ".jpeg", ".png")
# 文件名里不允许的字符（跨平台），其余（含中文）保留
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_user_stem(name: str) -> str:
    stem = _UNSAFE_NAME.sub("_", (name or "").strip()).strip(" ._")
    return stem or "user"


# ---------------- 检测器 ----------------

def _load_cascade() -> cv2.CascadeClassifier | None:
    for path in _CASCADE_CANDIDATES:
        if path and Path(path).exists():
            cascade = cv2.CascadeClassifier(path)
            if not cascade.empty():
                return cascade
    return None


def _synth_landmarks(box: tuple[float, float, float, float]) -> np.ndarray:
    """把对齐模板按人脸框缩放，合成 5 点关键点（Haar 没有关键点时用）。"""
    x, y, w, h = box
    pts = np.stack([
        x + _ALIGN_TEMPLATE[:, 0] / FACE_SIZE * w,
        y + _ALIGN_TEMPLATE[:, 1] / FACE_SIZE * h,
    ], axis=1).astype(np.float32)
    return pts.reshape(-1)


def _face_row(box: tuple[float, float, float, float], score: float = 1.0) -> np.ndarray:
    """构造 alignCrop 需要的 15 维人脸行：x, y, w, h, 5×关键点, score。"""
    row = np.zeros(15, dtype=np.float32)
    row[0:4] = box
    row[4:14] = _synth_landmarks(box)
    row[14] = score
    return row


class FaceDetector:
    """YuNet 优先、Haar 级联兜底的人脸检测器。

    对外统一吐「15 维人脸行」（含关键点），这样对齐环节不用关心用的是哪个后端。
    """

    def __init__(self) -> None:
        self._yunet: cv2.FaceDetectorYN | None = None
        self._cascade: cv2.CascadeClassifier | None = None
        self._input_size: tuple[int, int] = (0, 0)
        self.backend = "none"

        model = MODELS_DIR / DETECTOR_FILE
        if model.exists():
            try:
                self._yunet = cv2.FaceDetectorYN.create(
                    str(model), "", (320, 320), DETECT_SCORE, DETECT_NMS, DETECT_TOP_K,
                )
                self.backend = "yunet"
            except Exception as err:  # 模型损坏 / OpenCV 版本过旧
                print(f"[vision] YuNet 加载失败，回落 Haar：{err!r}")
                self._yunet = None
        if self._yunet is None:
            self._cascade = _load_cascade()
            if self._cascade is not None:
                self.backend = "haar"

    @property
    def ready(self) -> bool:
        return self._yunet is not None or self._cascade is not None

    def detect(self, bgr: np.ndarray) -> list[np.ndarray]:
        """返回人脸行列表，按人脸面积从大到小。"""
        if self._yunet is not None:
            h, w = bgr.shape[:2]
            if (w, h) != self._input_size:
                self._yunet.setInputSize((w, h))
                self._input_size = (w, h)
            try:
                _retval, faces = self._yunet.detect(bgr)
            except Exception as err:
                print(f"[vision] YuNet 检测失败：{err!r}")
                return []
            if faces is None:
                return []
            rows = [np.asarray(row, dtype=np.float32).reshape(-1) for row in faces]
        elif self._cascade is not None:
            gray = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
            boxes = self._cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48))
            rows = [_face_row(tuple(float(v) for v in b)) for b in boxes]
        else:
            return []
        rows.sort(key=lambda r: float(r[2] * r[3]), reverse=True)
        return rows


# ---------------- 识别器 ----------------

class _Engine:
    """YuNet + SFace 的进程内单例。

    OpenCV 的这两个对象都带内部状态（YuNet 的输入尺寸、dnn 的输入 blob），
    多线程同时调用并不安全 —— 而识别既可能发生在推流线程，也可能发生在
    HTTP 上传请求线程。所以这里用一把锁把整段推理串行化：单帧只要几毫秒，
    串行的代价远小于偶发崩溃/错帧的代价。
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.detector = FaceDetector()
        self.recognizer: cv2.FaceRecognizerSF | None = None
        self.recognizer_file: str | None = None

        for name in RECOGNIZER_FILES:
            path = MODELS_DIR / name
            if not path.exists():
                continue
            try:
                self.recognizer = cv2.FaceRecognizerSF.create(str(path), "")
                self.recognizer_file = name
                break
            except Exception as err:
                print(f"[vision] {name} 加载失败：{err!r}")

    @property
    def ready(self) -> bool:
        return self.detector.ready and self.recognizer is not None

    @property
    def backend(self) -> str:
        det = {"yunet": "YuNet", "haar": "Haar（兜底）", "none": "无"}[self.detector.backend]
        rec = self.recognizer_file or "无"
        return f"{det} + {rec}"

    @property
    def problem(self) -> str:
        if self.detector.backend == "none":
            return f"缺少人脸检测模型 {DETECTOR_FILE}（运行 python scripts/fetch_models.py 下载）"
        if self.recognizer is None:
            return (f"缺少人脸识别模型 {' / '.join(RECOGNIZER_FILES)}"
                    "（运行 python scripts/fetch_models.py 下载）")
        return ""

    def embed(self, bgr: np.ndarray, row: np.ndarray) -> np.ndarray | None:
        """对齐 + 提特征 + L2 归一化。调用方需持有 self.lock。"""
        if self.recognizer is None:
            return None
        try:
            aligned = self.recognizer.alignCrop(bgr, row.reshape(1, -1))
            feat = np.asarray(self.recognizer.feature(aligned), dtype=np.float32).reshape(-1)
        except Exception as err:
            print(f"[vision] 特征提取失败：{err!r}")
            return None
        norm = float(np.linalg.norm(feat))
        if not math.isfinite(norm) or norm < 1e-6:
            return None
        return feat / norm

    def embed_aligned(self, bgr: np.ndarray) -> np.ndarray | None:
        """已经是对齐好的 112×112 人脸时，跳过检测直接提特征。"""
        if self.recognizer is None:
            return None
        try:
            feat = np.asarray(self.recognizer.feature(bgr), dtype=np.float32).reshape(-1)
        except Exception as err:
            print(f"[vision] 特征提取失败：{err!r}")
            return None
        norm = float(np.linalg.norm(feat))
        if not math.isfinite(norm) or norm < 1e-6:
            return None
        return feat / norm


_ENGINE: _Engine | None = None
_ENGINE_LOCK = threading.Lock()


def engine() -> _Engine:
    """懒加载单例：导入模块时不碰模型，避免拖慢网关启动。"""
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = _Engine()
    return _ENGINE


# ---------------- 识别结果 ----------------

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


def _pil_to_bgr(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("RGB"), dtype=np.uint8)[:, :, ::-1].copy()


def _box_of(row: np.ndarray) -> tuple[int, int, int, int]:
    return (int(round(float(row[0]))), int(round(float(row[1]))),
            int(round(float(row[2]))), int(round(float(row[3]))))


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / float(aw * ah + bw * bh - inter)


# ---------------- 登记库 ----------------

class FaceMatcher:
    """登记一个或多个用户的人脸，之后对任意画面做人脸比对。

    登记向量有两个来源：
      * photo_path 指向的内置登记照（教学素材 camera/face.jpg），用户名固定；
      * enroll_dir 目录下运行时「登记新用户」保存的人脸裁剪图，文件名即用户名。

    所有方法线程安全：识别在推流线程 / 上传请求线程里都可能被调用。
    """

    def __init__(self, photo_path: str | Path | None, threshold: float = DEFAULT_THRESHOLD,
                 enroll_dir: str | Path | None = None, primary_name: str = "登记住户"):
        self.threshold = float(threshold)
        self._lock = threading.Lock()
        self._references: dict[str, np.ndarray] = {}
        self.photo_path: Path | None = Path(photo_path) if photo_path else None
        self.enroll_dir: Path | None = Path(enroll_dir) if enroll_dir else None
        self.primary_name = primary_name
        self.ready = False
        self.reason = "尚未登记人脸"
        self.reload()

    # -------- 只读信息 --------

    def users(self) -> list[str]:
        with self._lock:
            return list(self._references.keys())

    def list_users(self) -> list[dict]:
        """已登记用户清单（含来源），供设置弹窗的「人脸登记」列表使用。

        kind 只有两种：
          primary  —— config.toml 里 face.photo 指向的内置登记照
          enrolled —— 现场登记保存到 enroll_dir 的人脸图（文件名即用户名）
        """
        with self._lock:
            names = list(self._references.keys())
        has_photo = self.photo_path is not None and self.photo_path.exists()
        out: list[dict] = []
        for name in names:
            if has_photo and name == self.primary_name:
                out.append({"name": name, "kind": "primary",
                            "file": self.photo_path.name})  # type: ignore[union-attr]
            else:
                out.append({"name": name, "kind": "enrolled",
                            "file": self._enroll_file(name)})
        return out

    def _enroll_file(self, name: str) -> str | None:
        """按用户名找登记目录里的文件。

        登记时若重名会加 `-2` 之类后缀，但向量表的键就是最终文件名 stem，
        所以这里用 stem 比对即可（不是拿用户名直接拼路径）。
        """
        if self.enroll_dir is None or not self.enroll_dir.exists():
            return None
        for path in sorted(self.enroll_dir.iterdir()):
            if (path.is_file() and path.suffix.lower() in _ENROLL_SUFFIXES
                    and path.stem == name):
                return path.name
        return None

    @property
    def backend(self) -> str:
        return engine().backend

    # -------- 登记库构建 --------

    def reload(self) -> None:
        """（重新）读取内置登记照与登记目录，重建全部特征向量。"""
        with self._lock:
            self._references, self.reason = self._build_references()
            self.ready = bool(self._references)

    def _embed_file(self, path: Path) -> tuple[np.ndarray | None, str]:
        try:
            img = Image.open(path)
            img.load()
            # EXIF 方向：手机拍的登记照常带旋转标记，不转正人脸会横着躺
            img = ImageOps.exif_transpose(img).convert("RGB")
        except Exception as err:
            return None, f"照片无法读取：{err}"
        bgr = _pil_to_bgr(img)
        eng = engine()
        with eng.lock:
            rows = eng.detector.detect(bgr)
            if rows:
                vec = eng.embed(bgr, rows[0])
                if vec is not None:
                    return vec, "ok"
            # 登记目录里保存的本来就是对齐好的 112×112 人脸：
            # 万一检测不到（脸太满、太糊），直接当对齐结果提特征
            if img.size == (FACE_SIZE, FACE_SIZE):
                vec = eng.embed_aligned(bgr)
                if vec is not None:
                    return vec, "ok"
        return None, "照片里没检测到正脸"

    def _build_references(self) -> tuple[dict[str, np.ndarray], str]:
        eng = engine()
        if not eng.ready:
            return {}, f"识别模型未就绪：{eng.problem}"
        refs: dict[str, np.ndarray] = {}
        primary_ok = False
        if self.photo_path is not None and self.photo_path.exists():
            vec, _reason = self._embed_file(self.photo_path)
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
        if self.photo_path is not None and self.photo_path.exists():
            return {}, "登记照片里没检测到正脸，请换一张五官清晰的照片"
        return {}, "尚未登记人脸"

    # -------- 识别 --------

    def match(self, pil_image: Image.Image) -> MatchResult:
        """对一帧画面做人脸检测 + 与全部登记用户的距离比对，返回最接近的人。"""
        with self._lock:
            references = dict(self._references)
            threshold = self.threshold

        eng = engine()
        if not eng.ready:
            return MatchResult(False, False, None, threshold, f"识别模型未就绪：{eng.problem}")
        if not references:
            return MatchResult(False, False, None, threshold, f"识别模型未就绪：{self.reason}")

        try:
            bgr = _pil_to_bgr(pil_image)
        except Exception as err:
            return MatchResult(False, False, None, threshold, f"画面解析失败：{err}")

        with eng.lock:
            rows = eng.detector.detect(bgr)
            if not rows:
                return MatchResult(False, False, None, threshold, "画面中未检测到人脸")

            # 画面里可能有多个人：遍历「人脸 × 登记用户」，取距离最小的组合
            best: tuple[float, tuple[int, int, int, int], str] | None = None
            for row in rows[:MAX_FACES]:
                vec = eng.embed(bgr, row)
                if vec is None:
                    continue
                for person, reference in references.items():
                    # 两个向量都已 L2 归一化 → 点积就是余弦相似度
                    distance = 1.0 - float(np.dot(vec, reference))
                    if best is None or distance < best[0]:
                        best = distance, _box_of(row), person

        if best is None:
            return MatchResult(True, False, None, threshold, "人脸特征计算异常")

        distance, box, person = best
        if not math.isfinite(distance):
            return MatchResult(True, False, None, threshold, "特征计算异常", box)
        distance = max(0.0, min(2.0, distance))
        matched = distance <= threshold
        if matched:
            reason = f"人脸通过：{person}，相似度距离 {distance:.3f} ≤ 阈值 {threshold:.3f}"
        else:
            reason = f"未登记人脸：最近距离 {distance:.3f} > 阈值 {threshold:.3f}"
        return MatchResult(True, matched, distance, threshold, reason, box,
                           person=person if matched else None)

    # -------- 登记新用户 --------

    def enroll(self, name: str, pil_image: Image.Image,
               box: tuple[int, int, int, int] | None = None) -> tuple[bool, str, str | None]:
        """把画面里的人脸登记为新用户：对齐裁脸 → 存 JPEG → 向量加入登记库。

        box 只是「画面上人脸大概在哪」的提示；这里会重新检测一次，
        用 YuNet 的真实关键点做对齐 —— 保证登记向量和识别时的向量同源。
        返回 (是否成功, 说明, 最终用户名)。
        """
        eng = engine()
        if not eng.ready:
            return False, f"识别模型未就绪：{eng.problem}", None
        if self.enroll_dir is None:
            return False, "网关未配置登记目录", None
        display = (name or "").strip()[:20]
        with self._lock:
            if not display:
                display = f"新住户{len(self._references) + 1}"
            # 用户名去重：已存在（含内置登记照）则加序号
            final_name = display
            seq = 2
            while final_name in self._references:
                final_name = f"{display}（{seq}）"
                seq += 1

        try:
            bgr = _pil_to_bgr(pil_image)
            with eng.lock:
                rows = eng.detector.detect(bgr)
                if not rows:
                    return False, "画面中没有检测到正脸，无法登记", None
                # 有提示框就挑和它重叠最多的人脸，否则取最大的一张
                target = rows[0]
                if box is not None:
                    target = max(rows, key=lambda r: _iou(_box_of(r), box))
                aligned = eng.recognizer.alignCrop(bgr, target.reshape(1, -1))
                vec = eng.embed_aligned(aligned)
                if vec is None:
                    return False, "人脸特征提取失败，请换个角度再试", None

            self.enroll_dir.mkdir(parents=True, exist_ok=True)
            stem = safe_user_stem(final_name)
            path = self.enroll_dir / f"{stem}.jpg"
            n = 2
            while path.exists():
                path = self.enroll_dir / f"{stem}-{n}.jpg"
                n += 1
            # 存的是对齐后的 112×112 人脸：重启后重新提特征时结果与本次一致
            Image.fromarray(np.asarray(aligned)[:, :, ::-1]).save(
                path, format="JPEG", quality=95)
            with self._lock:
                self._references[final_name] = vec
                self.ready = True
        except Exception as err:
            return False, f"登记失败：{err}", None
        return True, f"已登记用户：{final_name}", final_name

    def remove(self, name: str) -> tuple[bool, str]:
        """删除一个登记用户：删掉磁盘上的人脸图，再重建登记库。

        内置登记照（primary）删的就是 config.toml 里 face.photo 指向的那张图 ——
        用户点「删除」就是不想再认这张脸，只在内存里移除、文件却留着，
        下次重启又冒出来，反而像没生效。
        """
        name = (name or "").strip()
        if not name:
            return False, "用户名不能为空"
        with self._lock:
            if name not in self._references:
                return False, f"没有名为「{name}」的登记用户"

        target: Path | None = None
        if (name == self.primary_name and self.photo_path is not None
                and self.photo_path.exists()):
            target = self.photo_path
        elif self.enroll_dir is not None and self.enroll_dir.exists():
            for path in sorted(self.enroll_dir.iterdir()):
                if (path.is_file() and path.suffix.lower() in _ENROLL_SUFFIXES
                        and path.stem == name):
                    target = path
                    break
        if target is None:
            return False, f"找不到「{name}」对应的人脸文件"

        try:
            target.unlink()
        except Exception as err:
            return False, f"删除人脸文件失败：{err}"
        self.reload()
        return True, f"已删除登记用户「{name}」"
