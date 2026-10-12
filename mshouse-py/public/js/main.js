/**
 * 主控 / 装配层
 * 职责：建立 WebSocket、分发消息、驱动场景与界面。
 * 这是唯一同时认识「三维层」和「界面层」的文件，替换任一层都不影响其它层。
 *
 * 视频：WebSocket 只承载「流信令」（通道名 / 是否在推 / 云台角），
 *       像素由 /?stream=<通道> 这个 HTTP 长连接提供，两条通道互不干扰。
 */
import { envelope, nextId } from "./protocol.js?v=24";
import { MSHouseScene } from "./scene.js?v=24";
import { Dashboard } from "./ui.js?v=24";

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
// 出站序号追踪：用于发现漏收的报文并触发重新同步
let lastSeq = 0;
let resyncPending = false;

/**
 * 取访问令牌。
 *
 * 优先 URL 参数 ?token=xxx（方便直接点链接进入），拿到后存进 localStorage
 * 并从地址栏抹掉 —— 免得令牌留在浏览历史、截图、以及随手转发出去的链接里。
 */
function resolveToken() {
  const DEFAULT_TOKEN = "quickform";
  let fromUrl = "";
  try {
    const url = new URL(location.href);
    fromUrl = url.searchParams.get("token") || "";
    if (fromUrl) {
      localStorage.setItem("mshouse.token", fromUrl);
      url.searchParams.delete("token");
      history.replaceState(null, "", url.toString());
      return fromUrl;
    }
    // 默认令牌也写入 localStorage：视频流地址（ui.js streamSrc）等读取方
    // 只认 localStorage，不写会导致 /?stream= 拼不出令牌而被 401 拒绝。
    const saved = localStorage.getItem("mshouse.token");
    if (saved) return saved;
    localStorage.setItem("mshouse.token", DEFAULT_TOKEN);
    return DEFAULT_TOKEN;
  } catch {
    return fromUrl || DEFAULT_TOKEN;
  }
}

function send(msg) {
  const full = { ...msg, id: msg.id || nextId("ui") };
  ui.addWire("out", full);
  if (ws?.readyState === 1) ws.send(JSON.stringify(full));
}

/** 不记入报文面板的发送（心跳回应这类高频报文不该刷屏）。 */
function sendQuiet(msg) {
  if (ws?.readyState === 1) ws.send(JSON.stringify({ ...msg, id: nextId("hb") }));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const token = resolveToken();
  ui.setConnection(false, retry ? `重连中 (${retry})` : "连接中");
  // 浏览器不允许给 WebSocket 加自定义请求头，子协议是唯一能携带凭据的地方。
  // 这也是 v2.0 要求客户端配合改动的唯一一处。
  const protocols = token ? ["mshouse.v1", `bearer.${token}`] : ["mshouse.v1"];
  const socket = new WebSocket(`${proto}://${location.host}/ws`, protocols);
  ws = socket;

  socket.onopen = () => {
    if (ws !== socket) return;         // 已被更新的连接取代
    retry = 0;
    lastSeq = 0;
    resyncPending = false;
    ui.setConnection(true, "已连接");
    ui.toast("已接入别墅网关");
  };

  socket.onmessage = (e) => {
    if (ws !== socket) return;
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    ui.addWire("in", msg);
    handle(msg);
  };

  socket.onclose = (ev) => {
    if (ws !== socket) return;         // 旧 socket 的关闭事件，忽略
    const reason = ev?.code === 4401 ? "令牌无效"
      : ev?.code === 4426 ? "协议版本不匹配"
      : ev?.code === 4408 ? "心跳超时"
      : "已断开";
    ui.setConnection(false, reason);
    if (manualClose) return;
    if (ev?.code === 4401 || ev?.code === 4426) {
      // 令牌不对 / 版本不匹配：重连只会刷屏，提示用户去改
      ui.toast(ev.code === 4401
        ? "认证失败：点右上角「连接」填入访问令牌（令牌见服务端启动日志）"
        : "协议版本不匹配：请更新页面或网关");
      return;
    }
    retry += 1;
    setTimeout(connect, Math.min(6000, 800 + retry * 900));
  };

  socket.onerror = () => { if (ws === socket) ui.setConnection(false, "连接异常"); };
}

/** 主动断开并立即用新凭据重连（令牌变更后调用）。 */
function reconnect() {
  manualClose = true;
  try { ws?.close(); } catch { /* 忽略 */ }
  manualClose = false;
  retry = 0;
  connect();
}

/**
 * 序号连续性检查。
 *
 * 服务端为每条状态报文编号（按连接独立）。发现跳号说明中间有报文
 * 在背压时被丢弃了 —— 这时不猜「丢了什么」，直接请求一次全量对齐。
 * 增量重放需要服务端保存全部历史，代价比一次全量快照大得多。
 */
function noteSeq(payload) {
  const seq = payload?.seq;
  if (typeof seq !== "number") return;
  if (lastSeq && seq > lastSeq + 1 && !resyncPending) {
    resyncPending = true;
    ui.toast(`检测到 ${seq - lastSeq - 1} 条报文丢失，正在重新同步…`);
    send(envelope("resync", { since: lastSeq }));
  }
  lastSeq = Math.max(lastSeq, seq);
}

