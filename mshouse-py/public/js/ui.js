/**
 * 界面层
 * 只做「渲染 + 收集用户操作」，通过 handlers 把意图交回 main.js。
 * 不认识 WebSocket，也不认识 Three.js —— 三层互不耦合，方便替换。
 */

const $ = (sel) => document.querySelector(sel);

function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text != null) n.textContent = text;
  return n;
}

/**
 * 拼一路视频流的地址：
 *   配置了独立推流端口 → http://当前主机:端口/（和真实 IP 摄像头一致）；
 *   否则复用主端口     → /?stream=<通道号>（相对地址，跟页面同源）。
 */
export function streamSrc(info) {
  if (info?.port) return `http://${location.hostname}:${info.port}/`;
  return `/?stream=${info.channel}`;
}

const TYPE_LABEL = { light: "灯光", sensor: "传感器", ac: "空调", camera: "摄像头", lock: "门锁" };
const ROOM_LABEL = { living: "客厅", kitchen: "厨房", bedroom: "主卧", bath: "卫生间", entry: "门厅" };
const MODE_LABEL = { cool: "制冷", heat: "制热", fan: "送风", dry: "除湿", auto: "自动" };
const FAN_LABEL = { low: "低风", mid: "中风", high: "高风", auto: "自动" };

export class Dashboard {
  constructor(handlers) {
    this.h = handlers;            // { send, select, ping, scene }
    this.cards = new Map();       // deviceId -> { sync, root }
    this.statusRows = new Map();
    this.wireCount = 0;
    this.deviceIndex = new Map();
    this._wireTimer = null;
    this._videoReady = false;       // 「画面」标签是否已首次打开（懒挂载 MJPEG）
    this.lockSource = null;         // 大门锁当前画面来源（快照下发，用于初始化弹窗预填）
    this.enrollToken = null;        // 未登记人脸登记询问的当前令牌
    this._bindChrome();
  }

