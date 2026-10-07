/**
 * IoT Gateway
 * 职责：维护设备影子、执行场景、周期遥测、向所有客户端广播状态与流信令。
 * 教学点：这是“设备接入层”的模拟实现。真实项目里，MQTT/Zigbee 适配器会把
 * 物理设备映射成同样的 state / command 接口，上层不用改。
 *
 * 视频：像素不经过 WebSocket。网关只做两件事 ——
 *   ① 按 stream 指令开关通道（StreamHub）
 *   ② 把通道地址作为「信令」广播出去，客户端自己去 HTTP 流里取画面
 */
import { DEVICE_CATALOG, SCENES, SETUP_PASSWORD, envelope, deviceById } from "../shared/model.js";
import { StreamHub } from "./stream.js";
import { createLivingRoomSource, createEntrySource, createStandbySource } from "./render/scenes.js";

const FACES = [
  { id: "resident.lin", name: "林晓", role: "住户" },
  { id: "resident.chen", name: "陈舟", role: "住户" },
  { id: "guest.unknown", name: "未登记访客", role: "访客" },
];

function clamp(n, min, max) {
  return Math.min(max, Math.max(min, n));
}

function round1(n) {
  return Math.round(n * 10) / 10;
}

export class Gateway {
  constructor() {
    this.clients = new Set();
    this.scene = "away";
    this.occupancy = "away";
    this.outdoor = { temp: 28.4, humidity: 62, source: "default" };
    this.site = {
      configured: false,
      name: "",
      lat: null,
      lon: null,
      timezone: null,
      weather: null,
    };
    this.seq = 1;
    this.events = [];
    this.devices = new Map();
    this._lastSignal = new Map();
    this.hub = new StreamHub({ fps: 8, quality: 72 });
    this._defineStreams();
    this._initDevices();
    this._telemetryTimer = setInterval(() => this._tickTelemetry(), 2000);
    this._videoTimer = setInterval(() => this._tickVideo(), 200);
    this._faceTimer = null;
  }

  /**
   * 注册视频通道。通道名来自物模型，地址形如 /?stream=233666。
   * 通道存在与否和「是否在推流」是两件事：通道一直在线，推流由 stream 指令控制。
   */
  _defineStreams() {
    const standby = createStandbySource({ width: 512, height: 320 });
    const cam = deviceById("camera.living");
    const lock = deviceById("lock.entry");

    this.hub.define(cam.channel, {
      name: cam.name,                       // 中文名：进 /streams 与流信令
      osdName: "LIVING ROOM CAM",           // 画面 OSD 用英文：点阵字只有 ASCII
      deviceId: cam.id,
      source: createLivingRoomSource({ channel: cam.channel, label: "LIVING CAM" }),
      standby,
      context: () => {
        const c = this.devices.get("camera.living");
        const lamp = this.devices.get("light.living");
        const hour = new Date().getHours();
        return {
          state: c ? c.state : {},
          env: {
            night: hour < 6 || hour >= 18,
            hour,
            occupancy: this.occupancy,
            lamp: lamp
              ? { on: lamp.state.power, brightness: lamp.state.brightness, colorTemp: lamp.state.colorTemp }
              : { on: false, brightness: 0, colorTemp: 4000 },
          },
        };
      },
    });

    this.hub.define(lock.channel, {
      name: lock.name,
      osdName: "ENTRY DOOR LOCK",
      deviceId: lock.id,
      source: createEntrySource({ channel: lock.channel, label: "ENTRY LOCK" }),
      standby,
      context: () => ({ state: this.devices.get("lock.entry")?.state || {}, env: {} }),
    });
  }

  _initDevices() {
    for (const spec of DEVICE_CATALOG) {
      this.devices.set(spec.id, {
        ...spec,
        online: true,
        updatedAt: Date.now(),
        state: this._defaultState(spec),
      });
    }
    this._applyScene("away", { silent: true, source: "boot" });
  }

  _defaultState(spec) {
    switch (spec.type) {
      case "light":
        return { power: false, brightness: 0, colorTemp: 4000 };
      case "sensor":
        return {
          temperature: spec.room === "kitchen" ? 26.8 : 25.2,
          humidity: spec.room === "bath" ? 58 : 48,
          comfort: "舒适",
        };
      case "ac":
        return {
          power: false,
          mode: "cool",
          targetTemp: 26,
          fan: "auto",
          indoorTemp: 27.5,
        };
      case "camera":
        return {
          power: true,
          pan: 0,
          // 推流是独立能力：默认关闭，等 stream 指令打开
          streaming: false,
          motion: false,
          armed: true,
        };
      case "lock":
        return {
          locked: true,
          lastPerson: null,
          lastResult: "idle",
          streaming: false,
          battery: 86,
        };
      default:
        return {};
    }
  }

