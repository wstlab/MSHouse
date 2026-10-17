/**
 * 浏览器摄像头推流（大门人脸锁的「浏览器摄像头」画面来源）
 * ========================================================
 *
 * 解决的问题：笔记本自带摄像头、手机摄像头、外接 USB 摄像头，浏览器
 * getUserMedia 都能拿到，但它们**没有 RTSP / HTTP 地址**，服务端没法像
 * 对待网络摄像头那样去拉流；服务端也不可能反向连进浏览器。
 *
 * 所以方向反过来 —— 由浏览器主动把画面推给网关：
 *
 *   getUserMedia  →  隐藏 <video>  →  每帧画到 canvas
 *                 →  toBlob('image/jpeg')  →  POST /api/lock/frame
 *
 * 网关收到帧后当成一路普通实时帧使用：渲染进大门锁的 MJPEG 画面，
 * 并交给 SFace 做识别。
 *
 * 两个刻意的设计：
 *   1. **像素走 HTTP 不走 WebSocket**。项目一贯的分工是「WebSocket 走控制
 *      与信令、HTTP 走像素」，摄像头的 MJPEG 就是这么出去的。保持一致，
 *      学生只需要理解一套模型。
 *   2. **串行发帧**。上一帧的 POST 没回来之前不发下一帧 —— 网络慢的时候
 *      宁可掉帧率，也不要在浏览器里堆一个越来越长的请求队列。
 */

const FRAME_INTERVAL_MS = 500;   // 目标 2fps，与大门锁默认推流帧率一致
const MAX_WIDTH = 640;           // 抓帧宽度上限：够 SFace 用，又省带宽
const JPEG_QUALITY = 0.72;
const DEVICE_KEY = "mshouse.camDevice";

export class BrowserCamera {
  /** @param {(status: object) => void} [opts.onStatus] 状态变化回调 */
  constructor({ onStatus } = {}) {
    this.onStatus = onStatus || (() => {});

    this.stream = null;
    this.deviceId = localStorage.getItem(DEVICE_KEY) || "";
    this.devices = [];
    this.error = null;
    this.frames = 0;
    this.dropped = 0;
    this.lastFrameAt = 0;
    this.pushing = false;    // 是否允许推帧（由「门锁是否在推流」决定）

    this._video = null;      // 承载 MediaStream 的隐藏元素（抓帧源）
    this._canvas = null;
    this._timer = null;
    this._sending = false;
    this._stopping = false;
    this._previews = new Set();
  }

  get running() {
    return !!this.stream;
  }

  /**
   * 开关「推帧」。摄像头本身照旧开着（设置弹窗里还要预览），
   * 只是不再往网关发 —— 免得门锁没在推流时白刷一堆 400。
   * @returns {boolean} 调用前是否在推帧（调用方据此判断「刚停」，可以顺手关摄像头）
   */
  setPushing(on) {
    const was = this.pushing;
    this.pushing = !!on;
    if (was && !this.pushing) this.error = null;
    this._emit();
    return was;
  }

  /** 有设备权限时才拿得到 label，所以列设备前会先尝试授权一次 */
  async listDevices() {
    if (!navigator.mediaDevices?.enumerateDevices) return [];
    try {
      let list = await navigator.mediaDevices.enumerateDevices();
      let cams = list.filter((d) => d.kind === "videoinput");
      if (cams.length && cams.every((d) => !d.label)) {
        // 未授权时 label 是空的：开一次流拿到权限后重新枚举
        const tmp = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        tmp.getTracks().forEach((t) => t.stop());
        list = await navigator.mediaDevices.enumerateDevices();
        cams = list.filter((d) => d.kind === "videoinput");
      }
      this.devices = cams;
      return cams;
    } catch (err) {
      this.devices = [];
      return [];
    }
  }

  setDevice(deviceId) {
    this.deviceId = deviceId || "";
    if (this.deviceId) localStorage.setItem(DEVICE_KEY, this.deviceId);
    else localStorage.removeItem(DEVICE_KEY);
  }

  /** 把预览画面挂到某个 <video> 上（同一个流可以挂到多个元素） */
  attachPreview(el) {
    if (!el) return;
    this._previews.add(el);
    el.muted = true;
    el.playsInline = true;
    el.autoplay = true;
    if (this.stream) {
      el.srcObject = this.stream;
      el.play?.().catch(() => {});
    }
  }

  detachPreview(el) {
    if (!el) return;
    this._previews.delete(el);
    el.srcObject = null;
  }

