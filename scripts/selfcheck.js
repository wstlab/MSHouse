/**
 * 协议自检：以真实 WebSocket 客户端身份跑一遍完整交互。
 * 教学用途：这份脚本本身就是「如何对接网关」的最小示例。
 *   node scripts/selfcheck.js [ws://host:port/ws]
 */
import http from "node:http";
import { WebSocket } from "ws";
import { envelope, DEVICE_CATALOG } from "../shared/model.js";

const WS_URL = process.argv[2] || "ws://127.0.0.1:8080/ws";
const results = [];
let ws;
let idSeq = 1;

/** 从 MJPEG 流通道拉一段数据，验证像素通道真的在出图 */
function pullFrame(path, timeout = 6000) {
  return new Promise((resolve) => {
    const base = new URL(WS_URL);
    let req;
    let settled = false;
    const finish = (payload) => {
      if (settled) return;
      settled = true;
      try { req?.destroy(); } catch {}
      resolve(payload);
    };
    req = http.get({ host: base.hostname, port: base.port, path }, (res) => {
      const contentType = res.headers["content-type"] || "";
      let bytes = 0;
      const timer = setTimeout(() => finish({ ok: false, bytes, contentType }), timeout);
      res.on("data", (chunk) => {
        bytes += chunk.length;
        if (bytes >= 2000) { clearTimeout(timer); finish({ ok: true, bytes, contentType }); }
      });
      res.on("end", () => { clearTimeout(timer); finish({ ok: bytes >= 2000, bytes, contentType }); });
      res.on("error", () => { clearTimeout(timer); finish({ ok: false, bytes, contentType }); });
    });
    req.on("error", () => finish({ ok: false, bytes: 0, contentType: "" }));
  });
}

/** 取一个 JSON 接口（/streams、/health 之类） */
function getJson(path) {
  return new Promise((resolve) => {
    const base = new URL(WS_URL);
    http.get({ host: base.hostname, port: base.port, path }, (res) => {
      let body = "";
      res.on("data", (c) => { body += c; });
      res.on("end", () => { try { resolve(JSON.parse(body)); } catch { resolve(null); } });
    }).on("error", () => resolve(null));
  });
}

const nextId = () => `chk-${idSeq++}`;
const ok = (name, pass, detail = "") => {
  results.push({ name, pass, detail });
  console.log(`${pass ? "  ✓" : "  ✗"} ${name}${detail ? "  " + detail : ""}`);
};

function once(type, predicate = () => true, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.off("message", onMsg);
      reject(new Error(`等待 ${type} 超时`));
    }, timeout);
    function onMsg(raw) {
      let m;
      try { m = JSON.parse(raw.toString()); } catch { return; }
      if (m.type !== type) return;
      if (!predicate(m)) return;
      clearTimeout(timer);
      ws.off("message", onMsg);
      resolve(m);
    }
    ws.on("message", onMsg);
  });
}

const send = (msg) => ws.send(JSON.stringify(msg));

