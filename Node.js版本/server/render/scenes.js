/**
 * 服务端画面合成：把设备状态画成「监控画面」。
 *
 * 两路画面：
 *   createLivingRoomSource —— 客厅云台。用软渲染把客厅的家具几何真正透视投影出来，
 *                             所以转云台、开灯、布防，画面都会跟着变。
 *   createEntrySource      —— 大门人脸锁。门外视角的 2D 合成（人脸 + 识别框 + OSD）。
 *
 * 分层：本文件认识「设备状态」，不认识 HTTP / WebSocket。
 *       它只吐出一个 Raster，编码与分发交给 ../stream.js。
 *
 * 几何与前端三维场景（public/js/scene.js）用的是同一套房间尺寸与家具坐标，
 * 方便课堂上对照讲解「同一份物模型，两种呈现」。
 */
import { Raster, Camera, drawScene, boxFaces, quad, kelvin } from "./raster.js";

/* ------------------------------------------------------------------ *
 * 客厅尺寸与家具（与 scene.js 对齐，单位：米）
 * ------------------------------------------------------------------ */
const X0 = -6, X1 = 1.5;        // 客厅西墙 / 客厅与厨房的分隔墙
const Z0 = -4.5, Z1 = 4.5;      // 北墙 / 南墙（前墙，带入户门）
const CEIL = 3.0;
// 云台光心。高度取 2.72m（贴近天花板）：装机位置越高、视线越陡，
// 越能越过电视与茶几看到沙发 —— 真实云台装墙角也是这个道理。
const CAM_POS = [-0.6, 2.72, -4.08];
const CAM_TILT = 0.3;                   // 下俯角（弧度），约 17°
const FOV = (58 * Math.PI) / 180;

const C = {
  floor: [124, 90, 60],
  wall: [206, 198, 182],
  wallSide: [192, 188, 180],
  ceil: [230, 226, 218],
  rug: [70, 82, 94],
  fabric: [104, 116, 108],
  fabric2: [126, 134, 122],
  wood: [140, 100, 64],
  dark: [26, 31, 36],
  green: [86, 138, 90],
  metal: [148, 154, 158],
  white: [224, 224, 220],
  door: [98, 68, 44],
  frame: [168, 158, 142],
};

const SKY_DAY = [156, 194, 216];
const SKY_NIGHT = [20, 30, 46];

/** OSD 只能画 ASCII，中文名在这里转写一次 */
const NAME_ASCII = {
  "林晓": "LIN XIAO",
  "陈舟": "CHEN ZHOU",
  "未登记访客": "UNKNOWN GUEST",
};