  async start() {
    if (this.stream) return true;
    if (!navigator.mediaDevices?.getUserMedia) {
      this._fail("当前浏览器不支持 getUserMedia（需要 https 或 localhost 才能访问摄像头）");
      return false;
    }
    this.error = null;
    this._emit("正在请求摄像头权限…");
    const video = this.deviceId
      ? { deviceId: { exact: this.deviceId }, width: { ideal: 1280 }, height: { ideal: 720 } }
      : { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } };
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ video, audio: false });
    } catch (err) {
      // 记着的设备 ID 失效（换了机器 / 拔了 USB 摄像头）时退回默认设备
      if (this.deviceId) {
        this.setDevice("");
        try {
          this.stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        } catch (err2) {
          this._fail(describeCameraError(err2));
          return false;
        }
      } else {
        this._fail(describeCameraError(err));
        return false;
      }
    }

    this._video = document.createElement("video");
    this._video.muted = true;
    this._video.playsInline = true;
    this._video.autoplay = true;
    this._video.srcObject = this.stream;
    try {
      await this._video.play();
    } catch {
      /* 自动播放被拦不影响抓帧，metadata 到位后 drawImage 照样能画 */
    }
    for (const el of this._previews) this.attachPreview(el);

    const track = this.stream.getVideoTracks()[0];
    const label = track?.label || "默认摄像头";
    this.devices = await this.listDevices();
    this._emit(`已开启：${label}`);

    this._canvas = document.createElement("canvas");
    this._schedule(0);
    return true;
  }

  stop() {
    this._stopping = true;
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    this._sending = false;
    if (this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());  // 停止后摄像头指示灯熄灭
      this.stream = null;
    }
    for (const el of this._previews) el.srcObject = null;
    if (this._video) this._video.srcObject = null;
    this._video = null;
    this._canvas = null;
    this.frames = 0;
    this._stopping = false;
    this._emit("已关闭浏览器摄像头");
  }

  /* ---------------- 抓帧循环 ---------------- */

  /**
   * 抓一帧当前画面（给「用摄像头抓拍登记」用）。
   *
   * 与 _tick 的区别：这里按原始分辨率抓、不做缩放宽高限制 ——
   * 登记要的是尽量清晰的整张脸，而不是推流那样压到 640 宽省带宽。
   * 摄像头没开或还没出画时返回 null，调用方负责提示。
   */
  async capture() {
    const video = this._video;
    if (!this.stream || !video || video.readyState < 2 || !video.videoWidth) return null;
    const canvas = this._canvas;
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    return await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
  }

  _schedule(delay = FRAME_INTERVAL_MS) {
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => this._tick(), delay);
  }

  async _tick() {
    if (!this.stream) return;
    const video = this._video;
    if (!video || video.readyState < 2 || !video.videoWidth) {
      this._schedule(200);
      return;
    }
    if (!this.pushing) {
      // 门锁没在推流：摄像头保持开着（预览用），但不发帧
      this._schedule(500);
      return;
    }
    if (this._sending) {
      // 上一帧还没发完：跳过这一拍（丢帧总比堆请求好）
      this.dropped += 1;
      this._schedule();
      return;
    }

    const scale = Math.min(1, MAX_WIDTH / video.videoWidth);
    const w = Math.max(2, Math.round(video.videoWidth * scale));
    const h = Math.max(2, Math.round(video.videoHeight * scale));
    const canvas = this._canvas;
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    canvas.getContext("2d").drawImage(video, 0, 0, w, h);

    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
    if (!this.stream) return;          // 发帧途中被关掉了
    if (!blob) {
      this._schedule();
      return;
    }

    this._sending = true;
    try {
      const res = await fetch("/api/lock/frame", {
        method: "POST",
        headers: { "Content-Type": "image/jpeg" },
        body: blob,
      });
      if (res.ok) {
        this.frames += 1;
        this.lastFrameAt = Date.now();
        if (this.error) this.error = null;
      } else {
        const data = await res.json().catch(() => ({}));
        this.error = data.error || `推帧被拒绝（HTTP ${res.status}）`;
      }
    } catch (err) {
      this.error = `推帧失败：${err.message}`;
    } finally {
      this._sending = false;
    }
    this._emit();
    this._schedule();
  }

  _fail(message) {
    this.error = message;
    this._emit(message);
  }

  _emit(note) {
    this.onStatus({
      running: this.running,
      pushing: this.pushing,
      frames: this.frames,
      dropped: this.dropped,
      error: this.error,
      note: note || null,
      deviceLabel: this.stream?.getVideoTracks?.()[0]?.label || null,
    });
  }
}

/** 把浏览器五花八门的 getUserMedia 报错翻译成人话 */
function describeCameraError(err) {
  const name = err?.name || "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "摄像头权限被拒绝：请在浏览器地址栏的权限设置里允许摄像头后重试";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "没有找到可用的摄像头设备";
  }
  if (name === "NotReadableError") {
    return "摄像头被其它程序占用，请关掉会议软件 / 相机后再试";
  }
  if (location.protocol === "http:" && !["localhost", "127.0.0.1"].includes(location.hostname)) {
    return "浏览器只允许 https 或 localhost 访问摄像头，请换用 https 地址打开本页";
  }
  return `打开摄像头失败：${err?.message || name || err}`;
}
