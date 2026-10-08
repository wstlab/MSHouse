/**
 * 主控 / 装配层
 * 职责：建立 WebSocket、分发消息、驱动场景与界面。
 * 这是唯一同时认识「三维层」和「界面层」的文件，替换任一层都不影响其它层。
 *
 * 视频：WebSocket 只承载「流信令」（通道名 / 是否在推 / 云台角），
 *       像素由 /?stream=<通道> 这个 HTTP 长连接提供，两条通道互不干扰。
 */
import { envelope, nextId } from "./protocol.js?v=17";
import { MSHouseScene } from "./scene.js?v=17";
import { Dashboard } from "./ui.js?v=17";

const OCCUPANCY = { home: "在家", away: "离家", sleep: "睡眠" };

const state = {
  devices: new Map(),
  scene: "home",
  occupancy: "home",
  streams: new Map(),
};

// 每次「打开首页」（整页加载）只欢迎一次；WebSocket 断线重连不再重复提示
let homeWelcomed = false;

// 等待口令校验 ack 的消息 id 集合
const pendingAuthAcks = new Set();

/* ---------------- 三维 + 界面 ---------------- */
/**
 * 三维层降级：如果浏览器/显卡拿不到 WebGL，控制面板与协议教学部分仍然可用，
 * 只是没有 3D 画面。课堂上遇到老机器不至于整页白屏。
 */
function makeFallbackScene(reason) {
  const canvas = document.getElementById("viewport");
  canvas.style.display = "none";
  const box = document.createElement("div");
  box.style.cssText = "position:fixed;left:16px;top:130px;max-width:520px;padding:16px 18px;border-radius:16px;background:rgba(16,24,22,.85);border:1px solid rgba(226,177,90,.4);color:#f3efe6;font-size:13px;line-height:1.6;z-index:3";
  box.innerHTML = `<b>三维视图不可用</b><br>当前环境无法创建 WebGL 上下文，设备控制 / 状态 / 报文 / 画面面板不受影响。<br><span style="color:#a7b3ab">原因：${reason}</span>`;
  document.body.appendChild(box);
  const noop = () => {};
  return {
    applySnapshot: noop, applyDeviceState: noop, setFloor: noop, setRoof: noop,
    select: noop, onSelect: noop, resize: noop, degraded: true,
  };
}

let scene;
try {
  scene = new MSHouseScene(document.getElementById("viewport"));
} catch (err) {
  console.error("[mshouse] 三维场景初始化失败：", err);
  scene = makeFallbackScene(err?.message || String(err));
}

const ui = new Dashboard({
  send: (msg) => send(msg),
  scene: (sceneId) => send(envelope("scene", { sceneId })),
  ping: () => send(envelope("ping", { at: Date.now() })),
  select: (id) => scene.select(id),
  floor: (mode) => scene.setFloor(mode),
  roof: (on) => scene.setRoof(on),
  setup: (payload) => send(envelope("setup", payload)),
  setupAuth: (password) => {
    const id = nextId("auth");
    pendingAuthAcks.add(id);
    send(envelope("setupAuth", { password }, { id }));
  },
  uploadFace: (file) => uploadFace(file),
  clearFace: () => clearFace(),
});

scene.onSelect((id) => {
  ui.highlightDevice(id);
  if (id) {
    const d = state.devices.get(id);
    ui.toast(`已选中：${d?.name || id}`);
  }
});

/* ---------------- WebSocket ---------------- */
let ws = null;
let retry = 0;
let manualClose = false;

function send(msg) {
  const full = { ...msg, id: msg.id || nextId("ui") };
  ui.addWire("out", full);
  if (ws?.readyState === 1) ws.send(JSON.stringify(full));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ui.setConnection(false, retry ? `重连中 (${retry})` : "连接中");
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    retry = 0;
    ui.setConnection(true, "已连接");
    ui.toast("已接入别墅网关");
  };

  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    ui.addWire("in", msg);
    handle(msg);
  };

  ws.onclose = () => {
    ui.setConnection(false, "已断开");
    if (manualClose) return;
    retry += 1;
    setTimeout(connect, Math.min(6000, 800 + retry * 900));
  };

  ws.onerror = () => ui.setConnection(false, "连接异常");
}