function clockOf(ts) {
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 云台角做指数缓动，避免指令一到位画面瞬移 */
function easePan(smooth, target, dt) {
  if (!smooth.inited) {
    smooth.pan = target;
    smooth.inited = true;
  }
  const delta = (((target - smooth.pan) % 360) + 540) % 360 - 180;
  smooth.pan = (smooth.pan + delta * Math.min(1, dt * 5) + 360) % 360;
  return smooth.pan;
}

/* ------------------------------------------------------------------ *
 * 客厅几何
 * ------------------------------------------------------------------ */
/** 绘制层级：0 壳体 → 1 贴附物 → 2 家具（详见 raster.js 的 drawScene） */
const L_FIX = 1;
const L_FURN = 2;

function buildLivingFaces(night, lampOn) {
  const faces = [];
  const sky = night ? SKY_NIGHT : SKY_DAY;
  const fix = { order: L_FIX };
  const furn = { order: L_FURN };

  // 壳体：地面 / 天花板 / 四面墙（法线朝房间内）
  faces.push(quad([[X0, 0, Z0], [X1, 0, Z0], [X1, 0, Z1], [X0, 0, Z1]], [0, 1, 0], C.floor));
  faces.push(quad([[X0, CEIL, Z0], [X0, CEIL, Z1], [X1, CEIL, Z1], [X1, CEIL, Z0]], [0, -1, 0], C.ceil));
  faces.push(quad([[X0, 0, Z0], [X1, 0, Z0], [X1, CEIL, Z0], [X0, CEIL, Z0]], [0, 0, 1], C.wall));
  faces.push(quad([[X0, 0, Z1], [X0, CEIL, Z1], [X1, CEIL, Z1], [X1, 0, Z1]], [0, 0, -1], C.wall));
  faces.push(quad([[X0, 0, Z0], [X0, CEIL, Z0], [X0, CEIL, Z1], [X0, 0, Z1]], [1, 0, 0], C.wallSide));
  faces.push(quad([[X1, 0, Z0], [X1, 0, Z1], [X1, CEIL, Z1], [X1, CEIL, Z0]], [-1, 0, 0], C.wallSide));

  // 贴附物：南墙的窗与入户门、西墙的窗、东墙的门洞、吸顶灯盘、地毯
  // （略微内缩，避免与墙面同深度导致前后关系不确定）
  faces.push(quad([[-1.0, 0.95, Z1 - 0.04], [1.1, 0.95, Z1 - 0.04], [1.1, 2.35, Z1 - 0.04], [-1.0, 2.35, Z1 - 0.04]], [0, 0, -1], sky, { ...fix, flat: true }));
  faces.push(quad([[-4.02, 0, Z1 - 0.05], [-2.78, 0, Z1 - 0.05], [-2.78, 2.15, Z1 - 0.05], [-4.02, 2.15, Z1 - 0.05]], [0, 0, -1], C.door, fix));
  faces.push(quad([[X0 + 0.04, 0.95, -2.1], [X0 + 0.04, 2.35, -2.1], [X0 + 0.04, 2.35, 0.5], [X0 + 0.04, 0.95, 0.5]], [1, 0, 0], sky, { ...fix, flat: true }));
  faces.push(quad([[X1 - 0.04, 0, -1.7], [X1 - 0.04, 0, -0.2], [X1 - 0.04, 2.2, -0.2], [X1 - 0.04, 2.2, -1.7]], [-1, 0, 0], night ? [16, 18, 20] : [52, 54, 56], { ...fix, flat: true }));
  faces.push(quad([[-3.4, CEIL - 0.06, 0.0], [-2.4, CEIL - 0.06, 0.0], [-2.4, CEIL - 0.06, 1.0], [-3.4, CEIL - 0.06, 1.0]], [0, -1, 0], lampOn ? [255, 246, 226] : [196, 192, 184], { ...fix, flat: lampOn }));
  faces.push(quad([[-4.9, 0.06, -0.9], [-0.9, 0.06, -0.9], [-0.9, 0.06, 1.9], [-4.9, 0.06, 1.9]], [0, 1, 0], C.rug, fix));

  // 家具：电视柜 / 电视 / 沙发 / 茶几 / 边几 / 绿植 / 楼梯
  faces.push(...boxFaces(-4.2, -1.6, 0.07, 0.49, -1.31, -0.89, C.wood, furn));
  faces.push(...boxFaces(-3.75, -2.05, 0.745, 1.695, -1.13, -1.07, C.dark, furn));
  faces.push(...boxFaces(-4.5, -1.3, 0.07, 0.47, 1.375, 2.325, C.fabric, furn));
  faces.push(...boxFaces(-4.5, -1.3, 0.445, 0.995, 2.15, 2.37, C.fabric, furn));
  faces.push(...boxFaces(-4.51, -4.29, 0.37, 0.87, 1.375, 2.325, C.fabric, furn));
  faces.push(...boxFaces(-1.51, -1.29, 0.37, 0.87, 1.375, 2.325, C.fabric, furn));
  for (let i = -1; i <= 1; i++) {
    const cx = -2.9 + i * 1.0;
    faces.push(...boxFaces(cx - 0.31, cx + 0.31, 0.47, 0.77, 1.85, 2.35, C.fabric2, furn));
  }
  faces.push(...boxFaces(-3.65, -2.15, 0.395, 0.485, 0.1, 0.9, C.wood, furn));
  for (const [dx, dz] of [[-0.66, -0.32], [0.66, -0.32], [-0.66, 0.32], [0.66, 0.32]]) {
    faces.push(...boxFaces(-2.9 + dx - 0.035, -2.9 + dx + 0.035, 0, 0.395, 0.5 + dz - 0.035, 0.5 + dz + 0.035, C.metal, furn));
  }
  faces.push(...boxFaces(-0.31, 0.11, 0.07, 0.49, 2.39, 2.81, C.wood, furn));
  faces.push(...boxFaces(-0.44, 0.24, 0.55, 1.12, 2.26, 2.94, C.green, furn));

  const steps = 8, run = 3.2 / steps, rise = 3.22 / steps;
  for (let i = 0; i < steps; i++) {
    const z = Z0 + 0.15 + i * run;
    faces.push(...boxFaces(-5.9, -4.5, 0, (i + 1) * rise, z, z + run, C.wallSide, furn));
  }

  return faces;
}

function livingLights({ night, lamp }) {
  const lights = [];
  lights.push({
    dir: norm([0.18, 0.9, 0.42]),
    color: night ? [0.36, 0.46, 0.72] : [1, 0.97, 0.9],
    intensity: night ? 0.12 : 0.55,
  });
  if (lamp.on && lamp.brightness > 0) {
    const k = (lamp.brightness / 100) * 0.85;
    const col = kelvin(lamp.colorTemp);
    lights.push({ dir: [0, 1, 0], color: col, intensity: k });            // 主光：照亮地面与家具
    lights.push({ dir: [0, -1, 0], color: col, intensity: k * 0.16 });    // 天花板与墙面散射
  }
  return lights;
}

function norm(v) {
  const m = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / m, v[1] / m, v[2] / m];
}