  /* ---------------- 顶栏 / 标签页 / 楼层 ---------------- */
  _bindChrome() {
    for (const btn of document.querySelectorAll("#tabs button")) {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
        document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${btn.dataset.pane}`));
        // MJPEG 是一条永不结束的 HTTP 长连接：在面板 display:none 时挂流，
        // 部分浏览器会暂缓首帧解码；更不能反复 remove/重挂 src（会 abort 连接
        // 造成重连风暴）。所以画面采用懒挂载：第一次打开「画面」标签才挂流，
        // 之后一直保持，切走再切回也不动它（新窗口打开本来就不受影响）。
        if (btn.dataset.pane === "video" && !this._videoReady) {
          this._videoReady = true;
          for (const id of ["camStream", "lockStream"]) {
            const img = document.getElementById(id);
            if (img?.dataset.src) img.setAttribute("src", img.dataset.src);
          }
        }
      });
    }
    const floors = { floorAll: "all", floor1: "1", floor2: "2" };
    for (const [id, mode] of Object.entries(floors)) {
      const node = document.getElementById(id);
      node?.addEventListener("click", () => {
        for (const key of Object.keys(floors)) document.getElementById(key)?.classList.toggle("on", key === id);
        this.h.floor?.(mode);
      });
    }
    const roof = document.getElementById("roofToggle");
    let roofOn = false;
    const syncRoof = () => {
      roof.classList.toggle("on", roofOn);
      roof.textContent = roofOn ? "盖上屋顶" : "掀开屋顶";
      this.h.roof?.(roofOn);
    };
    roof?.addEventListener("click", () => { roofOn = !roofOn; syncRoof(); });
    syncRoof();

    document.getElementById("clearWire")?.addEventListener("click", () => this.clearWire());
    document.getElementById("pingBtn")?.addEventListener("click", () => this.h.ping?.());
    document.getElementById("lockNow")?.addEventListener("click", () => this.h.send?.({
      type: "command", payload: { deviceId: "lock.entry", action: "lock", params: {} },
    }));
    document.getElementById("unlockNow")?.addEventListener("click", () => this.h.send?.({
      type: "command", payload: { deviceId: "lock.entry", action: "unlock", params: {} },
    }));
    document.getElementById("enrollOk")?.addEventListener("click", () => {
      const token = this.enrollToken;
      if (!token) return;
      const name = $("#enrollName")?.value?.trim() || "";
      $("#enrollOk").disabled = true;
      this.h.send?.({
        type: "command",
        payload: { deviceId: "lock.entry", action: "enrollFace", params: { token, name } },
      });
    });
    document.getElementById("enrollSkip")?.addEventListener("click", () => {
      const token = this.enrollToken;
      if (!token) return;
      this.h.send?.({
        type: "command",
        payload: { deviceId: "lock.entry", action: "dismissEnroll", params: { token } },
      });
    });

    // 上传人脸画面：把图片二进制 POST 给网关，画面从门外草坪道路切到上传的人脸
    const faceFile = document.getElementById("faceFile");
    document.getElementById("faceUpload")?.addEventListener("click", () => faceFile?.click());
    faceFile?.addEventListener("change", () => {
      const file = faceFile.files?.[0];
      if (file) this.h.uploadFace?.(file);
      faceFile.value = "";
    });
    document.getElementById("faceClear")?.addEventListener("click", () => this.h.clearFace?.());

    const menu = document.getElementById("menuToggle");
    // 默认隐藏控制面板；只有用户上次明确选择“显示”时才保持展开
    const saved = localStorage.getItem("mshouse.panel");
    this.setPanelHidden(saved !== "shown");
    menu?.addEventListener("click", () => this.setPanelHidden(!document.body.classList.contains("panel-hidden")));

    document.getElementById("setupBtn")?.addEventListener("click", () => this.openSetup());
    document.getElementById("setupCancel")?.addEventListener("click", () => this.closeSetup());
    document.getElementById("setupCancel2")?.addEventListener("click", () => this.closeSetup());
    document.getElementById("setupBack")?.addEventListener("click", () => this._showAuthPane());
    document.getElementById("setupGeo")?.addEventListener("click", () => this._fillGeo());
    document.getElementById("setupLockKind")?.addEventListener("change", () => this._syncLockSourceFields());
    document.getElementById("setupForm")?.addEventListener("submit", (e) => {
      e.preventDefault();
      // 同一个 form：口令页回车/点「下一步」走校验，卡片页点「保存设置」走提交
      if ($("#setupCardsPane")?.hidden) this._verifyPassword();
      else this._submitSetup();
    });

    // 画面面板：流控制按钮只发一条 command，地址由流信令带回来（见 setStream）
    const streamCmd = (deviceId, on) => this.h.send?.({
      type: "command", payload: { deviceId, action: "stream", params: { on } },
    });
    document.getElementById("camOn")?.addEventListener("click", () => streamCmd("camera.living", true));
    document.getElementById("camOff")?.addEventListener("click", () => streamCmd("camera.living", false));
    document.getElementById("lockOn")?.addEventListener("click", () => streamCmd("lock.entry", true));
    document.getElementById("lockOff")?.addEventListener("click", () => streamCmd("lock.entry", false));
    for (const [btnId, imgId] of [["camOpen", "camStream"], ["lockOpen", "lockStream"]]) {
      document.getElementById(btnId)?.addEventListener("click", () => {
        const img = document.getElementById(imgId);
        const src = img?.dataset.src || img?.getAttribute("src");
        if (src) window.open(src, "_blank");
      });
    }
  }

  /* ---------------- 场景 ---------------- */
  renderScenes(scenes, activeId) {
    const host = $("#scenes");
    if (!host) return;
    host.innerHTML = "";
    for (const s of Object.values(scenes)) {
      const b = el("button", "scene-btn", s.name.replace("模式", ""));
      b.title = s.hint;
      b.dataset.scene = s.id;
      b.classList.toggle("active", s.id === activeId);
      b.addEventListener("click", () => this.h.scene?.(s.id));
      host.appendChild(b);
    }
  }

  setSceneActive(id) {
    document.querySelectorAll("#scenes .scene-btn").forEach((b) => b.classList.toggle("active", b.dataset.scene === id));
  }

  /* ---------------- 顶部状态 ---------------- */
  setConnection(ok, text) {
    const dot = $("#linkDot");
    dot?.classList.toggle("ok", ok);
    dot?.classList.toggle("warn", !ok);
    const t = $("#linkText");
    if (t) t.textContent = text;
  }

  setOccupancy(text) { const n = $("#occupancy"); if (n) n.textContent = text; }
  setOutdoor(text) { const n = $("#outdoor"); if (n) n.textContent = text; }
  setOnline(text) { const n = $("#onlineCount"); if (n) n.textContent = text; }

  setSite(site) {
    this.site = site || null;
    const btn = $("#setupBtn");
    if (!btn) return;
    // 按钮名称固定为「设置」，用高亮与悬停提示表达配置状态
    btn.textContent = "设置";
    if (site?.configured) {
      btn.classList.add("on");
      btn.title = `${site.name || ""}${site.address ? "\n" + site.address : ""}\n${site.lat}, ${site.lon}`;
    } else {
      btn.classList.remove("on");
      btn.title = site?.name ? `当前默认：${site.name}（尚未写入定位）` : "尚未写入物理位置";
    }
  }

  setPanelHidden(hidden) {
    document.body.classList.toggle("panel-hidden", hidden);
    const btn = $("#menuToggle");
    if (btn) {
      btn.classList.toggle("on", !hidden);
      btn.setAttribute("aria-pressed", hidden ? "false" : "true");
      btn.textContent = hidden ? "控制" : "隐藏";
    }
    try { localStorage.setItem("mshouse.panel", hidden ? "hidden" : "shown"); } catch { /* 忽略隐私模式 */ }
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }

  openSetup() {
    const dlg = $("#setupDialog");
    if (!dlg) return;
    this._setupVerified = false;
    this._setupPassword = "";
    $("#setupPassword").value = "";
    this._setError("#setupAuthError", null);
    this._setError("#setupError", null);
    this.setAuthBusy(false);
    this.setSetupBusy(false);
    this._showAuthPane();
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
  }

  _showAuthPane() {
    const auth = $("#setupAuthPane");
    const cards = $("#setupCardsPane");
    if (auth) auth.hidden = false;
    if (cards) cards.hidden = true;
    $("#setupPassword")?.focus();
  }

  _showCardsPane() {
    const auth = $("#setupAuthPane");
    const cards = $("#setupCardsPane");
    if (auth) auth.hidden = true;
    if (cards) cards.hidden = false;
    this._prefillSetupCards();
  }

  /** 口令通过后用最新快照预填卡片（未配置时网关下发温州默认值） */
  _prefillSetupCards() {
    const s = this.site || {};
    $("#setupName").value = s.name || "";
    $("#setupAddress").value = s.address || "";
    $("#setupLat").value = s.lat ?? "";
    $("#setupLon").value = s.lon ?? "";
    const src = this.lockSource || { kind: "image", index: 0, url: "" };
    const kindSel = $("#setupLockKind");
    if (kindSel) kindSel.value = src.kind || "image";
    const idx = $("#setupLockIndex");
    if (idx) idx.value = String(src.index ?? 0);
    const url = $("#setupLockUrl");
    if (url) url.value = src.url || "";
    this._syncLockSourceFields();
  }

  setLockSource(source) {
    this.lockSource = source || null;
  }

  _syncLockSourceFields() {
    const kind = $("#setupLockKind")?.value || "image";
    const idxWrap = $("#setupLockIndexWrap");
    const urlWrap = $("#setupLockUrlWrap");
    if (idxWrap) idxWrap.hidden = kind !== "camera";
    if (urlWrap) urlWrap.hidden = kind !== "stream";
  }

  closeSetup() {
    const dlg = $("#setupDialog");
    dlg?.close?.();
    dlg?.removeAttribute("open");
  }

  _setError(selector, error) {
    const err = $(selector);
    if (!err) return;
    err.hidden = !error;
    err.textContent = error || "";
  }

  setAuthBusy(busy, error) {
    const btn = $("#setupAuthBtn");
    if (btn) {
      btn.disabled = busy;
      btn.textContent = busy ? "正在校验…" : "下一步";
    }
    if (error !== undefined) this._setError("#setupAuthError", error);
  }

  setSetupBusy(busy, error) {
    const btn = $("#setupSubmit");
    if (btn) {
      btn.disabled = busy;
      btn.textContent = busy ? "正在保存…" : "保存设置";
    }
    if (error !== undefined) this._setError("#setupError", error);
  }

  /** setupAuth 的 ack 回来后由 main.js 回调 */
  setupAuthResult(ok, error) {
    if (ok) {
      this._setupVerified = true;
      this._setupPassword = $("#setupPassword")?.value || "";
      this._setError("#setupAuthError", null);
      this.setAuthBusy(false);
      this._showCardsPane();
    } else {
      this.setAuthBusy(false, error || "口令校验失败");
    }
  }

  _verifyPassword() {
    const password = $("#setupPassword")?.value || "";
    if (!password) { this.setAuthBusy(false, "请输入初始化口令"); return; }
    this.setAuthBusy(true);
    this.h.setupAuth?.(password);
  }

  _fillGeo() {
    const hint = $("#setupHint");
    if (!navigator.geolocation) {
      if (hint) hint.textContent = "当前浏览器不支持定位，请手填经纬度。";
      return;
    }
    if (hint) hint.textContent = "正在请求浏览器定位…";
    navigator.geolocation.getCurrentPosition((pos) => {
      $("#setupLat").value = pos.coords.latitude.toFixed(4);
      $("#setupLon").value = pos.coords.longitude.toFixed(4);
      if (hint) hint.textContent = "已填入当前位置，保存后写入。";
    }, () => {
      if (hint) hint.textContent = "定位被拒绝，请手填经纬度。";
    }, { enableHighAccuracy: false, timeout: 8000 });
  }

  _submitSetup() {
    if (!this._setupVerified) { this._showAuthPane(); return; }
    // 经纬度整体可留空；只填一个要求补齐
    const latRaw = $("#setupLat")?.value?.trim() ?? "";
    const lonRaw = $("#setupLon")?.value?.trim() ?? "";
    if (!latRaw !== !lonRaw) { this.setSetupBusy(false, "经纬度需同时填写，或同时留空"); return; }
    let lat = null, lon = null;
    if (latRaw) {
      lat = Number(latRaw); lon = Number(lonRaw);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) { this.setSetupBusy(false, "请填写合法经纬度"); return; }
    }
    const name = $("#setupName")?.value?.trim() || "";
    const address = $("#setupAddress")?.value?.trim() || "";
    const kind = $("#setupLockKind")?.value || "image";
    const index = Math.max(0, Math.min(15, Number($("#setupLockIndex")?.value) || 0));
    const url = $("#setupLockUrl")?.value?.trim() || "";
    if (kind === "stream" && !url) { this.setSetupBusy(false, "选择网络视频流时必须填写流地址"); return; }
    this.setSetupBusy(true);
    const payload = {
      password: this._setupPassword,
      name, address,
      lockSource: { kind, index, url },
    };
    if (lat != null) { payload.lat = lat; payload.lon = lon; }
    this.h.setup?.(payload);
  }

  toast(text) {
    const t = $("#toast");
    if (!t) return;
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => t.classList.remove("show"), 1900);
  }

  /** 每次访问首页的「欢迎回家」浮层，约 2.6 秒后自动淡出 */
  welcomeHome() {
    const w = $("#welcomeHome");
    if (!w) return;
    w.classList.add("show");
    clearTimeout(this._welcomeTimer);
    this._welcomeTimer = setTimeout(() => w.classList.remove("show"), 2600);
  }

  /* ---------------- 设备控制卡 ---------------- */
  renderDevices(devices) {
    const host = $("#pane-control");
    if (!host) return;
    host.innerHTML = "";
    this.cards.clear();
    this.statusRows.clear();
    this.deviceIndex.clear();
    const statusHost = $("#statusList");
    if (statusHost) statusHost.innerHTML = "";

    const byFloor = { 1: [], 2: [] };
    for (const d of devices) {
      this.deviceIndex.set(d.id, d);
      (byFloor[d.floor] ||= []).push(d);
    }
    for (const floor of [1, 2]) {
      if (!byFloor[floor]?.length) continue;
      host.appendChild(el("h3", "meta", `第 ${floor} 层 · ${floor === 1 ? "公共区" : "私密区"}`));
      host.lastChild.style.margin = "6px 4px 8px";
      for (const d of byFloor[floor]) host.appendChild(this._buildCard(d));
    }

    if (statusHost) {
      for (const floor of [1, 2]) {
        for (const d of byFloor[floor] || []) {
          const row = el("div", "card");
          row.style.padding = "9px 11px";
          row.style.marginBottom = "6px";
          const head = el("div", "row");
          head.style.justifyContent = "space-between";
          const left = el("div");
          left.appendChild(el("h3", null, d.name));
          const sub = el("p", "meta", `${TYPE_LABEL[d.type] || d.type} · ${ROOM_LABEL[d.room] || d.room}`);
          sub.style.margin = "2px 0 0";
          left.appendChild(sub);
          const val = el("b", null, "—");
          val.style.fontSize = "12px";
          head.append(left, val);
          row.appendChild(head);
          statusHost.appendChild(row);
          this.statusRows.set(d.id, val);
        }
      }
    }
    for (const d of devices) this.updateDevice(d.id, d.state, d);
  }

  _buildCard(d) {
    const card = el("div", "card");
    card.dataset.device = d.id;
    const head = el("header");
    const title = el("h3", null, d.name);
    const tag = el("span", "meta", `${TYPE_LABEL[d.type] || d.type} · ${ROOM_LABEL[d.room] || d.room}`);
    head.append(title, tag);
    card.appendChild(head);
    const ctrl = el("div", "ctrl");
    card.appendChild(ctrl);
    const api = { root: card, sync: () => {} };

    const send = (action, params) => this.h.send({ type: "command", payload: { deviceId: d.id, action, params } });

    if (d.type === "light") {
      const btn = el("button", "primary", "开灯");
      btn.addEventListener("click", () => send(btn.dataset.on === "1" ? "off" : "on", {}));
      const row = el("div", "row");
      row.appendChild(btn);
      ctrl.appendChild(row);

      const mk = (label, min, max, step, key, fmt) => {
        const wrap = el("label");
        const cap = el("span", null, label);
        const val = el("b", null, "");
        cap.appendChild(document.createTextNode(" "));
        const range = el("input");
        range.type = "range"; range.min = min; range.max = max; range.step = step;
        let timer = null;
        range.addEventListener("input", () => {
          val.textContent = fmt(range.value);
          clearTimeout(timer);
          timer = setTimeout(() => send("set", { [key]: Number(range.value) }), 70);
        });
        wrap.append(cap, val, range);
        wrap.style.display = "block";
        ctrl.appendChild(wrap);
        return { range, val, fmt };
      };
      const bright = mk("亮度", 1, 100, 1, "brightness", (v) => `${v}%`);
      const ct = mk("色温", 2700, 6500, 100, "colorTemp", (v) => `${v}K`);
      api.sync = (s) => {
        const on = !!s.power;
        btn.dataset.on = on ? "1" : "0";
        btn.textContent = on ? "关灯" : "开灯";
        btn.classList.toggle("primary", on);
        bright.range.value = s.brightness ?? 0;
        bright.val.textContent = `${Math.round(s.brightness ?? 0)}%`;
        ct.range.value = s.colorTemp ?? 4000;
        ct.val.textContent = `${Math.round(s.colorTemp ?? 4000)}K`;
      };
    } else if (d.type === "sensor") {
      const big = el("div");
      big.style.display = "flex";
      big.style.gap = "10px";
      const t = el("b", null, "--");
      const hh = el("b", null, "--");
      const c = el("span", "meta", "--");
      t.style.fontSize = "20px";
      hh.style.fontSize = "20px";
      big.append(t, hh, c);
      ctrl.appendChild(big);
      api.sync = (s) => {
        t.textContent = `${(s.temperature ?? 0).toFixed(1)}°C`;
        hh.textContent = `${Math.round(s.humidity ?? 0)}%`;
        c.textContent = s.comfort || "";
      };
    } else if (d.type === "ac") {
      const btn = el("button", "primary", "开空调");
      btn.addEventListener("click", () => send(btn.dataset.on === "1" ? "off" : "on", {}));
      const row = el("div", "row");
      row.appendChild(btn);
      ctrl.appendChild(row);

      const modeSel = el("select");
      for (const [k, v] of Object.entries(MODE_LABEL)) {
        const o = el("option", null, v); o.value = k; modeSel.appendChild(o);
      }
      modeSel.addEventListener("change", () => send("set", { mode: modeSel.value }));
      const modeRow = el("label", null, "模式");
      modeRow.style.display = "block";
      modeRow.appendChild(modeSel);
      ctrl.appendChild(modeRow);

      const temp = el("label");
      const cap = el("span", null, "设定温度");
      const val = el("b", null, "26°C");
      const range = el("input");
      range.type = "range"; range.min = 16; range.max = 30; range.step = 1;
      let timer = null;
      range.addEventListener("input", () => {
        val.textContent = `${range.value}°C`;
        clearTimeout(timer);
        timer = setTimeout(() => send("set", { targetTemp: Number(range.value) }), 80);
      });
      temp.append(cap, val, range);
      temp.style.display = "block";
      ctrl.appendChild(temp);

      const fanSel = el("select");
      for (const [k, v] of Object.entries(FAN_LABEL)) {
        const o = el("option", null, v); o.value = k; fanSel.appendChild(o);
      }
      fanSel.addEventListener("change", () => send("set", { fan: fanSel.value }));
      const fanRow = el("label", null, "风速");
      fanRow.style.display = "block";
      fanRow.appendChild(fanSel);
      ctrl.appendChild(fanRow);

      const indoor = el("p", "meta", "室内 --°C");
      ctrl.appendChild(indoor);

      api.sync = (s) => {
        const on = !!s.power;
        btn.dataset.on = on ? "1" : "0";
        btn.textContent = on ? "关空调" : "开空调";
        btn.classList.toggle("primary", on);
        modeSel.value = s.mode || "cool";
        fanSel.value = s.fan || "auto";
        range.value = s.targetTemp ?? 26;
        val.textContent = `${Math.round(s.targetTemp ?? 26)}°C`;
        indoor.textContent = `室内 ${(s.indoorTemp ?? 0).toFixed(1)}°C · 回风温差 ${((s.indoorTemp ?? 0) - (s.targetTemp ?? 26)).toFixed(1)}°C`;
      };
    } else if (d.type === "camera") {
      const btn = el("button", "primary", "关闭画面");
      btn.addEventListener("click", () => send("set", { power: btn.dataset.on !== "1" }));
      const armBtn = el("button", null, "布防");
      armBtn.addEventListener("click", () => send("arm", { armed: armBtn.dataset.armed !== "1" }));
      const left = el("button", "mini", "◀ 15°");
      const right = el("button", "mini", "15° ▶");
      left.addEventListener("click", () => send("nudge", { delta: -15 }));
      right.addEventListener("click", () => send("nudge", { delta: 15 }));
      const row = el("div", "row");
      row.append(btn, armBtn, left, right);
      ctrl.appendChild(row);

      const panWrap = el("label");
      const cap = el("span", null, "云台角度");
      const val = el("b", null, "0°");
      const range = el("input");
      range.type = "range"; range.min = 0; range.max = 359; range.step = 1;
      let timer = null;
      range.addEventListener("input", () => {
        val.textContent = `${range.value}°`;
        clearTimeout(timer);
        timer = setTimeout(() => send("pan", { pan: Number(range.value) }), 80);
      });
      panWrap.append(cap, val, range);
      panWrap.style.display = "block";
      ctrl.appendChild(panWrap);
      const meta = el("p", "meta", "—");
      ctrl.appendChild(meta);

      api.sync = (s) => {
        const on = !!s.power;
        btn.dataset.on = on ? "1" : "0";
        btn.textContent = on ? "关闭画面" : "开启画面";
        btn.classList.toggle("primary", on);
        armBtn.dataset.armed = s.armed ? "1" : "0";
        armBtn.textContent = s.armed ? "撤防" : "布防";
        armBtn.classList.toggle("primary", !!s.armed);
        range.value = Math.round(s.pan ?? 0);
        val.textContent = `${Math.round(s.pan ?? 0)}°`;
        meta.textContent = `360° 云台 · ${s.streaming ? "推流中" : "已停止"}${s.motion ? " · 检测到移动" : ""}`;
      };
    } else if (d.type === "lock") {
      const unlock = el("button", "primary", "开锁");
      unlock.addEventListener("click", () => send("unlock", {}));
      const lockBtn = el("button", null, "上锁");
      lockBtn.addEventListener("click", () => send("lock", {}));
      const row = el("div", "row");
      row.append(unlock, lockBtn);
      ctrl.appendChild(row);
      const meta = el("p", "meta", "—");
      ctrl.appendChild(meta);
      api.sync = (s) => {
        const locked = !!s.locked;
        unlock.classList.toggle("primary", !locked);
        lockBtn.classList.toggle("primary", locked);
        const r = s.lastResult;
        const text = r === "pass" ? "识别通过" : r === "reject" ? "识别拒绝" : locked ? "已上锁" : "已开锁";
        meta.textContent = `${text}${s.lastPerson ? " · " + s.lastPerson : ""} · 电量 ${s.battery ?? "--"}%`;
      };
    }

    if (api.sync) api.sync(d.state || {});
    card.addEventListener("click", (e) => {
      if (e.target.closest("button, input, select, label")) return;
      this.h.select?.(d.id);
    });
    this.cards.set(d.id, api);
    return card;
  }

  updateDevice(id, state, meta) {
    const card = this.cards.get(id);
    if (card) card.sync(state);
    const row = this.statusRows.get(id);
    if (row) {
      const d = meta || this.deviceIndex.get(id) || {};
      row.textContent = this._summary(d.type, state);
    }
  }

  _summary(type, s) {
    switch (type) {
      case "light": return s.power ? `开 · ${Math.round(s.brightness)}% · ${Math.round(s.colorTemp)}K` : "关";
      case "sensor": return `${(s.temperature ?? 0).toFixed(1)}°C · ${Math.round(s.humidity ?? 0)}% · ${s.comfort ?? ""}`;
      case "ac": return s.power ? `${MODE_LABEL[s.mode] || s.mode} · ${s.targetTemp}°C · ${FAN_LABEL[s.fan] || s.fan}` : "关";
      case "camera": return `${s.power ? "在线" : "离线"} · PAN ${Math.round(s.pan ?? 0)}°${s.armed ? " · 布防" : ""}`;
      case "lock": return `${s.locked ? "已上锁" : "已开锁"} · ${s.lastResult === "pass" ? "人脸通过" : s.lastResult === "reject" ? "人脸拒绝" : "待机"}`;
      default: return "—";
    }
  }

  highlightDevice(id) {
    for (const [key, api] of this.cards) {
      api.root.style.borderColor = key === id ? "rgba(226,177,90,.85)" : "";
      api.root.style.background = key === id ? "rgba(226,177,90,.08)" : "";
    }
    if (id) {
      const card = this.cards.get(id);
      card?.root.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  /* ---------------- 事件日志 ---------------- */
  addEvent(evt) {
    const host = $("#eventLog");
    if (!host) return;
    const item = el("div", "log-item");
    const time = new Date(evt.ts || Date.now()).toLocaleTimeString("zh-CN", { hour12: false });
    const lv = el("span", `lv ${evt.level || "info"}`, (evt.level || "info").toUpperCase());
    item.append(lv, el("span", "meta", time + " "), el("span", null, evt.message || ""));
    host.prepend(item);
    while (host.children.length > 60) host.lastChild.remove();
  }

  /* ---------------- 报文面板 ---------------- */
  addWire(dir, msg) {
    const host = $("#wireLog");
    if (!host) return;
    this.wireCount++;
    const count = $("#wireCount");
    if (count) count.textContent = `${this.wireCount} 条`;
    const item = el("div", "wire-item");
    const head = el("div");
    const time = new Date(msg.ts || Date.now()).toLocaleTimeString("zh-CN", { hour12: false });
    head.append(
      el("span", "dir", dir === "out" ? "↑ 发出 " : "↓ 收到 "),
      el("span", null, `${msg.type} `),
      el("span", "meta", `${time} ${msg.id ? "· " + msg.id : ""}`),
    );
    const pre = el("pre", null, JSON.stringify(msg, null, 2));
    item.append(head, pre);
    host.prepend(item);
    while (host.children.length > 40) host.lastChild.remove();
  }

  clearWire() {
    const host = $("#wireLog");
    if (host) host.innerHTML = "";
    this.wireCount = 0;
    const count = $("#wireCount");
    if (count) count.textContent = "0 条";
  }

  /* ---------------- 画面（HTTP 流） ---------------- */

  /**
   * 流信令到达时更新画面面板。
   * 画面本身不经过 WebSocket：这里只是把 /?stream=<通道> 交给 <img>，
   * 由浏览器原生播 MJPEG。信令里的 live 决定按钮与徽标状态。
   */
  setStream(deviceId, info) {
    const key = deviceId === "camera.living" ? "cam" : deviceId === "lock.entry" ? "lock" : null;
    if (!key || !info?.channel) return;
    const src = streamSrc(info);
    const img = $(`#${key}Stream`);
    if (img) {
      img.dataset.src = src;
      // 面板还没首次打开时只记下地址，不建立 MJPEG 连接；
      // 已打开后，地址没变就绝不重挂（重挂会 abort 正在播放的长连接）。
      if (this._videoReady && img.getAttribute("src") !== src) img.setAttribute("src", src);
    }

    const tag = img?.parentElement?.querySelector(".tag");
    if (tag) tag.textContent = `CH ${info.channel}`;

    const badge = $(`#${key}Badge`);
    if (badge) {
      badge.textContent = info.live ? `流 · 推流中 ${info.fps}fps` : "流 · 待机";
      badge.classList.toggle("live", !!info.live);
    }
    const on = $(`#${key}On`);
    const off = $(`#${key}Off`);
    if (on) on.disabled = !!info.live;
    if (off) off.disabled = !info.live;
  }

  setVideoMeta(deviceId, text) {
    const node = deviceId === "camera.living" ? $("#camMeta") : $("#lockMeta");
    if (node) node.textContent = text;
  }

  /* ---------------- 大门锁：未登记人脸登记询问 ---------------- */

  /** 网关询问「是否登记用户」（或快照里带着待确认询问） */
  showEnrollPrompt(p) {
    if (!p || !p.token) return;
    // 旧询问未关闭又来新询问时，替换为新的
    this.enrollToken = p.token;
    const banner = $("#lockEnrollBanner");
    const dist = $("#enrollDist");
    if (dist) {
      dist.textContent = (p.distance != null && p.threshold != null)
        ? `人脸最近距离 ${p.distance} > 阈值 ${p.threshold}，不在当前登记库中。`
        : "该人脸不在当前登记库中。";
    }
    const okBtn = $("#enrollOk");
    if (okBtn) okBtn.disabled = false;
    banner?.removeAttribute("hidden");
  }

  /** 登记请求被网关拒绝（如提示过期）：保留横幅、恢复按钮、提示原因 */
  enrollFailed(error) {
    const okBtn = $("#enrollOk");
    if (okBtn) okBtn.disabled = false;
    this.toast(error || "登记失败");
  }

  /** 询问结束：enrolled=已登记 / dismissed=已忽略 / gone=人已离开 / canceled|expired=撤销 */
  hideEnrollPrompt(p) {
    if (p?.token && this.enrollToken && p.token !== this.enrollToken) return;
    this.enrollToken = null;
    const banner = $("#lockEnrollBanner");
    banner?.setAttribute("hidden", "");
    const nameInput = $("#enrollName");
    if (nameInput) nameInput.value = "";
    const okBtn = $("#enrollOk");
    if (okBtn) okBtn.disabled = false;
    const map = {
      enrolled: `已登记新用户${p?.name ? `「${p.name}」` : ""}，下次刷脸即可自动开锁`,
      dismissed: "已忽略，本次人脸停留期间不再提示",
      gone: "人脸已离开画面",
      expired: "登记提示已过期",
      canceled: null,
    };
    const text = p ? map[p.status] : null;
    if (text) this.toast(text);
  }
}