/* ---------------- 消息分发 ---------------- */
function handle(msg) {
  switch (msg.type) {
    case "hello":
      ui.toast(`网关握手成功 · ${msg.payload?.protocol || ""}`);
      if (msg.payload?.authRequired && msg.payload?.authed === false) {
        ui.toast("网关要求认证：请在地址后加 ?token=<访问令牌>");
      }
      break;
    case "snapshot":
      applySnapshot(msg.payload);
      break;
    case "state":
      noteSeq(msg.payload);
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
    case "fault":
      ui.addEvent({
        level: msg.payload?.severity || "warn",
        source: msg.payload?.deviceId,
        message: `故障：${msg.payload?.message || msg.payload?.faultCode}`,
        ts: msg.ts,
      });
      ui.toast(`⚠ ${msg.payload?.deviceId}：${msg.payload?.message}`);
      break;
    case "ota": {
      const p = msg.payload || {};
      const pct = Math.round((p.progress || 0) * 100);
      ui.toast(`固件升级 ${p.state} ${pct}%`);
      break;
    }
    case "ping":
      // 服务端心跳。必须回应：连续 3 次不回会被判定失联并断开连接，
      // 那样流通道也会跟着被释放。
      sendQuiet(envelope("pong", { at: Date.now() }));
      break;
    case "pong":
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
        ui.setupAuthResult(msg.payload?.ok !== false, errorText(msg.payload));
        break;
      }
      if (msg.payload && msg.payload.ok === false) {
        const code = msg.payload.errorInfo?.code;
        const text = errorText(msg.payload);
        if (code === "RATE_LIMITED") {
          ui.toast("操作过于频繁，请稍候再试");
        } else if (/口令|经纬|天气|预报|来源|流地址|摄像头序号|留空/.test(text)) {
          ui.setSetupBusy(false, text);
        } else if (msg.payload.action === "enrollFace") {
          ui.enrollFailed(text);
        } else {
          ui.toast(`${msg.payload.deviceId || "设备"}：${text}`);
        }
      }
      break;
    case "error": {
      const p = msg.payload || {};
      ui.toast(`网关错误：${p.message || p.code}`);
      if (p.code === "UNAUTHORIZED") {
        ui.setConnection(false, "未认证");
      }
      break;
    }
    default:
      break;
  }
}

/** 取错误文案：优先结构化的 errorInfo，回落到过渡期的 error 字符串。 */
function errorText(payload) {
  return payload?.errorInfo?.message || payload?.error || "";
}

function applySnapshot(snap) {
  if (!snap) return;
  state.scene = snap.scene;
  state.occupancy = snap.occupancy;
  state.devices.clear();
  for (const d of snap.devices) state.devices.set(d.id, d);
  // 全量快照到手，序号基准重置 —— 重新同步完成
  resyncPending = false;

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

/**
 * 应用一条状态报文。
 *
 * v2.0 起服务端默认只发「变化的字段」（stateKind = delta），
 * 所以这里必须先与本地状态**合并**再交给界面 ——
 * 直接覆盖会把这次没变化的字段全部抹成 undefined。
 */
function applyDeviceState(payload) {
  const d = state.devices.get(payload.deviceId);
  const kind = payload.stateKind || "full";
  let merged = payload.state;
  if (d) {
    // delta（中间值）与 confirmed（两阶段提交落地）都只携带变化字段，需合并；
    // 只有 full（快照）才是完整状态、直接覆盖。
    merged = kind === "full"
      ? { ...(payload.state || {}) }
      : { ...(d.state || {}), ...(payload.state || {}) };
    d.state = merged;
    if (payload.derived) d.derived = payload.derived;
    if (typeof payload.online === "boolean") d.online = payload.online;
  }
  scene.applyDeviceState({ ...payload, state: merged });
  ui.updateDevice(payload.deviceId, merged, d, payload.derived);
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

/* ---------------- 连接设置（v2.0：/ws 需要访问令牌） ---------------- */

const connDialog = document.getElementById("connDialog");
const connToken = document.getElementById("connToken");

document.getElementById("connBtn")?.addEventListener("click", () => {
  if (connToken) connToken.value = localStorage.getItem("mshouse.token") || "";
  connDialog?.showModal?.();
});
document.getElementById("connCancel")?.addEventListener("click", () => connDialog?.close?.());
document.getElementById("connForm")?.addEventListener("submit", () => {
  const value = connToken?.value.trim() || "";
  if (value) localStorage.setItem("mshouse.token", value);
  else localStorage.removeItem("mshouse.token");
  connDialog?.close?.();
  reconnect();
  ui.toast(value ? "令牌已保存，正在重连…" : "令牌已清除，以匿名方式重连…");
});

/* ---------------- 启动 ---------------- */
connect();
// 定期拉一次全量，保证长时间演示后仍然一致
setInterval(() => { if (ws?.readyState === 1) send(envelope("snapshot", {})); }, 60000);

window.__mshouse = { scene, ui, state, send };