/* ---------------- 消息分发 ---------------- */
function handle(msg) {
  switch (msg.type) {
    case "hello":
      ui.toast(`网关握手成功 · ${msg.payload?.protocol || ""}`);
      break;
    case "snapshot":
      applySnapshot(msg.payload);
      break;
    case "state":
      applyDeviceState(msg.payload);
      break;
    case "scene":
      state.scene = msg.payload.sceneId;
      state.occupancy = msg.payload.occupancy || state.occupancy;
      ui.setSceneActive(msg.payload.sceneId);
      ui.setOccupancy(OCCUPANCY[state.occupancy] || state.occupancy);
      ui.toast(msg.payload.name + " 已执行");
      break;
    case "event":
      ui.addEvent(msg.payload);
      if (msg.payload.level === "alarm") ui.toast("⚠ " + msg.payload.message);
      break;
    case "video":
      applyVideoSignal(msg.payload);
      break;
    case "faceEnroll": {
      const p = msg.payload || {};
      if (p.status === "ask") ui.showEnrollPrompt(p);
      else ui.hideEnrollPrompt(p);
      break;
    }
    case "setup":
      ui.setSite(msg.payload?.site);
      scene.setSiteName?.(msg.payload?.site?.name);
      if (msg.payload?.outdoor) {
        const o = msg.payload.outdoor;
        ui.setOutdoor(`${o.temp.toFixed(1)}°C / ${Math.round(o.humidity)}%`);
      }
      ui.setSetupBusy(false);
        ui.closeSetup();
        ui.toast("设置已保存");
      break;
    case "ack":
      if (pendingAuthAcks.has(msg.id)) {
        pendingAuthAcks.delete(msg.id);
        ui.setupAuthResult(msg.payload?.ok !== false, msg.payload?.error);
        break;
      }
      if (msg.payload && msg.payload.ok === false) {
        if (/口令|经纬|天气|预报|来源|流地址|摄像头序号|留空/.test(msg.payload.error || "")) {
          ui.setSetupBusy(false, msg.payload.error);
        } else if (msg.payload.action === "enrollFace") {
          ui.enrollFailed(msg.payload.error);
        }
      }
      break;
    case "error":
      ui.toast(`网关错误：${msg.payload?.message || msg.payload?.code}`);
      break;
    default:
      break;
  }
}

function applySnapshot(snap) {
  if (!snap) return;
  state.scene = snap.scene;
  state.occupancy = snap.occupancy;
  state.devices.clear();
  for (const d of snap.devices) state.devices.set(d.id, d);

  ui.renderScenes(snap.scenes, snap.scene);
  ui.renderDevices(snap.devices);
  ui.setOccupancy(OCCUPANCY[snap.occupancy] || snap.occupancy);
  ui.setOutdoor(`${snap.outdoor.temp.toFixed(1)}°C / ${Math.round(snap.outdoor.humidity)}%`);
  ui.setSite(snap.site);
  ui.setLockSource(snap.lock?.source);
  if (snap.lock?.enroll) ui.showEnrollPrompt(snap.lock.enroll);
  else ui.hideEnrollPrompt(null);
  ui.setOnline(`${snap.devices.filter((d) => d.online).length}/${snap.devices.length}`);
  for (const e of (snap.recentEvents || []).slice().reverse()) ui.addEvent(e);
  // 设备影子自带通道信息，首屏就能把画面面板接上流
  for (const d of snap.devices) if (d.stream) applyVideoSignal({ ...d.stream, kind: d.type, deviceId: d.id });

  scene.applySnapshot(snap);
  scene.select(null);

  // 每一次访问首页：先提示「欢迎回家」，再切换到回家模式
  if (!homeWelcomed) {
    homeWelcomed = true;
    ui.welcomeHome();
    setTimeout(() => send(envelope("scene", { sceneId: "home" })), 650);
  }
}

function applyDeviceState(payload) {
  const d = state.devices.get(payload.deviceId);
  if (d) d.state = payload.state;
  scene.applyDeviceState(payload);
  ui.updateDevice(payload.deviceId, payload.state, d);
}

/* ---------------- 画面（HTTP 流） ---------------- */
/**
 * video 消息现在是「流信令」，一条报文里没有任何像素。
 * 画面由 <img src="/?stream=<通道>"> 直接从 HTTP 流里取，浏览器原生播 MJPEG。
 * 所以这里只做两件事：把通道地址交给界面层、把元数据写成一行说明。
 */