  attach(ws) {
    this.clients.add(ws);
    ws.send(JSON.stringify(envelope( "hello", {
      role: "gateway",
      name: "MSHouse Gateway",
      protocol: "mshouse/1.0",
    })));
    this.pushSnapshot(ws);
    this._pushEvent(ws, {
      level: "info",
      source: "gateway",
      message: "控制端已接入，已下发全量设备影子",
    });
  }

  detach(ws) {
    this.clients.delete(ws);
  }

  handle(ws, raw) {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      this._send(ws, envelope("error", { code: "BAD_JSON", message: "报文不是合法 JSON" }));
      return;
    }
    if (!msg || typeof msg.type !== "string") {
      this._send(ws, envelope("error", { code: "BAD_ENVELOPE", message: "缺少 type 字段" }));
      return;
    }
    const id = msg.id || `srv-${this.seq++}`;
    switch (msg.type) {
      case "command":
        this._onCommand(ws, msg, id);
        break;
      case "scene":
        this._onScene(ws, msg, id);
        break;
      case "ping":
        this._send(ws, envelope("pong", { echo: msg.payload || null }, { id }));
        break;
      case "snapshot":
        this.pushSnapshot(ws);
        break;
      case "setup":
        this._onSetup(ws, msg, id);
        break;
      default:
        this._send(ws, envelope("error", {
          code: "UNKNOWN_TYPE",
          message: `未识别的消息类型：${msg.type}`,
        }, { id }));
    }
  }

  _onCommand(ws, msg, id) {
    const { deviceId, action, params } = msg.payload || {};
    const device = this.devices.get(deviceId);
    if (!device) {
      this._send(ws, envelope("ack", {
        ok: false,
        deviceId,
        action,
        error: "设备不存在",
      }, { id, ref: msg.id }));
      return;
    }
    const result = this._execute(device, action, params || {});
    if (result.ok) this._syncStream(device);
    const stream = device.channel ? this.hub.info(device.channel) : null;
    this._send(ws, envelope("ack", {
      ok: result.ok,
      deviceId,
      action,
      error: result.error || null,
      state: device.state,
      stream,
    }, { id, ref: msg.id || null }));
    if (result.ok) {
      device.updatedAt = Date.now();
      this.broadcast(envelope("state", {
        deviceId,
        type: device.type,
        room: device.room,
        state: device.state,
        online: device.online,
      }));
      if (result.event) this.broadcastEvent(result.event);
    }
  }

  _execute(device, action, params) {
    const s = device.state;
    if (action === "set" && params && typeof params === "object") {
      return this._applyPatch(device, params);
    }
    switch (device.type) {
      case "light":
        if (action === "toggle") return this._applyPatch(device, { power: !s.power, brightness: s.power ? 0 : (s.brightness || 80) });
        if (action === "on") return this._applyPatch(device, { power: true, brightness: params.brightness ?? (s.brightness || 80) });
        if (action === "off") return this._applyPatch(device, { power: false, brightness: 0 });
        break;
      case "ac":
        if (action === "toggle") return this._applyPatch(device, { power: !s.power });
        if (action === "on") return this._applyPatch(device, { power: true });
        if (action === "off") return this._applyPatch(device, { power: false });
        break;
      case "camera":
        if (action === "pan") return this._applyPatch(device, { pan: params.pan });
        if (action === "nudge") return this._applyPatch(device, { pan: s.pan + (params.delta || 15) });
        if (action === "toggle") return this._applyPatch(device, { power: !s.power });
        if (action === "arm") return this._applyPatch(device, { armed: params.armed !== false });
        // 推送 / 停止画面：只切通道的实景与待机，不改设备开关
        if (action === "stream") return this._applyPatch(device, { streaming: params.on !== false });
        break;
      case "lock":
        if (action === "lock") return this._lock(device, true, params);
        if (action === "unlock") return this._lock(device, false, params);
        if (action === "face") return this._faceAuth(device, params);
        if (action === "stream") return this._applyPatch(device, { streaming: params.on !== false });
        break;
      default:
        break;
    }
    return { ok: false, error: `动作 ${action} 不适用于 ${device.type}` };
  }

  _applyPatch(device, patch) {
    const s = device.state;
    if (device.type === "light") {
      if ("power" in patch) s.power = !!patch.power;
      if ("brightness" in patch) s.brightness = clamp(Number(patch.brightness) || 0, 0, 100);
      if ("colorTemp" in patch) s.colorTemp = clamp(Number(patch.colorTemp) || 4000, 2700, 6500);
      if (!s.power) s.brightness = 0;
      if (s.power && s.brightness === 0) s.brightness = 70;
    } else if (device.type === "ac") {
      if ("power" in patch) s.power = !!patch.power;
      if ("mode" in patch && ["cool", "heat", "fan", "dry", "auto"].includes(patch.mode)) s.mode = patch.mode;
      if ("targetTemp" in patch) s.targetTemp = clamp(Number(patch.targetTemp) || 26, 16, 30);
      if ("fan" in patch && ["low", "mid", "high", "auto"].includes(patch.fan)) s.fan = patch.fan;
    } else if (device.type === "camera") {
      if ("power" in patch) s.power = !!patch.power;
      if ("streaming" in patch) s.streaming = !!patch.streaming && s.power;
      if ("pan" in patch) s.pan = ((Number(patch.pan) % 360) + 360) % 360;
      if ("armed" in patch) s.armed = !!patch.armed;
    } else if (device.type === "sensor") {
      return { ok: false, error: "传感器为只读遥测设备" };
    } else if (device.type === "lock") {
      if ("locked" in patch) return this._lock(device, !!patch.locked, patch);
      if ("streaming" in patch) s.streaming = !!patch.streaming;
    }
    return {
      ok: true,
      event: {
        level: "info",
        source: device.id,
        message: `${device.name} 状态已更新`,
      },
    };
  }

  /** 把设备影子里的 streaming 状态同步到流通道（指令与场景都从这里过一道） */
  _syncStream(device) {
    if (!device.channel) return;
    const s = device.state;
    const live = device.type === "camera" ? !!(s.streaming && s.power) : !!s.streaming;
    this.hub.setLive(device.channel, live);
  }

  _lock(device, locked, params = {}) {
    device.state.locked = locked;
    device.state.lastResult = locked ? "locked" : "unlocked";
    if (params.person) device.state.lastPerson = params.person;
    return {
      ok: true,
      event: {
        level: locked ? "warn" : "ok",
        source: device.id,
        message: locked
          ? `${device.name} 已上锁`
          : `${device.name} 已开锁${params.person ? " · " + params.person : ""}`,
      },
    };
  }

  _faceAuth(device, params = {}) {
    const face = FACES.find((f) => f.id === params.faceId) || FACES[Math.floor(Math.random() * FACES.length)];
    const pass = face.role === "住户" && params.forceFail !== true;
    device.state.lastPerson = face.name;
    device.state.lastResult = pass ? "pass" : "reject";
    if (pass) device.state.locked = false;
    const event = {
      level: pass ? "ok" : "alarm",
      source: device.id,
      message: pass
        ? `人脸通过：${face.name}（${face.role}），门锁已打开`
        : `人脸拒绝：${face.name}，大门保持锁定`,
    };
    return { ok: true, event };
  }

  async _onSetup(ws, msg, id) {
    const { password, lat, lon, name } = msg.payload || {};
    if (String(password || "") !== SETUP_PASSWORD) {
      this._send(ws, envelope("ack", { ok: false, error: "初始化口令不正确" }, { id, ref: msg.id || null }));
      this.broadcastEvent({ level: "warn", source: "setup", message: "初始化设置被拒绝：口令错误" });
      return;
    }
    const latitude = Number(lat);
    const longitude = Number(lon);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)
      || latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
      this._send(ws, envelope("ack", { ok: false, error: "经纬度超出合法范围" }, { id, ref: msg.id || null }));
      return;
    }
    try {
      const weather = await fetchWeather(latitude, longitude);
      this.site = {
        configured: true,
        name: String(name || weather.name || "").slice(0, 40),
        lat: round1(latitude),
        lon: round1(longitude),
        timezone: weather.timezone,
        weather: {
          code: weather.code,
          label: weather.label,
          wind: weather.wind,
          observedAt: weather.observedAt,
        },
        configuredAt: Date.now(),
      };
      this.outdoor = {
        temp: weather.temp,
        humidity: weather.humidity,
        source: "forecast",
        place: this.site.name,
      };
      this._seedIndoorFromOutdoor();
      this._send(ws, envelope("ack", {
        ok: true,
        site: this.site,
        outdoor: this.outdoor,
      }, { id, ref: msg.id || null }));
      this.broadcast(envelope("setup", { site: this.site, outdoor: this.outdoor }));
      this.broadcastSnapshot();
      this.broadcastEvent({
        level: "ok",
        source: "setup",
        message: `别墅定位已写入 ${this.site.name || "未命名地点"}（${this.site.lat}, ${this.site.lon}），室外 ${this.outdoor.temp.toFixed(1)}°C / ${Math.round(this.outdoor.humidity)}% · ${weather.label}`,
      });
    } catch (err) {
      this._send(ws, envelope("ack", {
        ok: false,
        error: `天气初始化失败：${err.message}`,
      }, { id, ref: msg.id || null }));
    }
  }

  /** 用室外预报给室内传感器和空调回风一个合理起点，避免定位后室内还停在默认值。 */
  _seedIndoorFromOutdoor() {
    const base = this.outdoor.temp;
    for (const device of this.devices.values()) {
      if (device.type === "ac") {
        device.state.indoorTemp = round1(base - (device.floor === 2 ? 1.1 : 0.4));
      }
      if (device.type === "sensor") {
        const cook = device.room === "kitchen" ? 1.4 : 0;
        const floorDrop = device.floor === 2 ? 1.0 : 0.3;
        device.state.temperature = round1(base - floorDrop + cook);
        device.state.humidity = round1(clamp(this.outdoor.humidity - (device.room === "bath" ? 4 : 12), 30, 90));
        const t = device.state.temperature;
        const h = device.state.humidity;
        device.state.comfort = t >= 23 && t <= 27 && h >= 40 && h <= 60 ? "舒适" : t > 28 || h > 70 ? "偏闷" : "一般";
      }
      device.updatedAt = Date.now();
    }
  }

  _onScene(ws, msg, id) {
    const sceneId = msg.payload?.sceneId;
    if (!SCENES[sceneId]) {
      this._send(ws, envelope("ack", { ok: false, error: "未知场景" }, { id }));
      return;
    }
    this._applyScene(sceneId, { source: "user" });
    this._send(ws, envelope("ack", { ok: true, sceneId }, { id }));
  }

  _applyScene(sceneId, { silent = false, source = "scene" } = {}) {
    this.scene = sceneId;
    const set = (id, patch) => {
      const d = this.devices.get(id);
      if (!d) return;
      this._applyPatch(d, patch);
      d.updatedAt = Date.now();
    };
    if (sceneId === "home") {
      this.occupancy = "home";
      set("light.living", { power: true, brightness: 85, colorTemp: 3800 });
      set("light.kitchen", { power: true, brightness: 70, colorTemp: 4200 });
      set("light.bedroom", { power: false });
      set("light.bath", { power: false });
      set("ac.living", { power: true, mode: "cool", targetTemp: 25, fan: "auto" });
      set("ac.bedroom", { power: false });
      set("camera.living", { power: true, armed: false, pan: 20 });
      set("lock.entry", { locked: false });
    } else if (sceneId === "away") {
      this.occupancy = "away";
      for (const id of ["light.living", "light.kitchen", "light.bedroom", "light.bath"]) {
        set(id, { power: false });
      }
      set("ac.living", { power: false });
      set("ac.bedroom", { power: false });
      set("camera.living", { power: true, armed: true, pan: 0 });
      set("lock.entry", { locked: true });
    } else if (sceneId === "sleep") {
      this.occupancy = "sleep";
      set("light.living", { power: false });
      set("light.kitchen", { power: false });
      set("light.bedroom", { power: true, brightness: 8, colorTemp: 2700 });
      set("light.bath", { power: false });
      set("ac.living", { power: false });
      set("ac.bedroom", { power: true, mode: "cool", targetTemp: 26, fan: "low" });
      set("camera.living", { power: true, armed: true, pan: 180 });
      set("lock.entry", { locked: true });
    } else if (sceneId === "movie") {
      this.occupancy = "home";
      set("light.living", { power: true, brightness: 12, colorTemp: 2700 });
      set("light.kitchen", { power: false });
      set("light.bedroom", { power: false });
      set("light.bath", { power: false });
      set("ac.living", { power: true, mode: "cool", targetTemp: 24, fan: "low" });
      set("camera.living", { power: true, armed: false });
    }
    // 场景只管设备开关与云台位置，不碰推流状态：流是独立会话，由 stream 指令控制
    for (const d of this.devices.values()) this._syncStream(d);
    if (!silent) {
      this.broadcast(envelope("scene", {
        sceneId,
        name: SCENES[sceneId].name,
        occupancy: this.occupancy,
        source,
      }));
      this.broadcastSnapshot();
      this.broadcastEvent({
        level: "ok",
        source: "scene",
        message: `已切换到${SCENES[sceneId].name}`,
      });
    }
  }

  _tickTelemetry() {
    const hourBias = Math.sin(Date.now() / 40000);
    const lo = this.site.configured ? -15 : 22;
    const hi = this.site.configured ? 45 : 36;
    this.outdoor.temp = round1(clamp(this.outdoor.temp + (Math.random() - 0.48) * 0.08, lo, hi));
    this.outdoor.humidity = round1(clamp(this.outdoor.humidity + (Math.random() - 0.5) * 0.35, 15, 98));

    for (const device of this.devices.values()) {
      if (device.type === "ac" && device.state.power) {
        const target = device.state.targetTemp;
        const pull = device.state.mode === "heat" ? 0.18 : device.state.mode === "fan" ? 0.02 : 0.16;
        device.state.indoorTemp = round1(device.state.indoorTemp + (target - device.state.indoorTemp) * pull + (Math.random() - 0.5) * 0.05);
      } else if (device.type === "ac") {
        device.state.indoorTemp = round1(device.state.indoorTemp + (this.outdoor.temp - device.state.indoorTemp) * 0.03);
      }
    }

    for (const device of this.devices.values()) {
      if (device.type !== "sensor") continue;
      const ac = device.room === "living"
        ? this.devices.get("ac.living")
        : device.room === "bedroom"
          ? this.devices.get("ac.bedroom")
          : null;
      const base = ac ? ac.state.indoorTemp : this.outdoor.temp - (device.floor === 2 ? 1.2 : 0.4);
      const cook = device.room === "kitchen" ? 1.6 : 0;
      const steam = device.room === "bath" && this.devices.get("light.bath").state.power ? 8 : 0;
      device.state.temperature = round1(base + cook + hourBias * 0.2 + (Math.random() - 0.5) * 0.12);
      device.state.humidity = round1(clamp(
        (device.room === "bath" ? 56 : 46) + steam + (this.outdoor.humidity - 55) * 0.15 + (Math.random() - 0.5) * 0.8,
        30,
        90,
      ));
      const t = device.state.temperature;
      const h = device.state.humidity;
      device.state.comfort = t >= 23 && t <= 27 && h >= 40 && h <= 60 ? "舒适" : t > 28 || h > 70 ? "偏闷" : "一般";
      device.updatedAt = Date.now();
      this.broadcast(envelope("state", {
        deviceId: device.id,
        type: device.type,
        room: device.room,
        state: device.state,
        online: true,
      }));
    }

    const cam = this.devices.get("camera.living");
    if (cam.state.armed && this.occupancy === "away" && Math.random() < 0.04) {
      cam.state.motion = true;
      this.broadcast(envelope("state", {
        deviceId: cam.id,
        type: cam.type,
        room: cam.room,
        state: cam.state,
        online: true,
      }));
      this.broadcastEvent({
        level: "alarm",
        source: cam.id,
        message: "客厅摄像头检测到移动（布防中）",
      });
      setTimeout(() => {
        cam.state.motion = false;
        this.broadcast(envelope("state", {
          deviceId: cam.id,
          type: cam.type,
          room: cam.room,
          state: cam.state,
          online: true,
        }));
      }, 2400);
    }
  }

  /**
   * 流信令：只在状态真的变化时推一条。
   * 这里没有任何像素 —— 画面请去 /?stream=<通道名> 取。
   * 之所以保留这条消息，是因为客户端仍需要知道「通道名 / 是否在推 / 云台角度」，
   * 才能决定什么时候去订阅流、以及往哪个角度调云台。
   */
  _tickVideo() {
    this._signalStream(this.devices.get("camera.living"), "camera");
    this._signalStream(this.devices.get("lock.entry"), "lock");
  }

  _signalStream(device, kind) {
    if (!device || !device.channel) return;
    const s = device.state;
    const key = kind === "camera"
      ? `${s.streaming}|${s.pan}|${s.armed}|${s.motion}|${s.power}`
      : `${s.streaming}|${s.locked}|${s.lastPerson}|${s.lastResult}`;
    if (this._lastSignal.get(device.id) === key) return;
    this._lastSignal.set(device.id, key);

    const info = this.hub.info(device.channel);
    this.broadcast(envelope("video", {
      deviceId: device.id,
      kind,
      channel: device.channel,
      url: info?.url || `/?stream=${device.channel}`,
      streaming: !!s.streaming,
      live: !!info?.live,
      viewers: info?.viewers ?? 0,
      fps: info?.fps ?? this.hub.fps,
      idleFps: this.hub.idleFps,
      pan: s.pan,
      armed: s.armed,
      motion: s.motion,
      locked: s.locked,
      person: s.lastPerson,
      result: s.lastResult,
      occupancy: this.occupancy,
      frame: Date.now(),
    }));
  }

  snapshot() {
    return {
      scene: this.scene,
      occupancy: this.occupancy,
      outdoor: this.outdoor,
      site: this.site,
      devices: [...this.devices.values()].map((d) => ({
        id: d.id,
        type: d.type,
        name: d.name,
        room: d.room,
        floor: d.floor,
        capabilities: d.capabilities,
        channel: d.channel || null,
        stream: d.channel ? this.hub.info(d.channel) : null,
        online: d.online,
        updatedAt: d.updatedAt,
        state: d.state,
      })),
      streams: this.hub.list(),
      scenes: SCENES,
      recentEvents: this.events.slice(0, 12),
    };
  }

  pushSnapshot(ws) {
    this._send(ws, envelope("snapshot", this.snapshot()));
  }

  broadcastSnapshot() {
    this.broadcast(envelope("snapshot", this.snapshot()));
  }

  broadcastEvent(event) {
    const full = {
      id: `evt-${this.seq++}`,
      ts: Date.now(),
      ...event,
    };
    this.events.unshift(full);
    this.events = this.events.slice(0, 80);
    this.broadcast(envelope("event", full));
  }

  _pushEvent(ws, event) {
    this._send(ws, envelope("event", { id: `evt-${this.seq++}`, ts: Date.now(), ...event }));
  }

  broadcast(msg) {
    const data = typeof msg === "string" ? msg : JSON.stringify(msg);
    for (const ws of this.clients) {
      if (ws.readyState === 1) ws.send(data);
    }
  }

  _send(ws, msg) {
    if (ws.readyState === 1) ws.send(JSON.stringify(msg));
  }

  dispose() {
    clearInterval(this._telemetryTimer);
    clearInterval(this._videoTimer);
    this.hub.dispose();
  }
}

