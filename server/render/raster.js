/**
 * 服务端极简软件光栅化器
 *
 * 为什么需要它：视频画面改为「HTTP 流」后，像素必须由服务端产生。
 * 服务端没有 GPU，也不该为了教学演示拖进 headless-gl / puppeteer，
 * 所以这里用纯 JS 实现一个够用的软渲染：透视投影 + 背面剔除 + 画家算法 + 朗伯光照。
 *
 * 分层：本文件只认识「点 / 面 / 颜色 / 字体」，不认识任何设备与协议。
 *       场景内容在 scenes.js，传输在 ../stream.js。
 */

/* ------------------------------------------------------------------ *
 * 5x7 点阵字体（监控 OSD 用，只含 ASCII）
 * 每个字符 7 行、每行 5 位，高位在左。
 * ------------------------------------------------------------------ */
const GLYPHS = {
  " ": [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
  A: [0x0e, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11],
  B: [0x1e, 0x11, 0x11, 0x1e, 0x11, 0x11, 0x1e],
  C: [0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e],
  D: [0x1e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1e],
  E: [0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x1f],
  F: [0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x10],
  G: [0x0e, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0f],
  H: [0x11, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11],
  I: [0x0e, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0e],
  J: [0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0c],
  K: [0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11],
  L: [0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1f],
  M: [0x11, 0x1b, 0x15, 0x15, 0x11, 0x11, 0x11],
  N: [0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11],
  O: [0x0e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e],
  P: [0x1e, 0x11, 0x11, 0x1e, 0x10, 0x10, 0x10],
  Q: [0x0e, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0d],
  R: [0x1e, 0x11, 0x11, 0x1e, 0x14, 0x12, 0x11],
  S: [0x0f, 0x10, 0x10, 0x0e, 0x01, 0x01, 0x1e],
  T: [0x1f, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
  U: [0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e],
  V: [0x11, 0x11, 0x11, 0x11, 0x11, 0x0a, 0x04],
  W: [0x11, 0x11, 0x11, 0x15, 0x15, 0x1b, 0x11],
  X: [0x11, 0x11, 0x0a, 0x04, 0x0a, 0x11, 0x11],
  Y: [0x11, 0x11, 0x0a, 0x04, 0x04, 0x04, 0x04],
  Z: [0x1f, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1f],
  0: [0x0e, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0e],
  1: [0x04, 0x0c, 0x04, 0x04, 0x04, 0x04, 0x0e],
  2: [0x0e, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1f],
  3: [0x1f, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0e],
  4: [0x02, 0x06, 0x0a, 0x12, 0x1f, 0x02, 0x02],
  5: [0x1f, 0x10, 0x1e, 0x01, 0x01, 0x11, 0x0e],
  6: [0x06, 0x08, 0x10, 0x1e, 0x11, 0x11, 0x0e],
  7: [0x1f, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08],
  8: [0x0e, 0x11, 0x11, 0x0e, 0x11, 0x11, 0x0e],
  9: [0x0e, 0x11, 0x11, 0x0f, 0x01, 0x02, 0x0c],
  ":": [0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x00],
  ".": [0x00, 0x00, 0x00, 0x00, 0x00, 0x06, 0x06],
  ",": [0x00, 0x00, 0x00, 0x00, 0x06, 0x06, 0x0c],
  "/": [0x01, 0x02, 0x02, 0x04, 0x08, 0x08, 0x10],
  "-": [0x00, 0x00, 0x00, 0x1f, 0x00, 0x00, 0x00],
  "+": [0x00, 0x04, 0x04, 0x1f, 0x04, 0x04, 0x00],
  "#": [0x0a, 0x0a, 0x1f, 0x0a, 0x1f, 0x0a, 0x0a],
  "(": [0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02],
  ")": [0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08],
  "!": [0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04],
  "?": [0x0e, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04],
  "*": [0x00, 0x0a, 0x04, 0x1f, 0x04, 0x0a, 0x00],
  "%": [0x11, 0x01, 0x02, 0x04, 0x08, 0x10, 0x11],
  "°": [0x0c, 0x12, 0x12, 0x0c, 0x00, 0x00, 0x00],
  '"': [0x0a, 0x0a, 0x00, 0x00, 0x00, 0x00, 0x00],
  "'": [0x04, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
  "{": [0x02, 0x04, 0x04, 0x08, 0x04, 0x04, 0x02],
  "}": [0x08, 0x04, 0x04, 0x02, 0x04, 0x04, 0x08],
  "[": [0x0e, 0x08, 0x08, 0x08, 0x08, 0x08, 0x0e],
  "]": [0x0e, 0x02, 0x02, 0x02, 0x02, 0x02, 0x0e],
  "=": [0x00, 0x00, 0x1f, 0x00, 0x1f, 0x00, 0x00],
  _: [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1f],
  "<": [0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02],
  ">": [0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08],
  ";": [0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x08],
};

/* ------------------------------------------------------------------ *
 * 像素缓冲
 * ------------------------------------------------------------------ */
export class Raster {
  constructor(width, height) {
    this.width = width;
    this.height = height;
    this.data = Buffer.alloc(width * height * 4);
    this.data.fill(255);          // jpeg-js 只用 RGB，alpha 保持不透明即可
  }

  /** 直接写一个像素；alpha < 1 时按比例混合 */
  px(x, y, c, a = 1) {
    x |= 0;
    y |= 0;
    if (x < 0 || y < 0 || x >= this.width || y >= this.height) return;
    const d = this.data;
    const i = (y * this.width + x) << 2;
    if (a >= 1) {
      d[i] = c[0]; d[i + 1] = c[1]; d[i + 2] = c[2];
      return;
    }
    d[i] += (c[0] - d[i]) * a;
    d[i + 1] += (c[1] - d[i + 1]) * a;
    d[i + 2] += (c[2] - d[i + 2]) * a;
  }

  fillRect(x, y, w, h, c, a = 1) {
    const x0 = Math.max(0, Math.round(x));
    const y0 = Math.max(0, Math.round(y));
    const x1 = Math.min(this.width - 1, Math.round(x + w) - 1);
    const y1 = Math.min(this.height - 1, Math.round(y + h) - 1);
    for (let yy = y0; yy <= y1; yy++) for (let xx = x0; xx <= x1; xx++) this.px(xx, yy, c, a);
  }

  /** 扫描线填充凸多边形（屏幕坐标）。透视投影保持凸性，所以凸算法够用。 */
  fillPoly(pts, c, a = 1) {
    const n = pts.length;
    if (n < 3) return;
    let minY = Infinity, maxY = -Infinity;
    for (const p of pts) {
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
    }
    const y0 = Math.max(0, Math.floor(minY));
    const y1 = Math.min(this.height - 1, Math.ceil(maxY));
    for (let y = y0; y <= y1; y++) {
      const sy = y + 0.5;
      let xl = Infinity, xr = -Infinity;
      for (let i = 0; i < n; i++) {
        const p = pts[i], q = pts[(i + 1) % n];
        if ((p.y <= sy && q.y > sy) || (q.y <= sy && p.y > sy)) {
          const t = (sy - p.y) / (q.y - p.y);
          const x = p.x + (q.x - p.x) * t;
          if (x < xl) xl = x;
          if (x > xr) xr = x;
        }
      }
      if (xl > xr) continue;
      const xs = Math.max(0, Math.round(xl));
      const xe = Math.min(this.width - 1, Math.round(xr));
      for (let x = xs; x <= xe; x++) this.px(x, y, c, a);
    }
  }

  ellipse(cx, cy, rx, ry, c, a = 1, seg = 28) {
    const pts = [];
    for (let i = 0; i < seg; i++) {
      const t = (i / seg) * Math.PI * 2;
      pts.push({ x: cx + Math.cos(t) * rx, y: cy + Math.sin(t) * ry });
    }
    this.fillPoly(pts, c, a);
  }

  line(x0, y0, x1, y1, c, width = 1, a = 1) {
    const dx = x1 - x0, dy = y1 - y0;
    const steps = Math.max(1, Math.ceil(Math.max(Math.abs(dx), Math.abs(dy))));
    const r = Math.max(0, width / 2 - 0.5);
    for (let i = 0; i <= steps; i++) {
      const x = x0 + (dx * i) / steps;
      const y = y0 + (dy * i) / steps;
      if (r <= 0) {
        this.px(Math.round(x), Math.round(y), c, a);
      } else {
        for (let oy = -Math.ceil(r); oy <= Math.ceil(r); oy++) {
          for (let ox = -Math.ceil(r); ox <= Math.ceil(r); ox++) {
            if (ox * ox + oy * oy <= r * r + 0.4) this.px(Math.round(x + ox), Math.round(y + oy), c, a);
          }
        }
      }
    }
  }

  /** 竖直渐变（用于天空、暗角等），从 y0 到 y1 在两个颜色间插值 */
  vGradient(x, y, w, h, top, bottom, a = 1) {
    for (let i = 0; i < h; i++) {
      const t = h <= 1 ? 0 : i / (h - 1);
      const c = [
        top[0] + (bottom[0] - top[0]) * t,
        top[1] + (bottom[1] - top[1]) * t,
        top[2] + (bottom[2] - top[2]) * t,
      ];
      this.fillRect(x, y + i, w, 1, c, a);
    }
  }

  static textWidth(text, scale = 2, tracking = 1) {
    return text.length * (5 + tracking) * scale - tracking * scale;
  }

  text(text, x, y, c, scale = 2, a = 1, tracking = 1) {
    let cx = Math.round(x);
    const cy = Math.round(y);
    for (const ch of String(text).toUpperCase()) {
      const g = GLYPHS[ch] || GLYPHS["?"];
      for (let row = 0; row < 7; row++) {
        const bits = g[row];
        for (let col = 0; col < 5; col++) {
          if (!(bits & (1 << (4 - col)))) continue;
          this.fillRect(cx + col * scale, cy + row * scale, scale, scale, c, a);
        }
      }
      cx += (5 + tracking) * scale;
    }
  }

  /** 叠加传感器噪点：监控画面在低照度下的典型特征 */
  noise(amount, step = 7) {
    const d = this.data;
    const w = this.width;
    for (let y = 0; y < this.height; y++) {
      for (let x = 0; x < w; x += step) {
        const n = (Math.random() - 0.5) * amount;
        const i = (y * w + x) << 2;
        d[i] = clamp255(d[i] + n);
        d[i + 1] = clamp255(d[i + 1] + n);
        d[i + 2] = clamp255(d[i + 2] + n);
      }
    }
  }

  /** 暗角：越靠边越暗，让画面更像镜头而不是渲染图 */
  vignette(strength = 0.5) {
    const { width: w, height: h } = this;
    const cx = w / 2, cy = h / 2;
    const max = Math.hypot(cx, cy);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const d = Math.hypot(x - cx, y - cy) / max;
        const k = 1 - strength * d * d;
        if (k >= 1) continue;
        const i = (y * w + x) << 2;
        this.data[i] *= k;
        this.data[i + 1] *= k;
        this.data[i + 2] *= k;
      }
    }
  }
}

function clamp255(n) {
  return n < 0 ? 0 : n > 255 ? 255 : n;
}

/* ------------------------------------------------------------------ *
 * 相机：世界坐标 → 相机坐标 → 屏幕坐标
 * 约定：pan=0 看向 +Z，pan 增大绕 Y 轴转向 +X（与三维场景里的云台一致）。
 * ------------------------------------------------------------------ */
export class Camera {
  constructor({ pos, pan = 0, tilt = 0, fovY = Math.PI / 3, width, height, near = 0.06 }) {
    this.pos = pos;
    this.near = near;
    this.width = width;
    this.height = height;
    const th = (pan * Math.PI) / 180;
    const cp = Math.cos(tilt);
    const sp = Math.sin(tilt);
    const fwd = [Math.sin(th) * cp, -sp, Math.cos(th) * cp];
    const right = [Math.cos(th), 0, -Math.sin(th)];
    const up = cross(fwd, right);
    this.fwd = fwd;
    this.right = right;
    this.up = up;
    this.f = height / 2 / Math.tan(fovY / 2);
    this.cx = width / 2;
    this.cy = height / 2;
  }

  /** 世界点 → 相机空间（x 右、y 上、z 前） */
  toCamera(p) {
    const dx = p[0] - this.pos[0];
    const dy = p[1] - this.pos[1];
    const dz = p[2] - this.pos[2];
    const r = this.right, u = this.up, f = this.fwd;
    return {
      x: dx * r[0] + dy * r[1] + dz * r[2],
      y: dx * u[0] + dy * u[1] + dz * u[2],
      z: dx * f[0] + dy * f[1] + dz * f[2],
    };
  }

  /** 相机空间 → 屏幕坐标 */
  project(p) {
    const k = this.f / p.z;
    return { x: this.cx + p.x * k, y: this.cy - p.y * k, z: p.z };
  }

  /** 世界点直接投到屏幕，返回 null 表示在近平面之后 */
  projectWorld(p) {
    const c = this.toCamera(p);
    if (c.z < this.near) return null;
    return this.project(c);
  }

  /** 面是否朝向相机（用于背面剔除） */
  facesCamera(normal) {
    return normal[0] * this.fwd[0] + normal[1] * this.fwd[1] + normal[2] * this.fwd[2] < 0;
  }
}

function cross(a, b) {
  return [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
}

/** Sutherland–Hodgman 单平面裁剪：把多边形切到 z >= near 一侧 */
function clipNear(poly, near) {
  const out = [];
  const n = poly.length;
  for (let i = 0; i < n; i++) {
    const a = poly[i], b = poly[(i + 1) % n];
    const ain = a.z >= near, bin = b.z >= near;
    if (ain) out.push(a);
    if (ain !== bin) {
      const t = (near - a.z) / (b.z - a.z);
      out.push({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, z: near });
    }
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * 光照
 * ------------------------------------------------------------------ */
/**
 * @param {number[]} base   材质基色 [r,g,b] 0-255
 * @param {number[]} normal 面法线（世界空间，指向面的外侧）
 * @param {{dir:number[],color:number[],intensity:number}[]} lights
 * @param {number} ambient  环境光系数
 */
export function shade(base, normal, lights, ambient) {
  let r = base[0] * ambient;
  let g = base[1] * ambient;
  let b = base[2] * ambient;
  for (const L of lights) {
    if (L.intensity <= 0) continue;
    const d = normal[0] * L.dir[0] + normal[1] * L.dir[1] + normal[2] * L.dir[2];
    if (d <= 0) continue;
    const k = d * L.intensity;
    r += base[0] * L.color[0] * k;
    g += base[1] * L.color[1] * k;
    b += base[2] * L.color[2] * k;
  }
  return [clamp255(r), clamp255(g), clamp255(b)];
}

/* ------------------------------------------------------------------ *
 * 面绘制：剔除 → 裁剪 → 投影 → 着色 → 填充
 * ------------------------------------------------------------------ */
export function drawFace(raster, camera, face, lights, ambient) {
  if (face.normal && !camera.facesCamera(face.normal)) return;
  let poly = face.pts.map((p) => camera.toCamera(p));
  if (poly.every((p) => p.z < camera.near)) return;
  if (poly.some((p) => p.z < camera.near)) {
    poly = clipNear(poly, camera.near);
    if (poly.length < 3) return;
  }
  const scr = poly.map((p) => camera.project(p));
  const color = face.flat ? face.color : shade(face.color, face.normal, lights, ambient);
  raster.fillPoly(scr, color, face.alpha ?? 1);
}

/**
 * 画家算法绘制。
 *
 * 排序键是「层级 order → 面心到相机的距离」两个字段：
 *   order 用来处理「贴附关系」—— 窗贴在墙上、地毯铺在地板上、家具立在地毯上。
 *   这些情况光靠距离排序会出错（一块大地毯的面心可能比站在它上面的沙发更近，
 *   于是地毯被后画、把沙发盖掉），所以贴附物显式给更大的 order。
 *   order 相同的面之间再按距离从远到近画。
 *
 * 约定：0 = 房间壳体（地板/墙/天花板），1 = 贴附物（窗/门/地毯/灯盘），2 = 家具。
 */
export function drawScene(raster, camera, faces, lights, ambient) {
  const cam = camera.pos;
  const order = faces.map((f) => {
    let cx = 0, cy = 0, cz = 0;
    for (const p of f.pts) { cx += p[0]; cy += p[1]; cz += p[2]; }
    const n = f.pts.length;
    cx /= n; cy /= n; cz /= n;
    return {
      f,
      o: f.order || 0,
      d: (cx - cam[0]) ** 2 + (cy - cam[1]) ** 2 + (cz - cam[2]) ** 2,
    };
  });
  order.sort((a, b) => (a.o - b.o) || (b.d - a.d));
  for (const { f } of order) drawFace(raster, camera, f, lights, ambient);
}

/* ------------------------------------------------------------------ *
 * 几何小工具
 * ------------------------------------------------------------------ */
/** 由两个对角点生成一个长方体的 6 个面（法线朝外） */
export function boxFaces(x0, x1, y0, y1, z0, z1, color, opts = {}) {
  const v = [
    [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
    [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
  ];
  const mk = (idx, normal) => ({ pts: idx.map((i) => v[i]), normal, color, ...opts });
  return [
    mk([4, 5, 6, 7], [0, 0, 1]),    // 前 (+z)
    mk([1, 0, 3, 2], [0, 0, -1]),   // 后 (-z)
    mk([5, 1, 2, 6], [1, 0, 0]),    // 右 (+x)
    mk([0, 4, 7, 3], [-1, 0, 0]),   // 左 (-x)
    mk([3, 7, 6, 2], [0, 1, 0]),    // 上 (+y)
    mk([0, 1, 5, 4], [0, -1, 0]),   // 下 (-y)
  ];
}

/** 单个矩形面（顺序需按逆时针给出，法线自行指定） */
export function quad(pts, normal, color, opts = {}) {
  return { pts, normal, color, ...opts };
}

/** 色温(K) → 归一化 RGB，和服务端/前端保持同一套近似 */
export function kelvin(k) {
  const t = Math.min(12000, Math.max(1000, k)) / 100;
  let r, g, b;
  if (t <= 66) {
    r = 255;
    g = 99.47 * Math.log(t) - 161.12;
  } else {
    r = 329.7 * Math.pow(t - 60, -0.1332);
    g = 288.12 * Math.pow(t - 60, -0.0755);
  }
  if (t >= 66) b = 255;
  else if (t <= 19) b = 0;
  else b = 138.52 * Math.log(t - 10) - 305.04;
  return [Math.max(0, Math.min(255, r)) / 255, Math.max(0, Math.min(255, g)) / 255, Math.max(0, Math.min(255, b)) / 255];
}