async function main() {
  console.log(`\nMSHouse 镜像家居 · 协议自检 → ${WS_URL}\n`);
  ws = new WebSocket(WS_URL);
  // 先挂监听再等 open，避免漏掉服务端建链即推的 hello / snapshot
  const helloP = once("hello");
  const snapP = once("snapshot");

  await new Promise((res, rej) => {
    ws.once("open", res);
    ws.once("error", (e) => rej(new Error(`无法连接 ${WS_URL}：${e.message}`)));
  });
  ok("WebSocket 建链", true);

  // 1. 握手 + 全量影子
  const [hello, snap] = await Promise.all([helloP, snapP]);
  ok("收到 hello 握手", hello.payload?.protocol === "mshouse/1.0", hello.payload?.name);
  ok("收到全量设备影子", Array.isArray(snap.payload?.devices), `${snap.payload.devices.length} 台设备`);
  ok("设备数量与物模型一致", snap.payload.devices.length === DEVICE_CATALOG.length,
    `期望 ${DEVICE_CATALOG.length}，实际 ${snap.payload.devices.length}`);
  ok("快照包含场景定义", Object.keys(snap.payload.scenes || {}).length >= 3);

  // 2. 下发命令 → ack + state 推送
  const ackP = once("ack", (m) => m.ref === "cmd-light");
  const stateP = once("state", (m) => m.payload?.deviceId === "light.living");
  send(envelope("command", { deviceId: "light.living", action: "set", params: { power: true, brightness: 42 } }, { id: "cmd-light" }));
  const [ack, st] = await Promise.all([ackP, stateP]);
  ok("命令返回 ack", ack.payload?.ok === true);
  ok("状态变更被推送", st.payload.state.power === true && st.payload.state.brightness === 42,
    `brightness=${st.payload.state.brightness}`);

  // 3. 场景联动
  const sceneP = once("scene");
  const snapAfterP = once("snapshot", (m) => m.payload?.scene === "sleep");
  send(envelope("scene", { sceneId: "sleep" }, { id: "cmd-scene" }));
  const sceneMsg = await sceneP;
  const snapAfter = await snapAfterP;
  const lights = Object.fromEntries(snapAfter.payload.devices.filter((d) => d.type === "light").map((d) => [d.id, d.state]));
  ok("场景广播生效", sceneMsg.payload.sceneId === "sleep", sceneMsg.payload.name);
  ok("睡眠模式：客厅/厨房灯关闭", lights["light.living"].power === false && lights["light.kitchen"].power === false);
  ok("睡眠模式：卧室留夜灯", lights["light.bedroom"].power === true && lights["light.bedroom"].brightness <= 15,
    `brightness=${lights["light.bedroom"].brightness}`);
  const lock = snapAfter.payload.devices.find((d) => d.id === "lock.entry");
  ok("睡眠模式：大门上锁", lock.state.locked === true);

  // 4. 人脸识别
  const faceStateP = once("state", (m) => m.payload?.deviceId === "lock.entry" && m.payload.state.lastResult === "pass");
  const evtP = once("event", (m) => m.payload?.source === "lock.entry");
  send(envelope("command", { deviceId: "lock.entry", action: "face", params: { faceId: "resident.lin" } }, { id: "cmd-face" }));
  const faceState = await faceStateP;
  const evt = await evtP;
  ok("住户人脸识别通过", faceState.payload.state.lastResult === "pass", faceState.payload.state.lastPerson);
  ok("识别后自动开锁", faceState.payload.state.locked === false);
  ok("产生安防事件", evt.payload.level === "ok", evt.payload.message);

  // 5. 先上锁，再让访客刷脸 —— 应被拒绝且门锁保持闭合
  const relockP = once("ack", (m) => m.ref === "cmd-relock");
  send(envelope("command", { deviceId: "lock.entry", action: "lock", params: {} }, { id: "cmd-relock" }));
  ok("远程上锁成功", (await relockP).payload.ok === true);

  const rejectP = once("state", (m) => m.payload?.deviceId === "lock.entry" && m.payload.state.lastResult === "reject");
  send(envelope("command", { deviceId: "lock.entry", action: "face", params: { faceId: "guest.unknown" } }, { id: "cmd-face2" }));
  const rejected = await rejectP;
  ok("访客识别被拒且保持上锁", rejected.payload.state.lastResult === "reject" && rejected.payload.state.locked === true,
    rejected.payload.state.lastPerson);

  // 6. 云台控制
  const panP = once("state", (m) => m.payload?.deviceId === "camera.living" && m.payload.state.pan === 275);
  send(envelope("command", { deviceId: "camera.living", action: "pan", params: { pan: 275 } }, { id: "cmd-pan" }));
  const panState = await panP;
  ok("云台角度可控", panState.payload.state.pan === 275);

  // 7. 视频流：信令走 WebSocket，像素走独立的 HTTP 通道
  // 先回到关流状态 —— 信令只在状态「变化」时广播，上一轮残留 streaming=true 会导致本次不广播
  const preOff = once("ack", (m) => m.ref === "cmd-cam-pre");
  send(envelope("command", { deviceId: "camera.living", action: "stream", params: { on: false } }, { id: "cmd-cam-pre" }));
  await preOff;

  const liveP = once("video", (m) => m.payload?.deviceId === "camera.living" && m.payload.live === true, 5000);
  const camOn = once("ack", (m) => m.ref === "cmd-cam");
  send(envelope("command", { deviceId: "camera.living", action: "stream", params: { on: true } }, { id: "cmd-cam" }));
  const camAck = await camOn;
  ok("stream 指令被接受", camAck.payload.ok === true, `通道 ${camAck.payload.stream?.channel}`);

  const sig = await liveP;
  ok("流信令广播 live=true", sig.payload.channel === "233666" && sig.payload.streaming === true,
    `viewers=${sig.payload.viewers} fps=${sig.payload.fps}`);

  const frame = await pullFrame("/?stream=233666");
  ok("MJPEG 流通道可用", frame.contentType.includes("multipart/x-mixed-replace"),
    frame.contentType.split(";")[0]);
  ok("流里能取到 JPEG 帧", frame.bytes > 1000, `${frame.bytes} 字节`);

  const listed = await getJson("/streams");
  const ch = (listed?.streams || []).find((s) => s.channel === "233666");
  ok("/streams 反映推流状态", ch?.live === true, `viewers=${ch?.viewers}`);

  const camOff = once("ack", (m) => m.ref === "cmd-cam-off");
  send(envelope("command", { deviceId: "camera.living", action: "stream", params: { on: false } }, { id: "cmd-cam-off" }));
  ok("stream off 生效", (await camOff).payload.stream?.live === false);

  // 8. 错误处理
  const badDevice = once("ack", (m) => m.ref === "cmd-bad");
  send(envelope("command", { deviceId: "no.such.device", action: "set", params: {} }, { id: "cmd-bad" }));
  ok("未知设备被拒绝", (await badDevice).payload.ok === false);

  const badType = once("error", (m) => m.payload?.code === "UNKNOWN_TYPE");
  send(envelope("teleport", {}, { id: "cmd-unknown" }));
  ok("未知消息类型返回 error", (await badType).payload.code === "UNKNOWN_TYPE");

  ws.send("这不是 JSON");
  const badJson = await once("error", (m) => m.payload?.code === "BAD_JSON");
  ok("非法 JSON 被拦截", badJson.payload.code === "BAD_JSON");

  // 9. 心跳
  const pongP = once("pong");
  send(envelope("ping", { at: 1 }, { id: "cmd-ping" }));
  ok("ping/pong 正常", (await pongP).payload.echo.at === 1);

  // 10. 传感器只读
  const ro = once("ack", (m) => m.ref === "cmd-ro");
  send(envelope("command", { deviceId: "sensor.living", action: "set", params: { temperature: 99 } }, { id: "cmd-ro" }));
  ok("传感器拒写（只读遥测）", (await ro).payload.ok === false);

  // 11. 初始化设置：口令错误应拒绝，正确口令写入位置并改写室外温度
  const badSetup = once("ack", (m) => m.ref === "cmd-setup-bad");
  send(envelope("setup", { password: "wrong", lat: 30.27, lon: 120.15, name: "测试" }, { id: "cmd-setup-bad" }));
  ok("错误口令拒绝初始化", (await badSetup).payload.ok === false);

  const setupAck = once("ack", (m) => m.ref === "cmd-setup", 12000);
  const setupPush = once("setup", () => true, 12000);
  send(envelope("setup", { password: "villa", lat: 30.2741, lon: 120.1551, name: "杭州西湖" }, { id: "cmd-setup" }));
  const [setupDone, setupMsg] = await Promise.all([setupAck, setupPush]);
  ok("正确口令写入经纬度", setupDone.payload.ok === true && setupMsg.payload.site.lat === 30.3,
    `${setupMsg.payload.site.name} ${setupMsg.payload.outdoor.temp}°C`);
  ok("室外温度来自预报而非默认值", setupMsg.payload.outdoor.source === "forecast"
    && Number.isFinite(setupMsg.payload.outdoor.temp));

  // 收尾：恢复离家模式
  send(envelope("scene", { sceneId: "away" }, { id: "cmd-reset" }));
  await new Promise((r) => setTimeout(r, 300));

  const failed = results.filter((r) => !r.pass);
  console.log(`\n共 ${results.length} 项，通过 ${results.length - failed.length} 项，失败 ${failed.length} 项\n`);
  ws.close();
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => {
  console.error("\n自检中断：", e.message);
  process.exit(1);
});