/** 顶部 / 底部 OSD 信息条 */
function drawOsd(raster, { label, pan, armed, motion, live, channel, fps, now, night }) {
  const w = raster.width;
  const h = raster.height;
  const ink = [243, 239, 230];
  const barH = 22;

  raster.fillRect(0, 0, w, barH, [0, 0, 0], 0.42);
  raster.text(label, 8, 7, ink, 2);
  const mid = `${String(Math.round(pan)).padStart(3, "0")}°  ${armed ? "ARMED" : "DISARMED"}`;
  raster.text(mid, 8 + Raster.textWidth(label, 2) + 14, 7, armed ? [226, 177, 90] : [168, 178, 172], 2);

  const stamp = clockOf(now);
  raster.text(stamp, w - Raster.textWidth(stamp, 2) - 8, 7, ink, 2);

  raster.fillRect(0, h - barH, w, barH, [0, 0, 0], 0.42);
  raster.text(`CH ${channel}`, 8, h - barH + 7, ink, 2);
  const stat = live ? "STREAM ONLINE" : "STREAM IDLE";
  raster.text(stat, 8 + Raster.textWidth(`CH ${channel}`, 2) + 14, h - barH + 7, live ? [125, 206, 160] : [168, 178, 172], 2);
  const res = `${raster.width}X${raster.height} ${fps}FPS`;
  raster.text(res, w - Raster.textWidth(res, 2) - 8, h - barH + 7, [168, 178, 172], 2);

  // 中心准星
  const cx = w / 2, cy = h / 2;
  const cross = [235, 232, 224];
  raster.line(cx - 22, cy, cx - 7, cy, cross, 1, 0.8);
  raster.line(cx + 7, cy, cx + 22, cy, cross, 1, 0.8);
  raster.line(cx, cy - 22, cx, cy - 7, cross, 1, 0.8);
  raster.line(cx, cy + 7, cx, cy + 22, cross, 1, 0.8);

  // 录制指示：推流时右上角红点闪烁
  if (live && Math.floor(now / 600) % 2 === 0) {
    raster.fillRect(w - 46, 30, 8, 8, [239, 111, 108]);
    raster.text("REC", w - 34, 29, [239, 111, 108], 2);
  }

  // 移动侦测告警框
  if (motion) {
    const bx = 10, by = 30, bw = w - 20, bh = h - 60;
    raster.line(bx, by, bx + bw, by, [239, 111, 108], 2);
    raster.line(bx, by + bh, bx + bw, by + bh, [239, 111, 108], 2);
    raster.line(bx, by, bx, by + bh, [239, 111, 108], 2);
    raster.line(bx + bw, by, bx + bw, by + bh, [239, 111, 108], 2);
    raster.text("MOTION DETECTED", bx + 6, by + 6, [239, 111, 108], 2);
  }

  // 夜间模式下画面偏冷、噪点更重
  raster.noise(night ? 20 : 9, 5);
  raster.vignette(night ? 0.62 : 0.45);
}

