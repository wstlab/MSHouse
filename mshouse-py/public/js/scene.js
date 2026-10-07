/**
 * 三维孪生场景层
 * 只负责「把设备状态画出来」，不关心 WebSocket。数据由 main.js 从网关推来。
 * 分层：geometry（房子）→ furniture（家具）→ devices（设备视图）→ labels（标签）。
 * 新增设备类型时：写一个 createXxxDevice()，在 buildDevices() 里注册，再实现 update()。
 */
import * as THREE from "three";
import { OrbitControls } from "/vendor/OrbitControls.js";

/* ------------------------------------------------------------------ *
 * 建筑尺寸（米）
 * ------------------------------------------------------------------ */
const X0 = -6, X1 = 6, Z0 = -4.5, Z1 = 4.5;
const PART = 1.5;                 // 一二层共用的分隔墙位置
const FH = 3.0;                   // 净层高
const SLAB = 0.22;                // 楼板厚度
const Y2 = FH + SLAB;             // 二层地面
const Y3 = Y2 + FH;               // 屋顶底
const WT = 0.14;                  // 墙厚
const GROUND_Y = -0.3;
const DOOR_X = -3.4;              // 入户门中心
const DOOR_W = 1.2, DOOR_H = 2.15;

const clamp = (n, a, b) => Math.min(b, Math.max(a, n));
const lerp = (a, b, t) => a + (b - a) * t;

/** 色温(K) → 近似 RGB，用来表现暖白/冷白 */
function kelvinToColor(k) {
  const t = clamp(k, 1000, 12000) / 100;
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
  return new THREE.Color(clamp(r, 0, 255) / 255, clamp(g, 0, 255) / 255, clamp(b, 0, 255) / 255);
}

/* ------------------------------------------------------------------ *
 * 程序化贴图
 * ------------------------------------------------------------------ */
function makeWoodTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 512;
  const g = c.getContext("2d");
  g.fillStyle = "#6b4a30";
  g.fillRect(0, 0, 512, 512);
  const plank = 64;
  for (let y = 0; y < 512; y += plank) {
    for (let x = 0; x < 512; x += 256) {
      const off = (y / plank) % 2 ? 128 : 0;
      const tone = 46 + Math.random() * 22;
      g.fillStyle = `hsl(${26 + Math.random() * 8}, ${34 + Math.random() * 10}%, ${tone}%)`;
      g.fillRect(x + off, y + 1, 254, plank - 2);
      g.strokeStyle = "rgba(0,0,0,.18)";
      g.lineWidth = 1;
      for (let i = 0; i < 6; i++) {
        const gy = y + 6 + Math.random() * (plank - 12);
        g.beginPath();
        g.moveTo(x + off + 4, gy);
        g.bezierCurveTo(x + off + 80, gy + 3, x + off + 170, gy - 3, x + off + 250, gy);
        g.stroke();
      }
    }
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function makeTileTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 256;
  const g = c.getContext("2d");
  g.fillStyle = "#cfd6d2";
  g.fillRect(0, 0, 256, 256);
  g.strokeStyle = "rgba(90,105,100,.5)";
  g.lineWidth = 3;
  for (let i = 0; i <= 256; i += 64) {
    g.beginPath(); g.moveTo(i, 0); g.lineTo(i, 256); g.stroke();
    g.beginPath(); g.moveTo(0, i); g.lineTo(256, i); g.stroke();
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function makeGlowTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d");
  const grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grd.addColorStop(0, "rgba(255,255,255,1)");
  grd.addColorStop(0.25, "rgba(255,255,255,.55)");
  grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd;
  g.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/* ------------------------------------------------------------------ *
 * 文字标签（用 Sprite 实现，免装 CSS2DRenderer）
 * ------------------------------------------------------------------ */
class Label {
  constructor({ text = "", worldHeight = 0.6, color = "#f3efe6", bg = "rgba(10,16,14,.74)", accent = "rgba(226,177,90,.55)", font = "700 74px 'PingFang SC','Noto Sans SC',sans-serif", w = 512, h = 160 } = {}) {
    this.w = w; this.h = h;
    this.color = color; this.bg = bg; this.accent = accent; this.font = font;
    this.canvas = document.createElement("canvas");
    this.canvas.width = w;
    this.canvas.height = h;
    this.ctx = this.canvas.getContext("2d");
    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    const mat = new THREE.SpriteMaterial({ map: this.texture, transparent: true, depthWrite: false });
    this.sprite = new THREE.Sprite(mat);
    this.sprite.scale.set(worldHeight * (w / h), worldHeight, 1);
    this.setText(text);
  }

  setText(text) {
    if (text === this.text) return;
    this.text = text;
    const { ctx, w, h } = this;
    ctx.clearRect(0, 0, w, h);
    const r = 28;
    ctx.beginPath();
    ctx.roundRect(6, 6, w - 12, h - 12, r);
    ctx.fillStyle = this.bg;
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = this.accent;
    ctx.stroke();
    ctx.font = this.font;
    ctx.fillStyle = this.color;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, w / 2, h / 2 + 4, w - 44);
    this.texture.needsUpdate = true;
  }
}

/* ------------------------------------------------------------------ *
 * 墙体开洞分解：把带门窗洞的墙拆成若干实体块，避免引入 CSG
 * ------------------------------------------------------------------ */
function decompose(from, to, base, height, holes) {
  const L = to - from;
  const us = new Set([0, L]);
  const vs = new Set([0, height]);
  const H = holes.map((h) => ({
    u0: clamp(h.from - from, 0, L), u1: clamp(h.to - from, 0, L),
    v0: clamp(h.bottom - base, 0, height), v1: clamp(h.top - base, 0, height),
  }));
  for (const h of H) { us.add(h.u0); us.add(h.u1); vs.add(h.v0); vs.add(h.v1); }
  const U = [...us].sort((a, b) => a - b);
  const V = [...vs].sort((a, b) => a - b);
  const cells = [];
  for (let i = 0; i < U.length - 1; i++) {
    for (let j = 0; j < V.length - 1; j++) {
      const u0 = U[i], u1 = U[i + 1], v0 = V[j], v1 = V[j + 1];
      if (u1 - u0 < 1e-3 || v1 - v0 < 1e-3) continue;
      const cu = (u0 + u1) / 2, cv = (v0 + v1) / 2;
      const inside = H.some((h) => cu > h.u0 + 1e-3 && cu < h.u1 - 1e-3 && cv > h.v0 + 1e-3 && cv < h.v1 - 1e-3);
      if (!inside) cells.push([u0, u1, v0, v1]);
    }
  }
  return cells;
}

/* ================================================================== *
 * 场景
 * ================================================================== */
export class MSHouseScene {
  constructor(canvas) {
    this.canvas = canvas;
    this.deviceViews = new Map();
    this.roomLabels = [];
    this.frontWalls = [];
    this.selection = null;
    this.selectHandlers = [];
    this.clock = new THREE.Clock();
    this.time = 0;
    this.floorMode = "all";
    this.roofOn = false;

    this._initRenderer();
    this._initScene();
    this._buildVilla();
    this._buildDevices();
    this._initPicking();
    this.setFloor("all");
    window.addEventListener("resize", () => this.resize());
    this.resize();
    this._loop();
  }

  /* ---------- 基础设施 ---------- */
  _initRenderer() {
    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
  }

  _initScene() {
    this.scene = new THREE.Scene();
    // 黄昏天空
    const sky = document.createElement("canvas");
    sky.width = 4; sky.height = 256;
    const sg = sky.getContext("2d");
    const grd = sg.createLinearGradient(0, 0, 0, 256);
    grd.addColorStop(0, "#0a1524");
    grd.addColorStop(0.45, "#1d3348");
    grd.addColorStop(0.72, "#3d4a52");
    grd.addColorStop(1, "#121a1c");
    sg.fillStyle = grd;
    sg.fillRect(0, 0, 4, 256);
    const skyTex = new THREE.CanvasTexture(sky);
    skyTex.colorSpace = THREE.SRGBColorSpace;
    this.scene.background = skyTex;
    this.scene.fog = new THREE.Fog(0x16222b, 48, 120);

    this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 400);
    this.camera.position.set(17, 12.5, 19);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(0, 2.6, 0);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.07;
    this.controls.minDistance = 6;
    this.controls.maxDistance = 60;
    this.controls.maxPolarAngle = Math.PI * 0.495;
    this.controls.update();

    this.scene.add(new THREE.HemisphereLight(0x9fc6ff, 0x1b2a24, 0.55));
    this.scene.add(new THREE.AmbientLight(0xdfe9ff, 0.12));

    const moon = new THREE.DirectionalLight(0xc9dcff, 1.15);
    moon.position.set(-20, 26, 16);
    moon.castShadow = true;
    moon.shadow.mapSize.set(2048, 2048);
    moon.shadow.camera.left = -22;
    moon.shadow.camera.right = 22;
    moon.shadow.camera.top = 22;
    moon.shadow.camera.bottom = -22;
    moon.shadow.camera.far = 80;
    moon.shadow.bias = -0.0009;
    moon.shadow.normalBias = 0.02;
    this.scene.add(moon);

    const warm = new THREE.DirectionalLight(0xe2b15a, 0.22);
    warm.position.set(16, 8, -14);
    this.scene.add(warm);
  }

  _mat(color, opts = {}) {
    return new THREE.MeshStandardMaterial({ color, roughness: 0.88, metalness: 0.02, ...opts });
  }

  _box(w, h, d, mat, x, y, z, parent, { shadow = true } = {}) {
    const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
    m.position.set(x, y, z);
    m.castShadow = shadow;
    m.receiveShadow = true;
    parent.add(m);
    return m;
  }

  /* ---------- 建筑 ---------- */
  _buildVilla() {
    const wood = makeWoodTexture();
    const tile = makeTileTexture();
    this.mats = {
      wallOut: this._mat(0xd9d2c2, { roughness: 0.95 }),
      wallIn: this._mat(0xeae3d4, { roughness: 0.96 }),
      slab: this._mat(0xbdb4a2, { roughness: 0.94 }),
      plinth: this._mat(0x4a4f4c, { roughness: 0.98 }),
      wood: new THREE.MeshStandardMaterial({ map: wood, roughness: 0.72, metalness: 0.04 }),
      tile: new THREE.MeshStandardMaterial({ map: tile, roughness: 0.5, metalness: 0.05 }),
      // 二层地面单独一份材质：全屋视图时调成半透明，否则楼板会把一层挡死
      slabUpper: null, woodUpper: null, tileUpper: null,
      glass: new THREE.MeshStandardMaterial({ color: 0x9ec8e0, transparent: true, opacity: 0.22, roughness: 0.08, metalness: 0.25, side: THREE.DoubleSide }),
      frame: this._mat(0x2b3238, { roughness: 0.55, metalness: 0.5 }),
      metal: this._mat(0x39424a, { roughness: 0.42, metalness: 0.72 }),
      dark: this._mat(0x171c20, { roughness: 0.6, metalness: 0.3 }),
      fabric: this._mat(0x6c7f86, { roughness: 1 }),
      fabric2: this._mat(0x8a7a63, { roughness: 1 }),
      white: this._mat(0xf0efe9, { roughness: 0.7 }),
      wood2: this._mat(0x8a623f, { roughness: 0.8 }),
      green: this._mat(0x4f7a5a, { roughness: 1 }),
    };
    // 木地板 / 瓷砖分别铺到客厅·卧室、厨房·卫生间
    this.mats.wood.map.repeat.set(4, 3);
    this.mats.tile.map.repeat.set(3, 4);
    this.mats.slabUpper = this.mats.slab.clone();
    this.mats.woodUpper = this.mats.wood.clone();
    this.mats.tileUpper = this.mats.tile.clone();
    this.upperFadeMats = [this.mats.slabUpper, this.mats.woodUpper, this.mats.tileUpper];

    // 场地
    const site = new THREE.Mesh(
      new THREE.CircleGeometry(46, 64),
      new THREE.MeshStandardMaterial({ color: 0x223029, roughness: 1 }),
    );
    site.rotation.x = -Math.PI / 2;
    site.position.y = GROUND_Y - 0.02;
    site.receiveShadow = true;
    this.scene.add(site);

    const grid = new THREE.GridHelper(92, 92, 0x3c5a4c, 0x2a3f36);
    grid.position.y = GROUND_Y;
    grid.material.transparent = true;
    grid.material.opacity = 0.35;
    this.scene.add(grid);

    this.root = new THREE.Group();
    this.scene.add(this.root);

    this.levels = { 1: new THREE.Group(), 2: new THREE.Group() };
    this.roofGroup = new THREE.Group();
    this.root.add(this.levels[1], this.levels[2], this.roofGroup);

    this._buildFloor1();
    this._buildFloor2();
    this._buildRoof();
    this._buildSite();
    this._buildRoomLabels();
  }

  /** 建墙：axis='x' 表示墙沿 X 方向延伸（z=at 固定），axis='z' 反之 */
  _wall({ axis, at, from, to, base = 0, height = FH, holes = [], group, mat = this.mats.wallIn, tag }) {
    const cells = decompose(from, to, base, height, holes);
    for (const [u0, u1, v0, v1] of cells) {
      const len = u1 - u0, hh = v1 - v0;
      const cu = from + (u0 + u1) / 2;
      const cy = base + (v0 + v1) / 2;
      const m = axis === "x"
        ? this._box(len, hh, WT, mat, cu, cy, at, group)
        : this._box(WT, hh, len, mat, at, cy, cu, group);
      if (tag === "front") this.frontWalls.push(m);
    }
    // 窗玻璃 + 窗框
    for (const h of holes) {
      const w = h.to - h.from, hh = h.top - h.bottom;
      const cu = (h.from + h.to) / 2, cy = (h.bottom + h.top) / 2;
      if (h.door) continue;
      const pane = axis === "x"
        ? this._box(w, hh, 0.03, this.mats.glass, cu, cy, at, group, { shadow: false })
        : this._box(0.03, hh, w, this.mats.glass, at, cy, cu, group, { shadow: false });
      pane.renderOrder = 1;
      const t = 0.07;
      const frame = new THREE.Group();
      group.add(frame);
      const mk = (bw, bh, bd, px, py, pz) => this._box(bw, bh, bd, this.mats.frame, px, py, pz, frame, { shadow: false });
      if (axis === "x") {
        mk(w + t, t, WT + 0.03, cu, h.bottom, at);
        mk(w + t, t, WT + 0.03, cu, h.top, at);
        mk(t, hh, WT + 0.03, h.from, cy, at);
        mk(t, hh, WT + 0.03, h.to, cy, at);
      } else {
        mk(WT + 0.03, t, w + t, at, h.bottom, cu);
        mk(WT + 0.03, t, w + t, at, h.top, cu);
        mk(WT + 0.03, hh, t, at, cy, h.from);
        mk(WT + 0.03, hh, t, at, cy, h.to);
      }
    }
  }

  _floorPlate({ x0, x1, z0, z1, y, thick, mat, group, holes = [] }) {
    const xs = new Set([x0, x1]);
    const zs = new Set([z0, z1]);
    for (const h of holes) { xs.add(h.x0); xs.add(h.x1); zs.add(h.z0); zs.add(h.z1); }
    const XS = [...xs].sort((a, b) => a - b);
    const ZS = [...zs].sort((a, b) => a - b);
    for (let i = 0; i < XS.length - 1; i++) {
      for (let j = 0; j < ZS.length - 1; j++) {
        const cx = (XS[i] + XS[i + 1]) / 2;
        const cz = (ZS[j] + ZS[j + 1]) / 2;
        const skip = holes.some((h) => cx > h.x0 + 1e-3 && cx < h.x1 - 1e-3 && cz > h.z0 + 1e-3 && cz < h.z1 - 1e-3);
        if (skip) continue;
        const m = this._box(XS[i + 1] - XS[i], thick, ZS[j + 1] - ZS[j], mat, cx, y, cz, group);
        m.receiveShadow = true;
      }
    }
  }

  _buildFloor1() {
    const g = this.levels[1];
    // 地面：客厅木地板 / 厨房瓷砖
    const livingFloor = this._box(PART - X0, 0.06, Z1 - Z0, this.mats.wood, (X0 + PART) / 2, 0.03, 0, g, { shadow: false });
    livingFloor.receiveShadow = true;
    const kitchenFloor = this._box(X1 - PART, 0.06, Z1 - Z0, this.mats.tile, (PART + X1) / 2, 0.03, 0, g, { shadow: false });
    kitchenFloor.receiveShadow = true;

    const win = (from, to, b = 0.95, t = 2.35) => ({ from, to, bottom: b, top: t });

    // 外墙（前墙 z=Z1 带入户门）
    this._wall({ axis: "x", at: Z1, from: X0, to: X1, holes: [
      { from: DOOR_X - DOOR_W / 2 - 0.06, to: DOOR_X + DOOR_W / 2 + 0.06, bottom: 0, top: DOOR_H + 0.06, door: true },
      win(-1.0, 1.1),
      win(3.0, 5.0),
    ], group: g, tag: "front" });
    this._wall({ axis: "x", at: Z0, from: X0, to: X1, holes: [win(2.9, 4.7, 1.15, 2.35)], group: g });
    this._wall({ axis: "z", at: X0, from: Z0, to: Z1, holes: [win(-2.1, 0.5)], group: g });
    this._wall({ axis: "z", at: X1, from: Z0, to: Z1, holes: [win(-1.1, 1.1, 1.15, 2.35)], group: g });

    // 内隔墙（客厅/厨房）+ 门洞
    this._wall({ axis: "z", at: PART, from: Z0, to: Z1, holes: [{ from: -1.7, to: -0.2, bottom: 0, top: 2.2, door: true }], group: g, mat: this.mats.wallIn });
    // 隔墙上的门套
    this._box(0.1, 2.28, 0.1, this.mats.frame, PART, 1.14, -1.7, g, { shadow: false });
    this._box(0.1, 2.28, 0.1, this.mats.frame, PART, 1.14, -0.2, g, { shadow: false });

    this._buildStairs(g);
    this._buildLivingFurniture(g);
    this._buildKitchenFurniture(g);
  }

  _buildStairs(g) {
    const steps = 12, run = 3.2 / steps, rise = Y2 / steps;
    const sx0 = X0 + 0.1, sx1 = X0 + 1.5;
    const zStart = Z0 + 0.15;
    for (let i = 0; i < steps; i++) {
      const h = (i + 1) * rise;
      this._box(sx1 - sx0, h, run, this.mats.slab, (sx0 + sx1) / 2, h / 2, zStart + (i + 0.5) * run, g);
    }
    // 栏杆
    const rail = this.mats.metal;
    for (let i = 0; i < steps; i += 2) {
      const h = (i + 1) * rise;
      this._box(0.05, 0.95, 0.05, rail, sx1 - 0.05, h + 0.47, zStart + (i + 0.5) * run, g, { shadow: false });
    }
    const handrail = this._box(0.07, 0.07, 3.3, rail, sx1 - 0.05, Y2 * 0.62 + 1.0, zStart + 1.6, g, { shadow: false });
    handrail.rotation.x = -Math.atan2(rise, run);
  }

  _buildLivingFurniture(g) {
    const cx = -2.9;
    // 地毯
    const rug = this._box(4.0, 0.02, 2.8, this._mat(0x53606b, { roughness: 1 }), cx, 0.07, 0.5, g, { shadow: false });
    rug.receiveShadow = true;
    // 沙发
    this._box(3.2, 0.4, 0.95, this.mats.fabric, cx, 0.27, 1.85, g);
    this._box(3.2, 0.55, 0.22, this.mats.fabric, cx, 0.72, 2.26, g);
    this._box(0.22, 0.5, 0.95, this.mats.fabric, cx - 1.5, 0.62, 1.85, g);
    this._box(0.22, 0.5, 0.95, this.mats.fabric, cx + 1.5, 0.62, 1.85, g);
    for (let i = -1; i <= 1; i++) this._box(0.62, 0.3, 0.5, this.mats.fabric2, cx + i * 1.0, 0.62, 2.1, g, { shadow: false });
    // 茶几
    this._box(1.5, 0.09, 0.8, this.mats.wood2, cx, 0.44, 0.5, g);
    for (const [dx, dz] of [[-0.66, -0.32], [0.66, -0.32], [-0.66, 0.32], [0.66, 0.32]]) {
      this._box(0.07, 0.42, 0.07, this.mats.metal, cx + dx, 0.21, 0.5 + dz, g, { shadow: false });
    }
    // 电视柜 + 电视
    this._box(2.6, 0.42, 0.42, this.mats.wood2, cx, 0.28, -1.1, g);
    this._box(1.7, 0.95, 0.06, this.mats.dark, cx, 1.22, -1.1, g);
    // 边几绿植
    this._box(0.42, 0.42, 0.42, this.mats.wood2, -0.1, 0.28, 2.6, g, { shadow: false });
    const plant = new THREE.Mesh(new THREE.SphereGeometry(0.34, 12, 10), this.mats.green);
    plant.position.set(-0.1, 0.78, 2.6);
    plant.castShadow = true;
    g.add(plant);
  }

  _buildKitchenFurniture(g) {
    // 右侧橱柜
    this._box(0.62, 0.9, 3.4, this.mats.white, X1 - 0.45, 0.45, -1.3, g);
    this._box(0.4, 0.72, 3.4, this.mats.wood2, X1 - 0.36, 2.0, -1.3, g);
    // 后侧橱柜
    this._box(2.6, 0.9, 0.62, this.mats.white, 3.2, 0.45, Z0 + 0.45, g);
    // 灶台 + 水槽
    this._box(0.62, 0.04, 0.72, this.mats.dark, X1 - 0.45, 0.92, -2.1, g, { shadow: false });
    this._box(0.5, 0.05, 0.42, this.mats.metal, 3.0, 0.92, Z0 + 0.45, g, { shadow: false });
    // 冰箱
    this._box(0.78, 1.9, 0.78, this.mats.metal, X1 - 0.55, 0.95, 1.75, g);
    // 餐桌 + 椅子
    this._box(1.8, 0.08, 0.95, this.mats.wood2, 3.3, 0.76, 1.1, g);
    for (const [dx, dz] of [[-0.8, -0.4], [0.8, -0.4], [-0.8, 0.4], [0.8, 0.4]]) {
      this._box(0.07, 0.72, 0.07, this.mats.metal, 3.3 + dx, 0.36, 1.1 + dz, g, { shadow: false });
    }
    for (const dz of [-0.85, 0.85]) {
      this._box(0.42, 0.45, 0.42, this.mats.fabric2, 3.3 - 0.62, 0.24, 1.1 + dz, g, { shadow: false });
      this._box(0.42, 0.45, 0.42, this.mats.fabric2, 3.3 + 0.62, 0.24, 1.1 + dz, g, { shadow: false });
    }
  }

  _buildFloor2() {
    const g = this.levels[2];
    // 楼板（楼梯口留洞）
    this._floorPlate({
      x0: X0, x1: X1, z0: Z0, z1: Z1, y: FH + SLAB / 2, thick: SLAB, mat: this.mats.slabUpper, group: g,
      holes: [{ x0: X0 - 0.1, x1: X0 + 1.6, z0: Z0 - 0.1, z1: Z0 + 3.5 }],
    });
    // 地面饰面
    this._box(PART - X0, 0.05, Z1 - Z0, this.mats.woodUpper, (X0 + PART) / 2, Y2 + 0.025, 0, g, { shadow: false });
    this._box(X1 - PART, 0.05, Z1 - Z0, this.mats.tileUpper, (PART + X1) / 2, Y2 + 0.025, 0, g, { shadow: false });

    const win = (from, to, b, t) => ({ from, to, bottom: Y2 + b, top: Y2 + t });
    this._wall({ axis: "x", at: Z1, from: X0, to: X1, base: Y2, holes: [win(-4.4, -2.6, 0.95, 2.35), win(3.1, 4.7, 1.3, 2.4)], group: g, tag: "front" });
    this._wall({ axis: "x", at: Z0, from: X0, to: X1, base: Y2, holes: [win(-1.4, 0.6, 0.95, 2.35)], group: g });
    this._wall({ axis: "z", at: X0, from: Z0, to: Z1, base: Y2, holes: [win(-1.8, 0.6, 0.95, 2.35)], group: g });
    this._wall({ axis: "z", at: X1, from: Z0, to: Z1, base: Y2, holes: [win(-0.6, 1.0, 1.4, 2.4)], group: g });
    // 内隔墙
    this._wall({ axis: "z", at: PART, from: Z0, to: Z1, base: Y2, holes: [{ from: -0.8, to: 0.6, bottom: Y2, top: Y2 + 2.1, door: true }], group: g });

    this._buildBedroomFurniture(g);
    this._buildBathFurniture(g);
  }

  _buildBedroomFurniture(g) {
    const bx = -4.1, bz = -1.3;
    this._box(2.1, 0.34, 2.2, this.mats.wood2, bx, Y2 + 0.22, bz, g);
    this._box(2.0, 0.28, 2.1, this.mats.white, bx, Y2 + 0.53, bz, g);
    this._box(2.1, 0.85, 0.14, this.mats.fabric2, bx, Y2 + 0.65, bz - 1.14, g);
    this._box(0.62, 0.16, 0.4, this.mats.white, bx - 0.5, Y2 + 0.72, bz - 0.78, g, { shadow: false });
    this._box(0.62, 0.16, 0.4, this.mats.white, bx + 0.5, Y2 + 0.72, bz - 0.78, g, { shadow: false });
    this._box(2.0, 0.12, 0.9, this.mats.fabric, bx, Y2 + 0.72, bz + 0.55, g, { shadow: false });
    // 床头柜 + 衣柜 + 书桌
    this._box(0.5, 0.5, 0.45, this.mats.wood2, bx - 1.4, Y2 + 0.3, bz - 1.1, g);
    this._box(0.5, 0.5, 0.45, this.mats.wood2, bx + 1.4, Y2 + 0.3, bz - 1.1, g);
    this._box(2.4, 2.1, 0.6, this.mats.wood2, -1.0, Y2 + 1.08, Z0 + 0.42, g);
    this._box(1.3, 0.06, 0.6, this.mats.wood2, -0.2, Y2 + 0.76, 3.3, g);
    this._box(0.42, 0.45, 0.42, this.mats.fabric, -0.2, Y2 + 0.24, 2.6, g, { shadow: false });
    const rug2 = this._box(3.0, 0.02, 2.0, this._mat(0x6b5f52, { roughness: 1 }), -2.6, Y2 + 0.07, 1.4, g, { shadow: false });
    rug2.receiveShadow = true;
  }

  _buildBathFurniture(g) {
    // 淋浴房
    const sx = 4.9, sz = -3.4;
    this._box(1.5, 0.06, 1.5, this.mats.tile, sx, Y2 + 0.09, sz, g, { shadow: false });
    const glassA = this._box(1.5, 1.9, 0.03, this.mats.glass, sx, Y2 + 1.05, sz - 0.75, g, { shadow: false });
    const glassB = this._box(0.03, 1.9, 1.5, this.mats.glass, sx - 0.75, Y2 + 1.05, sz, g, { shadow: false });
    glassA.renderOrder = glassB.renderOrder = 1;
    this._box(0.06, 0.06, 0.06, this.mats.metal, sx + 0.6, Y2 + 2.05, sz + 0.6, g, { shadow: false });
    // 马桶
    this._box(0.42, 0.42, 0.6, this.mats.white, 2.3, Y2 + 0.21, -3.7, g);
    this._box(0.44, 0.55, 0.2, this.mats.white, 2.3, Y2 + 0.5, -4.05, g);
    // 洗手台
    this._box(1.5, 0.1, 0.5, this.mats.white, 4.9, Y2 + 0.85, 3.4, g);
    this._box(1.3, 0.78, 0.45, this.mats.wood2, 4.9, Y2 + 0.42, 3.42, g);
    this._box(0.5, 0.14, 0.36, this.mats.metal, 4.9, Y2 + 0.97, 3.4, g, { shadow: false });
    // 镜子
    this._box(1.2, 0.9, 0.04, this.mats.glass, 4.9, Y2 + 1.7, 3.66, g, { shadow: false });
  }

  _buildRoof() {
    const g = this.roofGroup;
    this._floorPlate({ x0: X0 - 0.35, x1: X1 + 0.35, z0: Z0 - 0.35, z1: Z1 + 0.35, y: Y3 + 0.14, thick: 0.28, mat: this.mats.slab, group: g });
    // 女儿墙
    const p = 0.42;
    this._box(X1 - X0 + 0.7, p, 0.16, this.mats.wallOut, 0, Y3 + 0.28 + p / 2, Z0 - 0.35, g);
    this._box(X1 - X0 + 0.7, p, 0.16, this.mats.wallOut, 0, Y3 + 0.28 + p / 2, Z1 + 0.35, g);
    this._box(0.16, p, Z1 - Z0 + 0.7, this.mats.wallOut, X0 - 0.35, Y3 + 0.28 + p / 2, 0, g);
    this._box(0.16, p, Z1 - Z0 + 0.7, this.mats.wallOut, X1 + 0.35, Y3 + 0.28 + p / 2, 0, g);
  }

  _buildSite() {
    // 基座
    this._box(12.8, 0.3, 9.8, this.mats.plinth, 0, GROUND_Y + 0.15, 0, this.root);
    // 入户台阶 + 雨棚
    this._box(2.6, 0.16, 1.5, this.mats.plinth, DOOR_X, GROUND_Y + 0.24, Z1 + 0.75, this.root);
    this._box(2.8, 0.18, 1.8, this.mats.slab, DOOR_X, 2.75, Z1 + 0.9, this.root);
    this._box(0.16, 2.9, 0.16, this.mats.metal, DOOR_X - 1.25, 1.45, Z1 + 1.6, this.root);
    this._box(0.16, 2.9, 0.16, this.mats.metal, DOOR_X + 1.25, 1.45, Z1 + 1.6, this.root);
  }

  _buildRoomLabels() {
    const items = [
      { text: "客厅", x: -2.9, y: 2.62, z: 3.5, h: 0.58, floor: 1, color: "#f6e3bd" },
      { text: "厨房", x: 3.9, y: 2.62, z: 3.5, h: 0.58, floor: 1, color: "#c9e6d6" },
      { text: "主卧", x: -3.4, y: Y2 + 2.62, z: 3.5, h: 0.58, floor: 2, color: "#f6e3bd" },
      { text: "卫生间", x: 4.2, y: Y2 + 2.62, z: 3.5, h: 0.54, floor: 2, color: "#c9e6d2" },
    ];
    for (const it of items) {
      const label = new Label({ text: it.text, worldHeight: it.h, color: it.color, font: "700 76px 'PingFang SC','Noto Sans SC',sans-serif" });
      label.sprite.position.set(it.x, it.y, it.z);
      // 教学用：房间名永远压在几何体之上，否则会被楼板/墙挡住
      label.sprite.material.depthTest = false;
      label.sprite.renderOrder = 900;
      label.floor = it.floor;
      this.root.add(label.sprite);
      this.roomLabels.push(label);
    }
    const mkFloorTag = (text, floor, y) => {
      const l = new Label({ text, worldHeight: 0.78, color: "#e2b15a", font: "800 92px 'IBM Plex Mono',monospace" });
      l.sprite.position.set(X0 - 1.7, y, Z1 + 0.7);
      l.sprite.material.depthTest = false;
      l.sprite.renderOrder = 900;
      l.floor = floor;
      this.levels[floor].add(l.sprite);
      this.roomLabels.push(l);
    };
    mkFloorTag("F1", 1, 1.5);
    mkFloorTag("F2", 2, Y2 + 1.5);
  }

  /* ---------- 设备视图 ---------- */
  _deviceGroup(id, position, parent) {
    const g = new THREE.Group();
    g.position.copy(position);
    g.userData.deviceId = id;
    parent.add(g);
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({
      map: this.glow, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0,
    }));
    halo.scale.set(1.6, 1.6, 1);
    g.add(halo);
    return { g, halo };
  }

  _buildDevices() {
    this.glow = makeGlowTexture();
    this.deviceRoot = new THREE.Group();
    this.root.add(this.deviceRoot);

    this._lightDevice("light.living", new THREE.Vector3(-2.9, FH - 0.42, 0.4), this.levels[1]);
    this._lightDevice("light.kitchen", new THREE.Vector3(3.9, FH - 0.42, -0.4), this.levels[1]);
    this._lightDevice("light.bedroom", new THREE.Vector3(-2.9, Y2 + FH - 0.42, -0.2), this.levels[2]);
    this._lightDevice("light.bath", new THREE.Vector3(3.9, Y2 + FH - 0.42, 0.4), this.levels[2]);

    this._sensorDevice("sensor.living", new THREE.Vector3(PART - 0.1, 1.62, 2.6), this.levels[1], -1);
    this._sensorDevice("sensor.kitchen", new THREE.Vector3(X1 - 0.1, 1.62, 2.9), this.levels[1], -1);
    this._sensorDevice("sensor.bedroom", new THREE.Vector3(PART - 0.1, Y2 + 1.62, 2.6), this.levels[2], -1);
    this._sensorDevice("sensor.bath", new THREE.Vector3(X1 - 0.1, Y2 + 1.62, 2.9), this.levels[2], -1);

    this._acDevice("ac.living", new THREE.Vector3(X0 + 0.22, 2.35, 2.5), this.levels[1], 1);
    this._acDevice("ac.bedroom", new THREE.Vector3(X0 + 0.22, Y2 + 2.35, 2.5), this.levels[2], 1);

    // 摄像头挂在 root 而不是楼层组：楼层组在全屋视图会被整体挪开，镜头必须留在建筑真实坐标
    this._cameraDevice("camera.living", new THREE.Vector3(-0.6, 2.72, Z0 + 0.42), this.root);
    this._lockDevice("lock.entry", new THREE.Vector3(DOOR_X, 0, Z1), this.levels[1]);
  }

  _lightDevice(id, pos, level) {
    const { g, halo } = this._deviceGroup(id, pos, level);
    const stem = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 0.3, 8), this.mats.metal);
    stem.position.y = 0.22;
    g.add(stem);
    const shadeMat = new THREE.MeshStandardMaterial({
      color: 0xf3e2c0, emissive: new THREE.Color(0xffdca8), emissiveIntensity: 0, roughness: 0.6, side: THREE.DoubleSide,
    });
    const shade = new THREE.Mesh(new THREE.ConeGeometry(0.3, 0.3, 20, 1, true), shadeMat);
    shade.rotation.x = Math.PI;
    shade.position.y = -0.02;
    g.add(shade);
    const bulbMat = new THREE.MeshStandardMaterial({ color: 0xfff3d6, emissive: new THREE.Color(0xffd9a0), emissiveIntensity: 0 });
    const bulb = new THREE.Mesh(new THREE.SphereGeometry(0.09, 12, 10), bulbMat);
    bulb.position.y = -0.12;
    g.add(bulb);
    const light = new THREE.PointLight(0xffd9a0, 0, 11, 2);
    light.position.y = -0.2;
    g.add(light);
    const glowSprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: this.glow, color: 0xffd9a0, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0,
    }));
    glowSprite.scale.set(2.8, 2.8, 1);
    glowSprite.position.y = -0.16;
    g.add(glowSprite);

    this.deviceViews.set(id, {
      id, type: "light", group: g, halo,
      update(state, animate = true) {
        const on = !!state.power;
        const b = on ? clamp(state.brightness, 0, 100) / 100 : 0;
        const col = kelvinToColor(state.colorTemp || 4000);
        this.target = { b, col };
        light.color.copy(col);
        bulbMat.emissive.copy(col);
        shadeMat.emissive.copy(col);
        glowSprite.material.color.copy(col);
        if (!animate) { this.snap(); }
      },
      snap() {
        const { b, col } = this.target;
        light.intensity = 22 * b;
        bulbMat.emissiveIntensity = 1.6 * b;
        shadeMat.emissiveIntensity = 0.9 * b;
        glowSprite.material.opacity = 0.55 * b;
      },
      tick(dt) {
        if (!this.target) return;
        const cur = this.cur || (this.cur = { b: 0 });
        cur.b = lerp(cur.b, this.target.b, Math.min(1, dt * 6));
        light.intensity = 22 * cur.b;
        bulbMat.emissiveIntensity = 1.6 * cur.b;
        shadeMat.emissiveIntensity = 0.9 * cur.b;
        glowSprite.material.opacity = 0.55 * cur.b;
      },
    });
  }

  _sensorDevice(id, pos, level, dir) {
    const { g, halo } = this._deviceGroup(id, pos, level);
    const body = new THREE.Mesh(new THREE.BoxGeometry(0.22, 0.3, 0.12), this.mats.white);
    g.add(body);
    const ledMat = new THREE.MeshStandardMaterial({ color: 0x9fe6c0, emissive: new THREE.Color(0x7dcea0), emissiveIntensity: 1.2 });
    const led = new THREE.Mesh(new THREE.SphereGeometry(0.022, 8, 8), ledMat);
    led.position.set(0.06, 0.1, 0.07);
    g.add(led);
    const slot = new THREE.Mesh(new THREE.BoxGeometry(0.13, 0.08, 0.02), this.mats.dark);
    slot.position.set(0, -0.06, 0.07);
    g.add(slot);
    g.rotation.y = dir > 0 ? Math.PI / 2 : -Math.PI / 2;

    const label = new Label({ text: "--", worldHeight: 0.36, color: "#d8f0e4", accent: "rgba(125,206,160,.6)", font: "700 60px 'PingFang SC',sans-serif", w: 512, h: 140 });
    label.sprite.position.set(0, 0.52, 0);
    g.add(label.sprite);

    this.deviceViews.set(id, {
      id, type: "sensor", group: g, halo,
      update(state) {
        const t = state.temperature?.toFixed(1) ?? "--";
        const h = Math.round(state.humidity ?? 0);
        label.setText(`${t}°C · ${h}%`);
        const hot = (state.temperature ?? 0) > 28;
        const cold = (state.temperature ?? 0) < 22;
        ledMat.color.set(hot ? 0xffb08a : cold ? 0x9ec8e0 : 0x9fe6c0);
        ledMat.emissive.set(hot ? 0xe07a5f : cold ? 0x8ecae6 : 0x7dcea0);
      },
      tick() {},
    });
  }

  _acDevice(id, pos, level, dir) {
    const { g, halo } = this._deviceGroup(id, pos, level);
    const body = new THREE.Mesh(new THREE.BoxGeometry(0.26, 0.62, 1.5), this.mats.white);
    g.add(body);
    const grille = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.16, 1.34), this.mats.dark);
    grille.position.set(0.14, -0.16, 0);
    g.add(grille);
    const fan = new THREE.Group();
    fan.position.set(0.16, -0.16, 0);
    g.add(fan);
    for (let i = 0; i < 5; i++) {
      const blade = new THREE.Mesh(new THREE.BoxGeometry(0.02, 0.3, 0.06), this.mats.metal);
      blade.rotation.x = (i / 5) * Math.PI * 2;
      blade.position.set(0, 0, 0);
      const pivot = new THREE.Group();
      pivot.add(blade);
      blade.position.y = 0.15;
      blade.rotation.x = 0;
      pivot.rotation.x = (i / 5) * Math.PI * 2;
      fan.add(pivot);
    }
    const ledMat = new THREE.MeshStandardMaterial({ color: 0x7dcea0, emissive: new THREE.Color(0x7dcea0), emissiveIntensity: 0 });
    const led = new THREE.Mesh(new THREE.SphereGeometry(0.02, 8, 8), ledMat);
    led.position.set(0.14, 0.22, 0.6);
    g.add(led);
    const airflow = new THREE.Mesh(
      new THREE.ConeGeometry(0.85, 1.9, 16, 1, true),
      new THREE.MeshBasicMaterial({ color: 0x8ecae6, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false }),
    );
    airflow.rotation.z = Math.PI / 2;
    airflow.position.set(1.05, -0.3, 0);
    g.add(airflow);
    g.rotation.y = dir > 0 ? 0 : Math.PI;

    this.deviceViews.set(id, {
      id, type: "ac", group: g, halo,
      update(state) {
        const on = !!state.power;
        this.on = on;
        this.speed = state.fan === "high" ? 9 : state.fan === "mid" ? 6 : state.fan === "low" ? 3 : 4.5;
        this.cooling = state.mode === "cool";
        ledMat.emissiveIntensity = on ? 1.4 : 0;
        ledMat.color.set(this.cooling ? 0x8ecae6 : 0xe2b15a);
        ledMat.emissive.set(this.cooling ? 0x8ecae6 : 0xe2b15a);
        this.targetAir = on ? (this.cooling ? 0.22 : 0.18) : 0;
        airflow.material.color.set(this.cooling ? 0x8ecae6 : 0xe07a5f);
      },
      tick(dt) {
        const cur = this.cur || (this.cur = { air: 0 });
        cur.air = lerp(cur.air, this.targetAir ?? 0, Math.min(1, dt * 2.4));
        airflow.material.opacity = cur.air;
        if (this.on) fan.rotation.x += dt * (this.speed || 4);
      },
    });
  }

  _cameraDevice(id, pos, level) {
    const { g, halo } = this._deviceGroup(id, pos, level);
    const mount = new THREE.Mesh(new THREE.BoxGeometry(0.22, 0.08, 0.16), this.mats.metal);
    mount.position.y = 0.16;
    g.add(mount);
    const yaw = new THREE.Group();
    yaw.position.y = 0.08;
    g.add(yaw);
    const body = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.12, 0.22), this.mats.dark);
    yaw.add(body);
    const lensMat = new THREE.MeshStandardMaterial({ color: 0x090d10, roughness: 0.15, metalness: 0.5 });
    const lens = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.05, 0.08, 16), lensMat);
    lens.rotation.x = Math.PI / 2;
    lens.position.set(0, -0.01, 0.14);
    yaw.add(lens);
    const ringMat = new THREE.MeshStandardMaterial({ color: 0xe07a5f, emissive: new THREE.Color(0xe07a5f), emissiveIntensity: 0.4 });
    const ring = new THREE.Mesh(new THREE.TorusGeometry(0.055, 0.01, 8, 18), ringMat);
    ring.rotation.x = Math.PI / 2;
    ring.position.set(0, -0.01, 0.175);
    yaw.add(ring);
    const cone = new THREE.Mesh(
      new THREE.ConeGeometry(1.6, 5.2, 20, 1, true),
      new THREE.MeshBasicMaterial({ color: 0xe07a5f, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending }),
    );
    cone.rotation.x = Math.PI / 2;
    cone.position.set(0, -0.55, 2.6);
    yaw.add(cone);

    // 云台朝向由 yaw 驱动。画面不再由前端渲染 —— 它走服务端的 MJPEG 流通道
    // （server/render/ 负责成像，见 README 第七节），所以这里不再挂相机。
    this.deviceViews.set(id, {
      id, type: "camera", group: g, halo, yaw,
      update(state) {
        this.targetPan = THREE.MathUtils.degToRad(state.pan || 0);
        this.streaming = !!state.streaming;
        this.motion = !!state.motion;
        ringMat.color.set(this.motion ? 0xef6f6c : this.streaming ? 0x7dcea0 : 0x6b7280);
        ringMat.emissive.set(this.motion ? 0xef6f6c : this.streaming ? 0x7dcea0 : 0x3a4249);
        ringMat.emissiveIntensity = this.motion ? 1.8 : this.streaming ? 1.1 : 0.2;
        this.targetCone = state.armed ? 0.1 : 0;
        cone.material.color.set(this.motion ? 0xef6f6c : 0xe07a5f);
      },
      tick(dt) {
        const p = this.curPan ?? this.targetPan ?? 0;
        this.curPan = lerp(p, this.targetPan ?? 0, Math.min(1, dt * 4));
        yaw.rotation.y = this.curPan;
        const c = this.curCone || (this.curCone = { v: 0 });
        c.v = lerp(c.v, this.targetCone ?? 0, Math.min(1, dt * 3));
        cone.material.opacity = c.v + (this.motion ? 0.05 : 0);
      },
    });
  }

  _lockDevice(id, pos, level) {
    const { g, halo } = this._deviceGroup(id, pos, level);
    // 门框
    const jamb = this.mats.frame;
    this._box(0.1, DOOR_H + 0.12, 0.16, jamb, -DOOR_W / 2 - 0.05, (DOOR_H + 0.12) / 2, 0, g);
    this._box(0.1, DOOR_H + 0.12, 0.16, jamb, DOOR_W / 2 + 0.05, (DOOR_H + 0.12) / 2, 0, g);
    this._box(DOOR_W + 0.2, 0.12, 0.16, jamb, 0, DOOR_H + 0.06, 0, g);
    // 门扇（绕左侧合页旋转）
    const hinge = new THREE.Group();
    hinge.position.set(-DOOR_W / 2, 0, 0.02);
    g.add(hinge);
    const leaf = new THREE.Mesh(new THREE.BoxGeometry(DOOR_W - 0.06, DOOR_H - 0.04, 0.06), this.mats.wood2);
    leaf.position.set(DOOR_W / 2, (DOOR_H - 0.04) / 2, 0);
    leaf.castShadow = true;
    hinge.add(leaf);
    const handle = new THREE.Mesh(new THREE.CylinderGeometry(0.025, 0.025, 0.24, 10), this.mats.metal);
    handle.rotation.x = Math.PI / 2;
    handle.position.set(DOOR_W - 0.22, 1.05, -0.07);
    hinge.add(handle);
    // 门锁面板（装在门内侧墙边）
    const panel = new THREE.Group();
    panel.position.set(DOOR_W / 2 + 0.34, 1.35, -0.1);
    g.add(panel);
    const panelBody = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.34, 0.05), this.mats.dark);
    panel.add(panelBody);
    const padMat = new THREE.MeshStandardMaterial({ color: 0x9ec8e0, emissive: new THREE.Color(0x8ecae6), emissiveIntensity: 0.5, roughness: 0.4 });
    const pad = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.14, 0.01), padMat);
    pad.position.set(0, 0.06, -0.03);
    panel.add(pad);
    const ledMat = new THREE.MeshStandardMaterial({ color: 0xef6f6c, emissive: new THREE.Color(0xef6f6c), emissiveIntensity: 1.4 });
    const led = new THREE.Mesh(new THREE.SphereGeometry(0.022, 10, 10), ledMat);
    led.position.set(0, -0.1, -0.035);
    panel.add(led);
    const scanRing = new THREE.Mesh(
      new THREE.TorusGeometry(0.13, 0.012, 8, 24),
      new THREE.MeshBasicMaterial({ color: 0xe2b15a, transparent: true, opacity: 0 }),
    );
    scanRing.position.set(0, 0, -0.05);
    panel.add(scanRing);
    panel.rotation.y = Math.PI;

    this.deviceViews.set(id, {
      id, type: "lock", group: g, halo,
      update(state) {
        this.locked = !!state.locked;
        this.targetOpen = this.locked ? 0 : 1.42;
        const pass = state.lastResult === "pass";
        const reject = state.lastResult === "reject";
        const scanning = state.lastResult === "scanning";
        ledMat.color.set(pass ? 0x7dcea0 : reject ? 0xef6f6c : this.locked ? 0xef6f6c : 0x7dcea0);
        ledMat.emissive.set(pass ? 0x7dcea0 : reject ? 0xef6f6c : this.locked ? 0xef6f6c : 0x7dcea0);
        ledMat.emissiveIntensity = scanning ? 2.2 : 1.4;
        padMat.emissiveIntensity = scanning ? 1.6 : 0.4;
        scanRing.material.color.set(pass ? 0x7dcea0 : reject ? 0xef6f6c : 0xe2b15a);
        this.targetRing = scanning ? 0.85 : pass || reject ? 0.5 : 0;
      },
      tick(dt, t) {
        const cur = this.cur || (this.cur = { open: 0, ring: 0 });
        cur.open = lerp(cur.open, this.targetOpen ?? 0, Math.min(1, dt * 3.2));
        hinge.rotation.y = cur.open;
        cur.ring = lerp(cur.ring, this.targetRing ?? 0, Math.min(1, dt * 4));
        scanRing.material.opacity = cur.ring * (0.6 + 0.4 * Math.sin(t * 5));
        scanRing.scale.setScalar(1 + 0.25 * Math.sin(t * 4));
      },
    });
  }

  /* ---------- 交互 ---------- */
  _initPicking() {
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    let downAt = null;
    const dom = this.renderer.domElement;
    dom.addEventListener("pointerdown", (e) => { downAt = { x: e.clientX, y: e.clientY, t: performance.now() }; });
    dom.addEventListener("pointerup", (e) => {
      if (!downAt) return;
      const moved = Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y);
      const dt = performance.now() - downAt.t;
      downAt = null;
      if (moved > 6 || dt > 400) return;
      const hit = this._pick(e);
      this.select(hit, { fromScene: true });
    });
  }

  _pick(e) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    this.pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObjects(this.deviceRoot.children, true);
    for (const h of hits) {
      let o = h.object;
      while (o && !o.userData.deviceId) o = o.parent;
      if (o?.userData.deviceId) return o.userData.deviceId;
    }
    return null;
  }

  onSelect(cb) { this.selectHandlers.push(cb); }

  select(id, { fromScene = false } = {}) {
    this.selection = id;
    for (const view of this.deviceViews.values()) {
      const on = view.id === id;
      view.halo.material.opacity = on ? 0.85 : 0;
      view.halo.material.color.set(0xe2b15a);
      view.halo.scale.setScalar(on ? 2.2 : 1.6);
    }
    if (!fromScene) this._focus(id);
    for (const cb of this.selectHandlers) cb(id);
  }

  _focus(id) {
    const view = this.deviceViews.get(id);
    if (!view) return;
    const p = new THREE.Vector3();
    view.group.getWorldPosition(p);
    this._tweenCamera(p, 9);
  }

  _tweenCamera(target, dist) {
    const dir = new THREE.Vector3().subVectors(this.camera.position, this.controls.target).normalize();
    const desired = new THREE.Vector3().copy(target).addScaledVector(dir, dist).add(new THREE.Vector3(0, dist * 0.35, 0));
    this.tween = { from: this.camera.position.clone(), to: desired, tFrom: this.controls.target.clone(), toT: target.clone(), t: 0 };
  }

  /* ---------- 视图模式 ---------- */
  setFloor(mode) {
    this.floorMode = mode;
    const l1 = this.levels[1], l2 = this.levels[2];
    l1.visible = mode !== "2";
    l2.visible = mode !== "1";
    // 全屋视图把二层地面调成半透明，否则一层完全看不见
    const fade = mode === "all";
    for (const m of this.upperFadeMats) {
      m.transparent = fade;
      m.opacity = fade ? 0.14 : 1;
      m.depthWrite = !fade;
      m.needsUpdate = true;
    }
    this._syncRoof();
    if (mode === "1") this._tweenCamera(new THREE.Vector3(-1, 1.2, 1.5), 16);
    else if (mode === "2") this._tweenCamera(new THREE.Vector3(-1, Y2 + 1.2, 1.5), 16);
    else this._tweenCamera(new THREE.Vector3(0, 2.6, 0), 26);
  }

  setRoof(on) {
    this.roofOn = on;
    this._syncRoof();
  }

  _syncRoof() {
    const exterior = this.roofOn;
    this.roofGroup.visible = exterior && this.floorMode !== "2";
    for (const m of this.frontWalls) m.visible = exterior;
    for (const l of this.roomLabels) {
      const floorOk = this.floorMode === "all" || String(l.floor) === this.floorMode;
      l.sprite.visible = !exterior && floorOk;
    }
  }

  resize() {
    const w = this.canvas.clientWidth || window.innerWidth;
    const h = this.canvas.clientHeight || window.innerHeight;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
  }

  /* ---------- 数据入口 ---------- */
  applySnapshot(snap) {
    if (!snap?.devices) return;
    for (const d of snap.devices) this.applyDeviceState({ deviceId: d.id, state: d.state });
  }

  applyDeviceState(msg) {
    const view = this.deviceViews.get(msg.deviceId);
    if (!view || !msg.state) return;
    view.update(msg.state, false);
    if (view.snap) view.snap();
    if (view.tick) view.tick(0.016, this.time);
  }

  /* ---------- 主循环 ---------- */
  _loop() {
    const step = () => {
      this._raf = requestAnimationFrame(step);
      const dt = Math.min(this.clock.getDelta(), 0.05);
      this.time += dt;
      if (this.tween) {
        const tw = this.tween;
        tw.t = Math.min(1, tw.t + dt * 1.7);
        const e = 1 - Math.pow(1 - tw.t, 3);
        this.camera.position.lerpVectors(tw.from, tw.to, e);
        this.controls.target.lerpVectors(tw.tFrom, tw.toT, e);
        if (tw.t >= 1) this.tween = null;
      }
      for (const view of this.deviceViews.values()) view.tick?.(dt, this.time);
      for (const v of this.deviceViews.values()) {
        if (v.halo.material.opacity > 0) {
          v.halo.material.opacity = 0.55 + 0.3 * Math.sin(this.time * 3);
        }
      }
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    };
    step();
  }
}