export function knownDevice(id) {
  return deviceById(id);
}

const WMO = {
  0: "晴", 1: "大部晴朗", 2: "多云", 3: "阴",
  45: "雾", 48: "雾凇",
  51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
  61: "小雨", 63: "中雨", 65: "大雨",
  71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
  80: "阵雨", 81: "中阵雨", 82: "强阵雨",
  85: "阵雪", 86: "强阵雪",
  95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴",
};

/**
 * 用公开预报接口取当前位置的实况，作为室外温度初值。
 * 教学点：数字孪生的“环境边界条件”应来自外部系统，而不是写死在网关里。
 */
export async function fetchWeather(lat, lon) {
  const q = new URLSearchParams({
    latitude: String(lat),
    longitude: String(lon),
    current: "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
    timezone: "auto",
  });
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  let res;
  try {
    res = await fetch(`https://api.open-meteo.com/v1/forecast?${q}`, {
      signal: ctrl.signal,
      headers: { "User-Agent": "mshouse/1.1 (teaching)" },
    });
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) throw new Error(`预报服务返回 ${res.status}`);
  const data = await res.json();
  const cur = data.current;
  if (!cur || !Number.isFinite(cur.temperature_2m)) throw new Error("预报数据缺少温度");
  const code = cur.weather_code;
  return {
    temp: round1(cur.temperature_2m),
    humidity: round1(cur.relative_humidity_2m ?? 50),
    code,
    label: WMO[code] || `天气码 ${code}`,
    wind: round1(cur.wind_speed_10m ?? 0),
    timezone: data.timezone || null,
    observedAt: cur.time || null,
    name: "",
  };
}