/* ------------------------------------------------------------------ *
 * 通道帧源
 * ------------------------------------------------------------------ */
/**
 * 客厅云台画面源。
 * @returns {{ render: (ctx) => Raster }}
 */
export function createLivingRoomSource({ width = 512, height = 320, channel = "", label = "LIVING CAM" } = {}) {
  const smooth = { pan: 0, inited: false };
  return {
    width,
    height,
    render({ state = {}, env = {}, now = Date.now(), dt = 0.125, fps = 8 }) {
      const night = !!env.night;
      const lamp = env.lamp || { on: false, brightness: 0, colorTemp: 4000 };
      const pan = easePan(smooth, state.pan || 0, dt);
      const raster = new Raster(width, height);
      raster.fillRect(0, 0, width, height, night ? [14, 18, 22] : [22, 26, 30]);

      const cam = new Camera({
        pos: CAM_POS,
        pan,
        tilt: CAM_TILT,
        fovY: FOV,
        width,
        height,
        near: 0.06,
      });
      const faces = buildLivingFaces(night, lamp.on && lamp.brightness > 0);
      const lights = livingLights({ night, lamp });
      // 环境项偏高：真实房间里墙面之间的漫反射会把垂直面也提亮，
      // 纯方向光会让墙和家具侧面全黑，反而不像监控画面。
      const ambient = (night ? 0.17 : 0.4) + (lamp.on ? 0.06 : 0);
      drawScene(raster, cam, faces, lights, ambient);

      drawOsd(raster, {
        label,
        pan,
        armed: !!state.armed,
        motion: !!state.motion,
        live: !!state.streaming,
        channel,
        fps,
        now,
        night,
      });
      return raster;
    },
  };
}

/* ------------------------------------------------------------------ *
 * 大门人脸锁：门外视角
 * ------------------------------------------------------------------ */
export function createEntrySource({ width = 512, height = 320, channel = "", label = "ENTRY LOCK" } = {}) {
  return {
    width,
    height,
    render({ state = {}, env = {}, now = Date.now(), fps = 8 }) {
      const raster = new Raster(width, height);
      const w = width, h = height;

      // 门外走廊：中间亮、四周暗
      const cx = w / 2, cy = h * 0.44;
      for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) {
          const d = Math.hypot(x - cx, y - cy) / Math.hypot(cx, cy);
          const t = Math.min(1, d * 1.15);
          const k = (1 - t) ** 1.6;
          raster.px(x, y, [18 + 52 * k, 24 + 62 * k, 22 + 56 * k]);
        }
      }

      const who = state.lastPerson ? (NAME_ASCII[state.lastPerson] || "VISITOR") : null;
      const pass = state.lastResult === "pass";
      const reject = state.lastResult === "reject";
      const fy = h * 0.46;

      if (who) {
        // 来人：画一个简笔人脸，让「识别」这件事看得见
        const skin = [214, 192, 162];
        const hair = [58, 44, 34];
        raster.ellipse(cx, fy + 58, 62, 46, [48, 60, 64]);           // 肩
        raster.ellipse(cx, fy, 42, 52, skin);                        // 脸
        raster.ellipse(cx, fy - 36, 44, 28, hair);                   // 头发
        raster.fillRect(cx - 46, fy - 32, 92, 12, hair);             // 刘海
        raster.ellipse(cx - 15, fy - 2, 5.5, 5.5, [40, 38, 36]);     // 眼
        raster.ellipse(cx + 15, fy - 2, 5.5, 5.5, [40, 38, 36]);
        raster.fillRect(cx - 11, fy + 24, 22, 3, [150, 96, 88]);     // 嘴
      } else {
        raster.ellipse(cx, fy, 42, 52, [30, 40, 40], 1, 24);         // 无人时的轮廓
      }

      // 识别框
      const box = pass ? [125, 206, 160] : reject ? [239, 111, 108] : [226, 177, 90];
      const bx = cx - 74, by = fy - 84, bw = 148, bh = 192;
      const seg = 22;
      const corner = (x, y, sx, sy) => {
        raster.line(x, y, x + sx * seg, y, box, 3);
        raster.line(x, y, x, y + sy * seg, box, 3);
      };
      corner(bx, by, 1, 1); corner(bx + bw, by, -1, 1);
      corner(bx, by + bh, 1, -1); corner(bx + bw, by + bh, -1, -1);

      // 底部信息条
      const barH = 26;
      raster.fillRect(0, 0, w, 22, [0, 0, 0], 0.45);
      raster.text(label, 8, 7, [243, 239, 230], 2);
      const mid = state.locked ? "LOCKED" : "UNLOCKED";
      raster.text(mid, 8 + Raster.textWidth(label, 2) + 14, 7, state.locked ? [239, 111, 108] : [125, 206, 160], 2);
      raster.text(clockOf(now), w - Raster.textWidth(clockOf(now), 2) - 8, 7, [243, 239, 230], 2);

      raster.fillRect(0, h - barH, w, barH, [0, 0, 0], 0.5);
      raster.text(`CH ${channel}`, 8, h - barH + 8, [243, 239, 230], 2);
      const result = pass ? "FACE PASS" : reject ? "FACE REJECT" : who ? "DETECTING" : "NO FACE";
      raster.text(result, w - Raster.textWidth(result, 2) - 8, h - barH + 8, box, 2);
      if (who) {
        raster.text(who, cx - Raster.textWidth(who, 2) / 2, h - barH - 26, box, 2);
      }

      raster.noise(14, 5);
      raster.vignette(0.55);
      return raster;
    },
  };
}