function applyVideoSignal(p) {
  if (!p?.channel) return;
  state.streams.set(p.deviceId, { channel: p.channel, live: !!p.live, port: p.port || null });
  ui.setStream(p.deviceId, {
    channel: p.channel,
    port: p.port || null,
    live: !!p.live,
    fps: p.live ? (p.fps ?? 8) : (p.idleFps ?? 2),
  });

  if (p.kind === "camera") {
    ui.setVideoMeta("camera.living", [
      p.live ? "推流中" : "待机",
      `云台 ${Math.round(p.pan ?? 0)}°`,
      p.armed ? "布防中" : "已撤防",
      p.motion ? "移动侦测触发" : "无异常",
      `订阅 ${p.viewers ?? 0}`,
      `通道 ${p.channel}`,
    ].join(" · "));
  } else if (p.kind === "lock") {
    const r = p.result === "pass" ? "识别通过" : p.result === "reject" ? "识别拒绝" : p.locked ? "已上锁" : "已开锁";
    const srcDesc = p.source
      ? (p.source.kind === "camera" ? `本机摄像头 #${p.source.target?.split("#")[1] || p.source.target || ""}`
        : p.source.kind === "stream" ? "网络视频流"
        : "静态画面")
      : null;
    const srcState = p.source && p.source.kind !== "image"
      ? (p.source.opened ? "" : `（${p.live ? "连接失败" : "未连接"}）`)
      : "";
    const phaseMap = { DAWN: "黎明", DAY: "白天", DUSK: "黄昏", NIGHT: "夜间" };
    ui.setVideoMeta("lock.entry", [
      r,
      p.person || null,
      phaseMap[p.phase] || null,
      p.facePresent ? "人脸画面" : "门外画面",
      srcDesc ? `来源：${srcDesc}${srcState}` : null,
      p.live ? "推流中" : "待机",
      `通道 ${p.channel}`,
      p.port ? `推流端口 ${p.port}` : null,
    ].filter(Boolean).join(" · "));
  }
}

/* ---------------- 大门锁：上传 / 移除人脸画面 ---------------- */
async function uploadFace(file) {
  ui.setVideoMeta("lock.entry", `正在上传「${file.name}」…`);
  try {
    const res = await fetch("/api/lock/face", {
      method: "POST",
      headers: { "Content-Type": file.type || "image/jpeg" },
      body: file,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      ui.toast("人脸画面上传失败：" + (data.error || res.status));
      return;
    }
    // 内置模型的比对结果：matched + 距离/阈值；状态行随后续 video 信令自动刷新
    if (data.recognitionReady === false) {
      ui.toast("画面已上传，但识别模型未就绪：" + (data.reason || ""));
    } else if (data.matched) {
      ui.toast(`识别通过：距离 ${data.distance} ≤ 阈值 ${data.threshold}，门锁已打开`);
    } else if (data.distance != null) {
      ui.toast(`识别拒绝：距离 ${data.distance} > 阈值 ${data.threshold}，大门保持锁定`);
    } else {
      ui.toast("画面已上传，但没有检测到正脸");
    }
  } catch (err) {
    ui.toast("人脸画面上传失败：" + err.message);
  }
}

async function clearFace() {
  try {
    const res = await fetch("/api/lock/face", { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      ui.toast("恢复门外画面失败：" + (data.error || res.status));
      return;
    }
    ui.toast("已恢复门外草坪道路画面");
  } catch (err) {
    ui.toast("恢复门外画面失败：" + err.message);
  }
}

/* ---------------- 快捷键（讲课时方便） ---------------- */
const SCENE_KEYS = ["home", "away", "sleep", "movie"];
window.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  const idx = Number(e.key) - 1;
  if (idx >= 0 && idx < SCENE_KEYS.length) {
    send(envelope("scene", { sceneId: SCENE_KEYS[idx] }));
    return;
  }
  if (e.key.toLowerCase() === "f") {
    const order = ["all", "1", "2"];
    const cur = document.querySelector(".floor-switch .chip.on")?.dataset.floor || "all";
    const next = order[(order.indexOf(cur) + 1) % order.length];
    document.querySelectorAll(".floor-switch .chip").forEach((b) => b.classList.toggle("on", b.dataset.floor === next));
    scene.setFloor(next);
  }
});

/* ---------------- 启动 ---------------- */
connect();
// 定期拉一次全量，保证长时间演示后仍然一致
setInterval(() => { if (ws?.readyState === 1) send(envelope("snapshot", {})); }, 60000);

window.__mshouse = { scene, ui, state, send };
