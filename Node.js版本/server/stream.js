/**
 * 视频流通道（MJPEG over HTTP）
 *
 * 设计要点：
 *   1. 画面不走 WebSocket。WebSocket 只传「流信令」（通道名、是否在推、云台角…），
 *      像素走标准 HTTP 长连接：GET /?stream=<通道名>
 *   2. 响应是 multipart/x-mixed-replace，浏览器 <img> 原生就能播，不需要 MSE / WebRTC。
 *      真实 IP 摄像头（/video.cgi、/mjpg/video.mjpg）用的就是这套，所以替换成本最低。
 *   3. 一个通道只渲染一次，广播给所有订阅者，CPU 不随观看人数增长。
 *   4. 只有存在订阅者时才启动定时器；没人看就不烧 CPU。
 *
 * 分层：本文件只认识「通道 / 订阅者 / JPEG」，不认识设备。设备状态由外部通过 context() 提供。
 */
import jpeg from "jpeg-js";

const BOUNDARY = "mshouseStream";

export class StreamHub {
  /**
   * @param {object} opts
   * @param {number} opts.fps      推流时的帧率
   * @param {number} opts.idleFps  待机画面的帧率（画面本身不变，压低省 CPU）
   * @param {number} opts.quality  JPEG 质量（1-100）
   */
  constructor({ fps = 8, idleFps = 2, quality = 72 } = {}) {
    this.fps = fps;
    this.idleFps = idleFps;
    this.quality = quality;
    this.channels = new Map();
  }

  /**
   * 注册一个通道。
   * @param {string} channel 通道名，例如 "233666"
   * @param {object} spec
   * @param {string} spec.name     人类可读名称
   * @param {string} spec.deviceId 所属设备
   * @param {object} spec.source   { render(ctx) -> Raster }，实景画面源
   * @param {Function} spec.context 返回 { state, env }，每次取帧时调用
   */
  define(channel, spec) {
    this.channels.set(String(channel), {
      channel: String(channel),
      name: spec.name || "",
      osdName: spec.osdName || spec.name || "",   // 画面 OSD 上的名字（点阵字只有 ASCII）
      deviceId: spec.deviceId || "",
      source: spec.source,
      context: spec.context || (() => ({})),
      standby: spec.standby || null,
      live: false,
      viewers: new Set(),
      timer: null,
      lastTick: 0,
      frames: 0,
      startedAt: 0,
      lastFrame: null,
    });
  }

  has(channel) {
    return this.channels.has(String(channel));
  }

  get(channel) {
    return this.channels.get(String(channel)) || null;
  }

  /** 由设备指令驱动：切换该通道的实景 / 待机画面 */
  setLive(channel, live) {
    const ch = this.get(channel);
    if (!ch || ch.live === !!live) return false;
    ch.live = !!live;
    if (ch.live) ch.startedAt = Date.now();
    if (ch.viewers.size) {
      this._tick(ch);              // 立即换帧，观众不用等下一个周期
      this._schedule(ch);
    }
    return true;
  }

  isLive(channel) {
    return !!this.get(channel)?.live;
  }

  /** 通道描述，进 snapshot / ack 用 */
  info(channel) {
    const ch = this.get(channel);
    if (!ch) return null;
    return {
      channel: ch.channel,
      name: ch.name,
      deviceId: ch.deviceId,
      live: ch.live,
      viewers: ch.viewers.size,
      fps: ch.live ? this.fps : this.idleFps,
      url: `/?stream=${ch.channel}`,
    };
  }

  list() {
    return [...this.channels.keys()].map((c) => this.info(c));
  }

  /* ---------------- HTTP 订阅 ---------------- */

  /**
   * 处理 GET /?stream=<channel>。
   * 通道不存在 → 404；存在 → 建立 multipart 长连接，直到客户端断开。
   */
  attach(req, res, channel) {
    const ch = this.get(channel);
    if (!ch) {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      res.end(`未知的视频通道：${channel}\n可用通道：${[...this.channels.keys()].join(", ")}\n`);
      return false;
    }
    if (req.method === "HEAD") {
      res.writeHead(200, { "Content-Type": `multipart/x-mixed-replace; boundary=${BOUNDARY}` });
      res.end();
      return true;
    }

    res.writeHead(200, {
      "Content-Type": `multipart/x-mixed-replace; boundary=${BOUNDARY}`,
      "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
      Pragma: "no-cache",
      "Access-Control-Allow-Origin": "*",
      "X-Stream-Channel": ch.channel,
    });

    ch.viewers.add(res);
    req.socket?.setNoDelay?.(true);

    const drop = () => {
      ch.viewers.delete(res);
      if (!ch.viewers.size) this._stop(ch);
    };
    res.on("close", drop);
    res.on("error", drop);

    this._tick(ch);
    this._schedule(ch);
    return true;
  }

  /* ---------------- 帧循环 ---------------- */

  _schedule(ch) {
    if (ch.timer || !ch.viewers.size) return;
    const delay = Math.round(1000 / (ch.live ? this.fps : this.idleFps));
    ch.timer = setTimeout(() => {
      ch.timer = null;
      this._tick(ch);
      if (ch.viewers.size) this._schedule(ch);
    }, delay);
    ch.timer.unref?.();
  }

  _stop(ch) {
    if (ch.timer) {
      clearTimeout(ch.timer);
      ch.timer = null;
    }
  }

  _tick(ch) {
    if (!ch.viewers.size) return;
    const now = Date.now();
    const dt = ch.lastTick ? Math.min(0.5, (now - ch.lastTick) / 1000) : 1 / this.fps;
    ch.lastTick = now;

    let raster;
    try {
      if (ch.live) {
        const ctx = ch.context() || {};
        raster = ch.source.render({
          state: ctx.state || {},
          env: ctx.env || {},
          now,
          dt,
          fps: this.fps,
        });
      } else if (ch.standby) {
        raster = ch.standby.render({ now, channel: ch.channel, name: ch.osdName, fps: this.fps });
      }
    } catch (err) {
      console.error(`[stream] 通道 ${ch.channel} 渲染失败：`, err.message);
      return;
    }
    if (!raster) return;

    let jpg;
    try {
      jpg = jpeg.encode({ data: raster.data, width: raster.width, height: raster.height }, this.quality).data;
    } catch (err) {
      console.error(`[stream] 通道 ${ch.channel} 编码失败：`, err.message);
      return;
    }
    ch.frames += 1;
    ch.lastFrame = jpg;
    this._broadcast(ch, jpg);
  }

  _broadcast(ch, jpg) {
    const head = `--${BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpg.length}\r\n\r\n`;
    for (const res of [...ch.viewers]) {
      if (res.writableEnded || res.destroyed) {
        ch.viewers.delete(res);
        continue;
      }
      // 背压保护：客户端跟不上就丢帧，避免服务端内存被堆满
      if (res.writableLength > 2 * 1024 * 1024) continue;
      try {
        res.write(head);
        res.write(jpg);
        res.write("\r\n");
      } catch {
        ch.viewers.delete(res);
      }
    }
  }

  dispose() {
    for (const ch of this.channels.values()) {
      this._stop(ch);
      for (const res of ch.viewers) {
        try { res.end(); } catch { /* 忽略 */ }
      }
      ch.viewers.clear();
    }
  }
}