/* ------------------------------------------------------------------ *
 * 待机画面：通道在线但没有推流指令时推这一路
 * 这样 <img src="/?stream=..."> 一直是活的连接，指令一到画面立刻切换，
 * 不用刷新页面 —— 和真实摄像头「未取流」的状态也一致。
 * ------------------------------------------------------------------ */
export function createStandbySource({ width = 512, height = 320 } = {}) {
  return {
    width,
    height,
    render({ now = Date.now(), channel = "", name = "" }) {
      const raster = new Raster(width, height);
      const w = width, h = height;
      const gold = [226, 177, 90];
      const dim = [138, 150, 144];

      raster.fillRect(0, 0, w, h, [13, 18, 17]);

      // 扫描线，让待机画面也有「视频」的质感
      for (let y = 0; y < h; y += 4) raster.fillRect(0, y, w, 1, [255, 255, 255], 0.025);

      // 四角框
      const seg = 26;
      const corner = (x, y, sx, sy) => {
        raster.line(x, y, x + sx * seg, y, gold, 2, 0.75);
        raster.line(x, y, x, y + sy * seg, gold, 2, 0.75);
      };
      corner(10, 10, 1, 1); corner(w - 10, 10, -1, 1);
      corner(10, h - 10, 1, -1); corner(w - 10, h - 10, -1, -1);

      const title = "STANDBY";
      raster.text(title, (w - Raster.textWidth(title, 5)) / 2, h / 2 - 34, gold, 5);
      const sub = "WAITING FOR STREAM COMMAND";
      raster.text(sub, (w - Raster.textWidth(sub, 2)) / 2, h / 2 + 16, dim, 2);
      const hint = "SEND  { action: \"stream\", params: { on: true } }";
      raster.text(hint, (w - Raster.textWidth(hint, 1)) / 2, h / 2 + 40, [96, 108, 102], 1);

      raster.text(`CH ${channel}`, 20, 22, gold, 2);
      if (name) raster.text(name, 20 + Raster.textWidth(`CH ${channel}`, 2) + 12, 22, dim, 2);
      raster.text(clockOf(now), w - Raster.textWidth(clockOf(now), 2) - 20, 22, dim, 2);
      raster.text("NO SIGNAL", w - Raster.textWidth("NO SIGNAL", 2) - 20, h - 34, [96, 108, 102], 2);

      raster.vignette(0.5);
      return raster;
    },
  };
}
