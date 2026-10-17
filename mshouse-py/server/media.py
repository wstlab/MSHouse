"""
大门锁 · 外部画面源（本机摄像头 / 网络视频流 / 浏览器摄像头）
==============================================================

config.toml 里 [streams.lock_entry.source] kind 取值：

    image   → 静态天气底图（不走这里，见 gateway._lock_scene）
    camera  → cv2.VideoCapture(index)      本机 USB / 内建摄像头
    stream  → cv2.VideoCapture(rtsp/http)  门口旧摄像头、NVR 等网络视频流
    browser → 浏览器摄像头：前端 getUserMedia 抓帧后 POST 上来

VideoSource 与 BrowserSource 是同一套「鸭子接口」（is_external / latest /
status / open / close / target），网关侧完全不关心画面是拉来的还是推来的。

设计要点（本机没有摄像头时也不能让网关崩）：
  * 所有属性在 __init__ 里统一初始化，任何路径都不提前 return；
  * VideoSource 的采集放后台线程，主线程只取 latest() 的最新帧快照，绝不阻塞推流；
  * BrowserSource 没有采集线程 —— 帧是推进来的，超时未收到就视为断线；
  * 打开失败 / 读帧失败 / 断流只记状态，latest() 返回 None，调用方自动回落静态底图；
  * close() 幂等，释放设备后摄像头指示灯会熄灭。
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image

try:
    import cv2
except Exception:  # cv2 缺失时整体降级为「只有静态图」模式
    cv2 = None  # type: ignore[assignment]

REOPEN_INTERVAL = 2.0   # 断线后重连间隔（秒）

# 支持的画面来源（网关校验、初始化弹窗、config.lock.json 共用同一份清单）
SOURCE_KINDS = ("image", "camera", "stream", "browser")


def cover_resize(image: Image.Image, width: int, height: int) -> Image.Image:
    """等比缩放并居中裁切到通道分辨率（与上传人脸图的 cover 裁剪语义一致）。"""
    image = image.convert("RGB")
    w, h = image.size
    scale = max(width / w, height / h)
    new_w, new_h = max(width, round(w * scale)), max(height, round(h * scale))
    image = image.resize((new_w, new_h), Image.BILINEAR)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return image.crop((left, top, left + width, top + height))


@dataclass
class SourceStatus:
    kind: str
    opened: bool
    running: bool
    frame_count: int
    last_frame_at: float
    error: str | None
    target: str

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "opened": self.opened,
            "running": self.running,
            "frameCount": self.frame_count,
            "lastFrameAt": round(self.last_frame_at, 1) if self.last_frame_at else None,
            "error": self.error,
            "target": self.target,
        }


class VideoSource:
    """后台线程持续读帧，latest() 非阻塞返回最近一帧（PIL Image）或 None。"""

    def __init__(self, kind: str = "image", index: int = 0, url: str = "",
                 width: int = 512, height: int = 320):
        self.kind = kind if kind in ("camera", "stream") else "image"
        self.index = index
        self.url = url or ""
        self.width = width
        self.height = height

        self._cap = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frame_lock = threading.Lock()
        self._frame: Image.Image | None = None
        self._frame_count = 0
        self._last_frame_at = 0.0
        self.opened = False
        self.running = False
        self.error: str | None = None

    @property
    def is_external(self) -> bool:
        return self.kind in ("camera", "stream")

    @property
    def target(self) -> str:
        if self.kind == "camera":
            return f"本机摄像头 #{self.index}"
        if self.kind == "stream":
            return self.url
        return "静态画面"

    def status(self) -> SourceStatus:
        return SourceStatus(
            kind=self.kind, opened=self.opened, running=self.running,
            frame_count=self._frame_count, last_frame_at=self._last_frame_at,
            error=self.error, target=self.target,
        )

    def open(self) -> None:
        """启动后台采集（幂等）。静态图模式直接空操作。"""
        if not self.is_external:
            return
        if cv2 is None:
            self.error = "当前 Python 环境缺少 cv2（opencv-python），无法打开摄像头/视频流"
            print(f"[video] {self.error}", file=sys.stderr)
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name=f"video-source-{self.kind}", daemon=True
        )
        self.running = True
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        """停止采集并释放设备（幂等）。timeout 控制等待采集线程退出的最长秒数。

        timeout=0 时不等待：不在这里跨线程释放摄像头（OpenCV 不保证安全），
        由采集线程退出时自行 release，调用方立刻可以创建新的 VideoSource。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if thread is None or not thread.is_alive():
            self._release_cap()
            self._thread = None
        self.running = False
        with self._frame_lock:
            self._frame = None

    def latest(self) -> Image.Image | None:
        """非阻塞取最新一帧（已 cover 裁成通道分辨率）；没有帧时返回 None。"""
        with self._frame_lock:
            return self._frame

    # ---------------- 后台线程 ----------------

    def _release_cap(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._cap = None
        self.opened = False

    def _open_cap(self) -> bool:
        if self.kind == "camera":
            # macOS 上显式选 AVFoundation；Linux 上该常量不存在，回退默认后端
            backend = getattr(cv2, "CAP_AVFOUNDATION", None)
            cap = cv2.VideoCapture(self.index, backend) if backend is not None else cv2.VideoCapture(self.index)
        else:
            cap = cv2.VideoCapture(self.url, getattr(cv2, "CAP_FFMPEG", cv2.CAP_ANY))
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 缓冲只留 1 帧，取到的尽量是实时画面
        except Exception:
            pass
        if not cap.isOpened():
            cap.release()
            self.error = f"无法打开{self.target}（设备未连接 / 没授权 / 地址不通）"
            print(f"[video] {self.error}", file=sys.stderr)
            return False
        self._cap = cap
        self.opened = True
        self.error = None
        print(f"[video] 已打开{self.target}")
        return True

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            if self._cap is None and not self._open_cap():
                # 打开失败：等一会再试，不刷爆日志
                if self._stop.wait(REOPEN_INTERVAL):
                    break
                continue
            assert self._cap is not None
            ok, raw = self._cap.read()
            if not ok or raw is None:
                # 设备掉线：释放后走重连
                self.error = f"{self.target} 读帧中断，准备重连"
                self._release_cap()
                if self._stop.wait(REOPEN_INTERVAL):
                    break
                continue
            try:
                rgb = Image.fromarray(raw[:, :, ::-1])  # BGR → RGB
                frame = cover_resize(rgb, self.width, self.height)
                with self._frame_lock:
                    self._frame = frame
                    self._frame_count += 1
                    self._last_frame_at = time.time()
            except Exception as err:
                self.error = f"帧解析失败：{err}"
        self._release_cap()
        self.running = False


class BrowserSource:
    """浏览器摄像头：帧由浏览器 getUserMedia 抓取后 POST 上来，服务端只负责接收。

    为什么不做成「服务端去拉浏览器」：浏览器里的摄像头根本没有可访问的
    RTSP/HTTP 地址，安全模型也不允许服务端反向连接。所以方向反过来 ——
    浏览器把画布上的 JPEG 帧推给网关，网关当作一路普通的实时帧使用。

    与 VideoSource 的差异只有一点：没有采集线程。帧是推进来的，
    所以「是否在线」只能靠**帧的新鲜度**判断：
      * 超过 FRESH_SECONDS 没收到新帧 → latest() 返回 None（画面回落静态底图），
        status().opened 也变成 False，前端据此提示「摄像头未连接」。
    """

    FRESH_SECONDS = 3.0     # 多久没收到帧就算断流
    MAX_FRAME_BYTES = 2 * 1024 * 1024   # 单帧上限，正常 JPEG 只有几十 KB

    def __init__(self, width: int = 512, height: int = 320):
        self.kind = "browser"
        self.width = width
        self.height = height
        self.error: str | None = None
        self.running = False

        self._accepting = False     # 推流开关打开后才接收帧
        self._frame: Image.Image | None = None
        self._frame_lock = threading.Lock()
        self._frame_count = 0
        self._last_frame_at = 0.0

    # ---------------- 与 VideoSource 对齐的接口 ----------------

    @property
    def is_external(self) -> bool:
        return True

    @property
    def target(self) -> str:
        return "浏览器摄像头"

    @property
    def opened(self) -> bool:
        """最近 FRESH_SECONDS 内收到过帧，才算「真的连上了」。"""
        with self._frame_lock:
            return bool(self._accepting and self._frame is not None
                        and time.time() - self._last_frame_at <= self.FRESH_SECONDS)

    def status(self) -> SourceStatus:
        with self._frame_lock:
            count, last = self._frame_count, self._last_frame_at
        return SourceStatus(
            kind=self.kind, opened=self.opened, running=self.running,
            frame_count=count, last_frame_at=last, error=self.error, target=self.target,
        )

    def open(self) -> None:
        """开始接收浏览器推来的帧（幂等）。"""
        self._accepting = True
        self.running = True
        self.error = None

    def close(self, timeout: float = 3.0) -> None:
        """停止接收并丢弃当前帧（幂等）。timeout 只为与 VideoSource 保持同签名。"""
        self._accepting = False
        self.running = False
        with self._frame_lock:
            self._frame = None
            self._last_frame_at = 0.0

    def latest(self) -> Image.Image | None:
        """非阻塞取最新一帧（已 cover 裁成通道分辨率）；断流时返回 None。"""
        with self._frame_lock:
            if self._frame is None or time.time() - self._last_frame_at > self.FRESH_SECONDS:
                return None
            return self._frame

    # ---------------- 接收帧 ----------------

    def push(self, data: bytes) -> tuple[bool, str]:
        """收下一帧 JPEG。返回 (是否收下, 说明)。"""
        if not self._accepting:
            return False, "浏览器摄像头未开启：请先在「画面」面板点「推送画面」"
        if not data:
            return False, "没有收到画面数据"
        if len(data) > self.MAX_FRAME_BYTES:
            return False, f"单帧过大（上限 {self.MAX_FRAME_BYTES // 1024}KB）"
        if cv2 is None:
            return False, "当前 Python 环境缺少 cv2（opencv-python），无法解码浏览器帧"
        try:
            raw = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception as err:
            return False, f"帧解码失败：{err}"
        if raw is None:
            return False, "帧解码失败：不是有效的 JPEG / PNG 图像"
        try:
            frame = cover_resize(Image.fromarray(raw[:, :, ::-1]), self.width, self.height)
        except Exception as err:
            return False, f"帧处理失败：{err}"
        with self._frame_lock:
            self._frame = frame
            self._frame_count += 1
            self._last_frame_at = time.time()
        self.error = None
        return True, "ok"


def make_source(kind: str, index: int = 0, url: str = "",
                width: int = 512, height: int = 320) -> VideoSource | BrowserSource:
    """按 kind 造出对应的画面源对象。kind 不认识时退回静态图（返回的源不采集任何东西）。"""
    kind = str(kind or "image").strip().lower()
    if kind == "browser":
        return BrowserSource(width=width, height=height)
    return VideoSource(kind=kind, index=index, url=url, width=width, height=height)
