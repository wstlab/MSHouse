# 客户端接入指南

> 面向**要写自己客户端**的读者：学生、二次开发者、想把别墅接进自己系统的人。
>
> 读这份文档**不需要**看三维场景代码。数字孪生的「协议面」和「呈现面」是分开的：
> 你只需要会连 WebSocket、会读写 JSON，就能拿到全部传感器数据、控制全部设备。
>
> 配套代码：`public/demo.html` —— 一个单文件、零依赖的完整客户端，本文所有片段都出自它。

---

## 一、五分钟跑通

**第 1 步：启动网关**（如果你还没启动）

```bash
cd mshouse-py
pip install -r requirements.txt
python -m server.main     # 默认 http://localhost:8080
```

启动日志会打印**访问令牌**，只打印一次，先抄下来：

```
==============================================================
MSHouse 镜像家居 · 网关已启动
==============================================================
访问令牌：3f9c1a...
初始化设置口令（本次启动生成，仅显示一次）：7Qm4-Xr2Bk9LdPw
==============================================================
```

> 后端是 Python（FastAPI + asyncio），前端是 Three.js —— 两层通过标准协议对话，
> 所以本指南里的客户端代码与后端语言无关。
>
> **`/ws` 需要鉴权**：连不上、报 `4401` 基本都是没带令牌。
> 三种携带方式见 3.1。

**第 2 步：打开 demo 客户端**

浏览器访问 `http://localhost:8080/demo.html`。页面**默认不连接**——在右上角填入服务器地址与
**访问令牌**，点「连接」才开始建链。断开后不会自动重连，再点一次即重新连上。

> 也可以用 URL 参数一次性带上：`http://localhost:8080/demo.html?token=<令牌>`
> （会被记到 `localStorage`，下次自动填回）。

**第 3 步：认识界面**

连上后顶部状态灯变绿，左侧设备区分成两个标签：

| 标签 | 设备 | 作用 |
|---|---|---|
| **感知** | 4 台温湿度 + 客厅摄像头 + 大门人脸锁 | 勾选某台设备，它的报文（读数 / 流信令 / 识别记录）才会出现在右侧报文面板；**默认全部未勾选**，未勾选的会被过滤 |
| **执行** | 4 盏灯 + 2 台空调 + 摄像头 + 人脸锁 | 每台卡片带一份**报文样例**，展开后可「复制」或「填入执行框」 |

摄像头与人脸锁同时出现在两个标签里——它们既推流（感知）也接受命令（执行）。

**第 4 步：完整走一遍链路**

1. 到「感知」标签勾选**客厅温湿度** → 右侧报文面板开始出现 `state` 报文
   （注意：稳态下可能**几十秒才来一条**，这是变化上报，不是掉线，见第四节）
2. 切到「执行」标签，展开**客厅主灯**的「报文样例」→ 点「填入执行框」
3. 到右侧「发送报文」卡片点**发送** → 报文面板出现一条 `↑ 发出`，紧跟着 `ack`（受理）
   和 `state`（执行完成）；卡片上的状态行同步变成「开 · 亮度 80%」
4. 也可以直接在执行框里手写报文，`v` / `id` / `ts` 缺失会自动补齐：

```json
{ "type": "command", "payload": { "deviceId": "light.living", "action": "set", "params": { "power": true } } }
```

执行框不限于 `command`，`scene` / `snapshot` / `resync` / `ota` / `ping` / `setup` 都能发。

**换服务器地址**：直接改右上角输入框，或在 URL 上加参数：

```
http://localhost:8080/demo.html?server=192.168.1.20:8080&token=<令牌>
```

地址与令牌会记在 `localStorage`，下次打开自动填回。地址接受 `http://host:port`、`host:port`、`ws://...` 三种写法。

---

## 二、最小可运行客户端（20 行）

`demo.html` 里的 `TwinClient` 类是完整实现。如果你只想在浏览器控制台里试试，这些就够了：

```js
// 令牌是唯一必需的「额外一步」：浏览器只能通过子协议携带它
const token = localStorage.getItem("mshouse.token") || prompt("访问令牌（见网关启动日志）");
const ws = new WebSocket("ws://localhost:8080/ws", ["mshouse.v1", `bearer.${token}`]);

ws.onopen = () => {
  // 控制状态：打开客厅灯，亮度 60%
  ws.send(JSON.stringify({
    v: "1.0.0", type: "command", id: "c-1", ts: Date.now(),
    payload: { deviceId: "light.living", action: "set", params: { power: true, brightness: 60 } },
  }));
};

const shadow = new Map();   // 本地设备影子

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  // 服务端心跳必须回，否则 3 次未回应会被以 4408 断开
  if (msg.type === "ping") {
    ws.send(JSON.stringify({ v: "1.0.0", type: "pong", ts: Date.now(), payload: {} }));
    return;
  }
  // 认证后服务端主动推 hello + snapshot（全量影子），之后是增量 state
  if (msg.type === "snapshot") {
    for (const d of msg.payload.devices) {
      shadow.set(d.id, { ...d.state });
      if (d.type === "sensor") console.log(d.name, d.state.temperature, d.state.humidity);
    }
  }
  if (msg.type === "state") {
    const p = msg.payload;
    // delta / confirmed 是增量，必须合并；只有 full 才能直接替换
    shadow.set(p.deviceId, p.stateKind === "full"
      ? { ...p.state }
      : { ...(shadow.get(p.deviceId) || {}), ...p.state });
    if (p.deviceId === "light.living") console.log("客厅灯现在是：", shadow.get(p.deviceId));
  }
};
```

**Python 客户端**同样简单（`scripts/selfcheck.py` 是完整范例，可以直接当范例读）：

```python
import asyncio
import json

import websockets


async def main():
    token = "从网关启动日志里抄来的令牌"
    async with websockets.connect(
        "ws://localhost:8080/ws",
        subprotocols=["mshouse.v1", f"bearer.{token}"],   # 令牌搭子协议的车
    ) as ws:
        await ws.send(json.dumps({
            "v": "1.0.0", "type": "command", "id": "c-1",
            "payload": {"deviceId": "light.living", "action": "set",
                        "params": {"power": True, "brightness": 60}},
        }))
        shadow = {}
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "ping":
                await ws.send(json.dumps({"v": "1.0.0", "type": "pong", "payload": {}}))
            elif msg["type"] == "snapshot":
                for d in msg["payload"]["devices"]:
                    shadow[d["id"]] = dict(d["state"])
                    if d["type"] == "sensor":
                        print(d["name"], d["state"]["temperature"], d["state"]["humidity"])
            elif msg["type"] == "state":
                p = msg["payload"]
                cur = {} if p["stateKind"] == "full" else shadow.get(p["deviceId"], {})
                shadow[p["deviceId"]] = {**cur, **p["state"]}
                if p["deviceId"] == "light.living":
                    print("客厅灯现在是：", shadow[p["deviceId"]])


asyncio.run(main())
```

---

## 三、连接与握手

| 项 | 值 |
|---|---|
| 端点 | `ws://<host>:<port>/ws`（HTTPS 页面下用 `wss://`） |
| 子协议 | `mshouse.v1`（服务端会回显） |
| 鉴权 | **必需**，通过子协议头 / 查询串 / 首帧 hello 三选一携带令牌 |
| 协议版本 | `1.0.0`（`v` 的主版本不一致会被拒绝） |
| 编码 | UTF-8 JSON 文本帧 |

### 3.1 令牌怎么带

浏览器的 WebSocket 构造函数没法自定义请求头，所以令牌有三个入口，
任一条通过即可 —— 三种都支持是因为不同客户端的能力边界不同：

**① 子协议头（浏览器推荐）**

```js
new WebSocket("ws://host:8080/ws", ["mshouse.v1", `bearer.${token}`]);
```

**② 查询串（命令行与脚本最顺手）**

```bash
websocat "ws://127.0.0.1:8080/ws?token=<令牌>"
```

**③ 首帧 hello（某些 SDK 只能连上之后才发得出数据）**

```json
{ "v": "1.0.0", "type": "hello", "payload": { "token": "<令牌>" } }
```

> **令牌从哪来**：网关启动日志会打印。首次启动自动生成，抄进 `config.toml` 的
> `security.token` 即可固定下来；也可以用环境变量 `MSHOUSE_TOKEN` 覆盖。
>
> 未认证的连接只能发 `hello` 与 `ping`，**5 秒**内未完成认证会被以
> close code `4401` 关闭 —— 没有这条，未认证连接可以无限占位。

### 3.2 建链后收到什么

认证通过后，服务端依次推三条：

1. `hello` — 网关标识、协议版本、是否需要认证
2. `snapshot` — **全量**设备影子（13 台设备 + 场景定义 + 最近事件 + 站点/室外信息）
3. `event` — 一条 info 级接入事件

未认证时只收到 `hello`，且 `payload.authed` 为 `false`。
设备影子包含「家里有没有人」「门锁着没有」这类信息，不该让一个还没出示
令牌的连接读到 —— 这也是把全量下发挪到认证之后的原因。

### 3.3 心跳与断线判定

服务端每 **20 秒**下发一条 `ping`，客户端必须回 `pong`：

```js
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === "ping") {
    ws.send(JSON.stringify({ v: "1.0.0", type: "pong", payload: {} }));
  }
};
```

连续 **3 次**没有回应 → 服务端判定失联，以 close code `4408` 断开，
并释放该连接占用的视频流通道。

### 3.4 出站序号与重新同步

每条 `state` 报文都带一个按连接递增的 `seq`。客户端发现跳号说明中间有报文
丢失（服务端在背压时会合并或丢弃），此时请求全量对齐：

```json
{ "v": "1.0.0", "type": "resync", "payload": { "since": 128 } }
```

服务端会回一份完整 `snapshot`，并带上 `currentSeq`。

**为什么不做增量重放**：重放需要服务端保存全部历史报文，历史越长代价越大；
一次全量快照反而更干脆。真实 IoT 平台普遍是这个取舍。

### 3.5 协议版本协商

`v` 的主版本与网关不一致时，服务端返回 `PROTOCOL_MISMATCH` 并以
close code `4426` 断开。放行会让新旧客户端各读一半字段，
表现为「界面上某些值永远不动」—— 比直接报错难查得多。

**关键约定**：所有消息都是同一个信封 `{ v, type, id?, ts, payload }`。
`v` 是协议版本，`id` 用于把回执关联回请求。

---

## 四、读传感器

传感器是**只读遥测**设备，你只能读，不能写（写会被拒，见第七节）。

### 4.1 什么时候会收到数据

传感器不再是「每 2 秒无条件推一次」，而是按真实无线设备的
**变化上报**规则来：只有满足下面任一条件才会产生报文。

| 参数 | 含义 | 温湿度默认值 |
|---|---|---|
| `reportableChange` | 变化量达到多少才上报 | 温度 0.2 °C / 湿度 1 % |
| `minInterval` | 两次上报的最小间隔（秒） | 10 |
| `maxInterval` | 最长静默多久必须保活上报一次（秒） | 600 |

这三个参数对应真实 Zigbee 设备的 `ReportableChange` /
`MinReportingInterval` / `MaxReportingInterval`，可以在 `server/model.py`
的 `REPORTING` 表里调整（改完跑一次 `scripts/export_model.py`，
前端与 HA 桥会同步拿到新值）。

**所以：稳态下可能几十秒都不来一条报文，这是正常的，不是掉线。**
旧版恒定 2 条/秒的推送看着「热闹」，但其中近三成是重复值 ——
真实设备不会那样工作。

### 4.2 报文长什么样

```json
{
  "v": "1.0.0",
  "type": "state",
  "ts": 1796000000000,
  "payload": {
    "deviceId": "sensor.living",
    "type": "sensor",
    "room": "living",
    "state": { "temperature": 25.6, "humidity": 47 },
    "derived": {
      "thi": 23.4,
      "comfort": "comfortable",
      "algorithm": "thi-v1",
      "computedAt": 1796000000000
    },
    "stateKind": "delta",
    "online": true,
    "updatedAt": 1796000000000,
    "seq": 128
  }
}
```

`stateKind` 有三种取值，客户端必须区别对待：

| 取值 | 含义 | 客户端该怎么做 |
|---|---|---|
| `delta` | 只含**本次变化**的字段 | 与本地状态**合并** |
| `confirmed` | 命令执行完成后的确认上报 | 与本地状态**合并** |
| `full` | 完整快照（重连、恢复在线、OTA 后） | 直接**替换**本地状态 |

> ⚠️ 只有 `full` 是完整状态。把 `delta` 当全量用，
> 会把这次没变化的字段全部抹成 `undefined`。

### 4.3 字段表

**`state`** —— 设备原生上报的物理量：

| 字段 | 含义 | 说明 |
|---|---|---|
| `temperature` | 温度 °C | 受同房间空调、厨房灶台、卫生间热水影响；已量化到 0.1 °C 分辨率 |
| `humidity` | 相对湿度 % | 受室外湿度与洗浴水汽影响；已量化到 1 % 分辨率 |
| `battery` | 电量 % | 按每天约 0.1 % 缓慢下降，低于 15 % 触发 `LOW_BATTERY` 故障 |
| `rssi` | 无线信号强度 dBm | −30（极好）～ −95（极差），会小幅波动 |

**`derived`** —— 网关算出来的派生量，**不是设备测的**：

| 字段 | 含义 | 说明 |
|---|---|---|
| `thi` | 温湿指数 | `T − 0.55 × (1 − RH/100) × (T − 14.5)` |
| `comfort` | 舒适度枚举 | `cool` 偏凉 / `comfortable` 舒适 / `warm` 偏热 / `hot` 闷热 |
| `algorithm` | 算法版本 | 当前为 `thi-v1`；换算法会变，便于区分历史数据 |
| `computedAt` | 计算时间戳 | |

> **为什么 comfort 不在 `state` 里**：真实温湿度传感器（Zigbee `0x0402`、
> Matter `TemperatureMeasurement`）只上报物理量，不会在自己的属性里塞一个
> 语义标签。「舒适」是应用层算出来的。而且旧版的判据 `23~27 °C 且 40~60 %RH`
> 没有标准出处，用 `RH > 70 → 偏闷` 更错 —— 30 °C/75 % 和 18 °C/75 %
> 会被压成同一个结论，但这两者体感差别很大。
>
> 中文映射放在前端（`public/js/ui.js` 的 `COMFORT_LABEL`），
> 服务端只发机器可读枚举，这样换语言不用动协议。

**室外环境**（不在设备列表里，是网关级数据）：

```js
// 来自 snapshot.payload.outdoor 或 setup 消息
{ temp: 24.7, humidity: 91, source: "forecast", place: "杭州西湖" }
```

`source` 为 `forecast` 时表示已按初始化设置的地理位置取过真实预报；为 `default` 时是网关的默认值。

### 4.4 两种读法

**① 被动接收（推荐）**

```js
const shadow = new Map();   // 客户端自己的设备影子

ws.onmessage = (e) => {
  const { type, payload } = JSON.parse(e.data);
  if (type !== "state" || payload.type !== "sensor") return;
  const cur = shadow.get(payload.deviceId) || {};
  // delta / confirmed 是增量，必须合并；只有 full 才能直接替换
  shadow.set(payload.deviceId, payload.stateKind === "full"
    ? { ...payload.state }
    : { ...cur, ...payload.state });
  const s = shadow.get(payload.deviceId);
  console.log(payload.deviceId, s.temperature, s.humidity, payload.derived?.comfort);
};
```

**② 主动拉全量** —— 想立刻对账时（这也是 `resync` 背后的机制）：

```js
ws.send(JSON.stringify({ v: "1.0.0", type: "snapshot", id: "sync-1", ts: Date.now(), payload: {} }));
```

---

## 五、控制设备

### 5.1 两阶段：接受 ≠ 执行

一条命令走**两阶段**。这不是为了多绕一步，而是因为
「命令已受理」和「设备已执行」在真实系统里是两件不同的事 ——
中间隔着继电器吸合、锁舌弹出、压缩机启动这些物理耗时，
还隔着「设备压根没响应」这种故障。

**第 ① 阶段：网关受理（立即回 `ack`）**

```json
{
  "v": "1.0.0",
  "type": "command",
  "id": "c-8",
  "ts": 1796000000000,
  "payload": {
    "deviceId": "light.living",
    "action": "set",
    "params": { "power": true, "brightness": 60 }
  }
}
```

```json
{ "v": "1.0.0", "type": "ack", "id": "srv-9", "ref": "c-8",
  "payload": { "ok": true, "status": "ACCEPTED", "deviceId": "light.living",
               "action": "set", "desired": { "power": true, "brightness": 60 },
               "delta": { "power": true, "brightness": 60 },
               "stream": null } }
```

`status: "ACCEPTED"` 只表示**网关收下了这条命令**，`desired` 是写进去的期望态。
此刻设备还没动，`reported` 也没变。

**第 ② 阶段：设备执行（延迟后广播 `state`）**

经过 `execLatencyMs`（灯 120ms、锁 600ms、空调 900ms…见 `server/model.py`
的 `DEVICE_BEHAVIOR`）后，设备真正动作，网关广播：

```json
{ "v": "1.0.0", "type": "state", "payload": {
    "deviceId": "light.living", "type": "light", "room": "living",
    "state": { "power": true, "brightness": 60 },
    "stateKind": "confirmed", "ref": "c-8",
    "online": true, "updatedAt": 1796000000120, "seq": 129 } }
```

| 阶段 | 报文 | 含义 |
|---|---|---|
| ① 受理 | `ack.payload.status === "ACCEPTED"` | 参数合法、已写入期望态 |
| ② 执行 | `state.stateKind === "confirmed"` | 设备真的动了，`ref` 指回命令 `id` |

> **为什么值得这么做**：只回一个 `ok:true` 的话，界面上灯「立刻」就亮了 ——
> 但真实世界里它要过几十毫秒，而且可能失败。两阶段让你能画出
> 「发送中 → 已生效」的中间态，也能在超时后如实告诉用户「设备没响应」。
> 这套 `reported` / `desired` 的分离就是 AWS IoT 的 Device Shadow、
> Matter 的 `desired` 属性所做的事。

### 5.2 抖动合并（拖动滑杆只执行最终值）

连续拖动亮度滑块会瞬间产生几十条命令。真实设备只会执行**最终值**，
不会把 30 个中间值挨个打一遍。网关对同一设备的待执行命令做合并：
新命令取消上一条未执行的定时器并重新计时，于是只有最后一次生效。

```js
// 连发 8 条，最终只会产生 1 条 confirmed 广播，brightness = 55
for (const b of [10, 20, 30, 40, 45, 50, 52, 55]) {
  cmd("light.living", "set", { power: true, brightness: b });
}
```

合并窗口由 `devices.coalesce_window_ms` 控制（默认 200ms，见 config.toml）。
**注意**：每条命令仍会收到各自的 `ack`（受理成功），但只有一条
`state`（执行完成）—— 这正是两阶段分离带来的好处。

### 5.3 各类型设备的可用动作

| 设备类型 | action | params | 说明 |
|---|---|---|---|
| **light** | `on` / `off` / `toggle` | `{ brightness? }` | 开关；`on` 可带亮度 |
| | `set` | `{ power, brightness, colorTemp }` | 任意组合。`brightness` 0–100，`colorTemp` 2700–6500K |
| **ac** | `on` / `off` / `toggle` | — | 开关 |
| | `set` | `{ power, mode, targetTemp, fan }` | `mode`: `cool`/`heat`/`fan`/`dry`/`auto`；`targetTemp` 16–30；`fan`: `low`/`mid`/`high`/`auto` |
| **camera** | `set` | `{ power, streaming, armed }` | 开关与布防 |
| | `pan` | `{ pan }` | 绝对角度，0–359，自动取模；**分 4 步渐进转动**，不是瞬移 |
| | `nudge` | `{ delta }` | 相对转动，如 `{ delta: 15 }` / `{ delta: -15 }` |
| | `arm` | `{ armed }` | 布防开关 |
| | `stream` | `{ on }` | 推流开关 |
| | `toggle` | — | 电源翻转 |
| **lock** | `lock` / `unlock` | `{ person? }` | 上锁 / 开锁 |
| | `stream` | `{ on }` | 推流开关 |
| | `enrollFace` / `dismissEnroll` | `{ token, name? }` | 回应陌生人脸登记询问；**立即执行**，不走设备延迟 |
| **sensor** | — | — | **只读**，任何写命令返回 `READ_ONLY_DEVICE` |

> 动作是否可用还受**能力位**约束：不是每台设备都支持全部字段。
> 给不支持的字段写值会返回 `UNSUPPORTED_CAPABILITY`（而不是含糊的 `INVALID_PARAM`）。
> 能力表见 `server/model.py` 的 `FIELD_CAPABILITY`。

人脸识别由画面源实时驱动：识别到已登记人脸自动开锁并广播 `state`（`lastResult:"pass"` + `lastPerson`）；识别到未登记人脸时广播 `faceEnroll` 询问，面板可用 `enrollFace` / `dismissEnroll` 回应。

识别用的是 OpenCV Zoo 的 **YuNet（检测）+ SFace INT8 块量化（识别）**，
比对的是 128 维 embedding 的**余弦距离**：`distance = 1 − cos`，`distance ≤ threshold`（默认 `0.637`）
即判定为本人。阈值与模型说明见 `config.toml` 的 `[streams.lock_entry.face]`，
运行时状态见 `GET /health` → `lock.face`。

**常用代码片段**：

```js
const cmd = (deviceId, action, params = {}) => ws.send(JSON.stringify({
  v: "1.0.0", type: "command", id: `c-${Date.now()}`, ts: Date.now(),
  payload: { deviceId, action, params },
}));

cmd("light.bedroom", "set", { power: true, brightness: 8, colorTemp: 2700 });  // 卧室夜灯
cmd("ac.living", "set", { power: true, mode: "cool", targetTemp: 25 });       // 客厅制冷 25°C
cmd("camera.living", "pan", { pan: 180 });                                    // 云台转 180°
cmd("camera.living", "nudge", { delta: -15 });                                // 向左微调 15°
cmd("lock.entry", "unlock");                                                  // 远程开锁
cmd("lock.entry", "lock");                                                    // 远程上锁
cmd("curtain.bedroom", "set", { position: 60 });                              // 窗帘开度 60%
cmd("curtain.bedroom", "open");                                               // 窗帘全开（position 100）
cmd("curtain.bedroom", "close");                                              // 窗帘全闭（position 0）
```

**离线设备不会假装成功**：

```json
{ "type": "ack", "ref": "c-9", "payload": { "ok": false,
  "errorInfo": { "code": "DEVICE_OFFLINE", "message": "卧室空调 当前离线，命令未下发", "details": [] },
  "error": "卧室空调 当前离线，命令未下发",
  "desired": {} } }
```

> 旧版对离线设备也回 `ok:true`，调用方会以为灯已经开了 —— 这是最容易被
> 真实设备打脸的一处。网关在命令下发前先看设备是否在线。

### 5.4 命令限流

每条连接有一个令牌桶（默认 `devices.command_rate_limit` = 10 条/秒，见 config.toml）。
超限时命令被拒，**但连接不会被断开**：

```json
{ "type": "ack", "payload": { "ok": false,
  "errorInfo": { "code": "RATE_LIMITED", "message": "命令过于频繁（上限 10 条/秒），请稍后重试" } } }
```

真实网关一定会限速 —— 否则一个写错的 `for` 循环就能把设备刷到不停开关。

### 5.5 场景模式

场景是**服务端的批量操作**，客户端只发一个 id，网关负责改一堆设备并广播结果：

```js
ws.send(JSON.stringify({ v: "1.0.0", type: "scene", id: "s-1", ts: Date.now(),
                         payload: { sceneId: "away" } }));
```

| sceneId | 名称 | 效果 |
|---|---|---|
| `home` | 回家模式 | 客厅/厨房灯亮，客厅空调制冷 25°C，摄像头撤防，门锁开 |
| `away` | 离家模式 | 全屋灯与空调关闭，摄像头布防并归零，门锁上锁 |
| `sleep` | 睡眠模式 | 只留卧室 8% 夜灯，卧室空调 26°C 低风，摄像头布防转 180° |
| `movie` | 观影模式 | 客厅调暗至 12%，厨房关闭，空调 24°C 低风 |

成功后收到 `scene`（通知）+ `event`（日志）。

> **场景不推 `snapshot`**：场景只改一部分设备，网关会为**真正变化**的设备
> 各推一条 `state`（`stateKind: "confirmed"`，`payload` 里带 `changed` 说明改了哪几台）。
> 想知道全量请自己发 `snapshot` —— 见第四节 4.4 的第二种读法。
> 旧版每次切场景都推一份全量影子，在设备多的时候是很重的冗余。

### 5.6 固件升级（OTA）

升级不是「一个请求就完事」，而是一条**状态机**：下载 → 校验 → 安装 → 重启 → 生效，
每一步都可能失败、都可能超时。这里实现同样的状态机（教学场景下进度是模拟的）：

```js
ws.send(JSON.stringify({ v: "1.0.0", type: "ota", id: "o-1", ts: Date.now(),
                         payload: { target: "sensor.living", version: "2.1.3" } }));
```

受理后依次广播 `ota` 消息，最后落一条全量 `state`：

```json
{ "type": "ota", "payload": { "deviceId": "sensor.living", "version": "2.1.3",
                              "state": "downloading", "progress": 0.55 } }
{ "type": "ota", "payload": { "deviceId": "sensor.living", "version": "2.1.3",
                              "state": "success", "progress": 1.0 } }
```

`state` ∈ `downloading` / `verifying` / `applying` / `rebooting` / `success` / `failed`。

> **为什么值得做**：没有 OTA 能力的设备出厂后就没法修 bug ——
> 这在真实产品里是硬需求，也是判断「是不是真设备」的一条标准。

---

## 六、消息类型总表

| 方向 | type | payload 要点 |
|---|---|---|
| S→C | `hello` | `{ role, name, protocol, authed, needAuth }` |
| S→C | `snapshot` | 全量影子：`devices` / `scenes` / `scene` / `occupancy` / `outdoor` / `site` / `recentEvents` / `currentSeq` |
| S→C | `state` | `{ deviceId, type, room, online, state, stateKind, seq, derived? }`，单设备（增量或全量） |
| S→C | `event` | `{ id, ts, level, source, message }`，`level` ∈ `info`/`ok`/`warn`/`alarm`/`error` |
| S→C | `scene` | `{ sceneId, name, occupancy, source }` |
| S→C | `video` | 流信令：通道 / 推流状态 / 云台 / 观众数 / 帧率，见第八节 |
| S→C | `setup` | `{ site, outdoor }`，位置被改写后广播 |
| S→C | `setupAuth` | `{ needPassword }`，初始化设置是否需要口令 |
| S→C | `faceEnroll` | `{ token, snapshot? }`，陌生人脸登记询问 |
| S→C | `fault` | `{ deviceId, faultCode, severity, message, since }`，见 6.2 |
| S→C | `ota` | `{ deviceId, version, state, progress }`，见 5.6 |
| S→C | `ack` | `{ ok, status?, deviceId?, action?, desired?, delta?, errorInfo?, ... }`，`ref` 关联请求 `id` |
| S→C | `error` | `{ code, message, details }`，见第七节 |
| S→C | `ping` | `{}`，服务端每 20s 下发，客户端须回 `pong` |
| S→C | `pong` | `{ echo }` |
| C→S | `command` | 设备控制 |
| C→S | `scene` | 场景切换 |
| C→S | `snapshot` | 主动拉全量 |
| C→S | `resync` | `{ since }` 序号对齐请求，回全量快照 |
| C→S | `ota` | `{ target, version }` 固件升级 |
| C→S | `setup` | 初始化定位（需口令），见第九节 |
| C→S | `hello` | 首帧补交令牌（可选） |
| C→S | `ping` / `pong` | 心跳与心跳回应 |

**服务端主动心跳**：网关每 **20 秒**发一条 `ping`，客户端回 `pong`（见 3.3）。
客户端也可以自己发 `ping` 测往返延迟：

```js
const t0 = performance.now();
ws.send(JSON.stringify({ v: "1.0.0", type: "ping", id: "p-1", ts: Date.now(), payload: { at: t0 } }));
// 收到 pong 后：console.log("RTT", performance.now() - msg.payload.echo.at, "ms");
```

### 6.1 每条 `state` 都带 `seq`

`seq` 是**按连接**递增的出站序号。客户端记录最大值，发现跳号即知中间丢过报文，
发 `resync` 对齐（见 3.4）。

### 6.2 故障上报 `fault`

设备故障走**事件通道主动推**，不是等平台来轮询 —— 轮询查不出「刚才闪过一次故障」。

```json
{ "v": "1.0.0", "type": "fault", "ts": 1796000000000, "payload": {
    "deviceId": "light.bath", "faultCode": "LAMP_BURNOUT",
    "severity": "error", "message": "灯珠损坏，无法点亮", "since": 1796000000000 } }
```

故障码见 `server/model.py` 的 `FAULT_CODES`（灯珠损坏、驱动过温、滤网脏堵、
压缩机故障、电量低、锁舌卡阻、镜头遮挡、信号弱）。

**教学场景下怎么触发**：`POST /api/simulate?token=<令牌>`（需令牌，见第十节）：

```bash
curl -X POST "http://localhost:8080/api/simulate?token=$MSHOUSE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "fault", "deviceId": "light.bath", "faultCode": "LAMP_BURNOUT"}'
```

支持 `action`：`offline`（设备离线）/ `online`（恢复）/ `fault`（上报故障）/ `clearFault`（清除故障）。
`fault` 与 `clearFault` 需带 `faultCode`；`clearFault` 不带则清除该设备全部故障。

---

## 七、错误处理

错误信息是**结构化**的：`{ code, message, details }`。客户端按 `code` 分支，
不要去匹配中文文案（文案随时可能改）。

**两类错误，处理方式不同**：

**① 协议级错误 → `error` 消息**（你的报文本身有问题）

| code | 触发条件 | 后续 |
|---|---|---|
| `BAD_JSON` | 发过去的不是合法 JSON | 连接保留 |
| `BAD_ENVELOPE` | 缺 `type` 字段 | 连接保留 |
| `UNKNOWN_TYPE` | `type` 不认识 | 连接保留 |
| `UNAUTHORIZED` | 未认证就发业务消息 | 以 `4401` 关闭 |
| `PROTOCOL_MISMATCH` | `v` 主版本不一致 | 以 `4426` 关闭 |
| `INTERNAL` | 服务端内部异常 | 连接保留 |

```json
{ "v": "1.0.0", "type": "error", "id": "c-8", "ts": 1796000000000,
  "payload": { "code": "BAD_JSON", "message": "报文不是合法 JSON", "details": [] } }
```

```js
if (msg.type === "error") {
  console.error(`协议错误 ${msg.payload.code}：${msg.payload.message}`);
}
```

> **重要修正**：旧版遇到服务端异常会**静默吞掉并断开连接**，客户端只看到
> 「莫名其妙掉线」。单条报文出错不再断连，且必定回一条带 `code` 的
> `error` —— 只有鉴权与版本这两类**连接级**问题才关连接。

**② 业务级失败 → `ack.payload.ok === false`**（报文合法，但设备不买账）

```json
{ "type": "ack", "ref": "c-8", "payload": { "ok": false,
  "errorInfo": { "code": "UNSUPPORTED_CAPABILITY",
                 "message": "客厅主灯 拒绝了 1 个字段",
                 "details": [ { "field": "brightness", "reason": "该设备不支持此字段" } ] },
  "error": "客厅主灯 拒绝了 1 个字段" } }
```

```js
if (msg.type === "ack" && msg.payload.ok === false) {
  const { code, message, details } = msg.payload.errorInfo;
  console.warn(`命令被拒绝 [${code}]：${message}`, details);
}
```

> **过渡期双写**：`errorInfo`（结构化对象，新客户端用）与 `error`（纯字符串，
> 老客户端直接显示）同时下发，到下一个主版本移除 `error`。

完整错误码表（`server/model.py` 的 `ERROR_CODES`）：

| code | 含义 | 客户端建议动作 |
|---|---|---|
| `DEVICE_NOT_FOUND` | `deviceId` 拼错 | 检查设备 ID |
| `ACTION_NOT_SUPPORTED` | 动作与设备类型不匹配 | 查 5.3 动作表 |
| `UNSUPPORTED_CAPABILITY` | 设备不支持该字段 | 查能力位，别写 |
| `INVALID_PARAM` | 参数越界 / 类型错 | 按 `details` 修正 |
| `READ_ONLY_DEVICE` | 试图写传感器 | 改走遥测，不要发命令 |
| `DEVICE_OFFLINE` | 设备当前离线 | 提示用户，稍后重试 |
| `DEVICE_TIMEOUT` | 命令超时未确认 | 提示用户，可重试 |
| `RATE_LIMITED` | 命令过于频繁 | 退避后重试 |
| `SETUP_DENIED` | 初始化口令错误 | 重新输入口令 |
| `UNAUTHORIZED` / `PROTOCOL_MISMATCH` | 连接级问题 | 重连（带令牌 / 换协议版本） |

> **教学点**：传感器拒写不是 bug，是**故意**的。真实物联网里感知设备不可控，这条约束逼着客户端区分**遥测（telemetry）**和**控制（command）**两条数据流。自检脚本 `python scripts/selfcheck.py` 里有专门一项验证它。

---

## 八、视频流怎么接

**重要**：网关**不通过 WebSocket 推像素**。视频被拆成两条独立的路：

| | 走什么 | 传什么 |
|---|---|---|
| **信令** | WebSocket `/ws` 的 `video` 消息 | 通道名、是否推流、云台角度、布防、识别结果、观众数、帧率 |
| **像素** | HTTP 长连接 `GET /?stream=<通道名>` | MJPEG（`multipart/x-mixed-replace`） |

### 第一步：直接播（一行 HTML）

像素通道不需要任何 SDK。把通道地址塞进 `<img>` 就能播：

```html
<img src="http://localhost:8080/?stream=233666" alt="客厅摄像头" />
```

通道是**常在线**的：即使设备没有推流，连接也不会断，服务端推的是待机画面。所以 `<img>` 可以一直挂着，指令一到画面自动切换，**不需要刷新、不需要重连**。

### 第二步：用信令控制推流

发一条 `stream` 指令开始 / 停止推流：

```js
ws.send(JSON.stringify({
  v: "1.0.0", type: "command", id: "cmd-stream-1", ts: Date.now(),
  payload: { deviceId: "camera.living", action: "stream", params: { on: true } }
}));
```

`ack` 里会带上通道的最新状态：

```json
{ "type": "ack", "ref": "cmd-stream-1", "payload": { "ok": true, "status": "ACCEPTED",
  "stream": { "channel": "233666", "live": true, "viewers": 1, "fps": 8 } } }
```

### 通道分配

| 通道名 | 设备 | 画面内容 |
|---|---|---|
| `233666` | `camera.living` | 客厅云台视角 |
| `233667` | `lock.entry` | 门厅人脸锁视角 |

也可以直接查 `GET /streams` 拿全部通道的实时状态，不用连 WebSocket：

```bash
curl http://localhost:8080/streams
```

> **像素通道也要带令牌**：`GET /?stream=<通道>` 在开启鉴权后需要三者之一 ——
> `?token=<令牌>`、请求头 `Authorization: Bearer <令牌>`，或**签名地址**
> `?stream=<通道>&exp=<过期秒>&sig=<签名>`。
>
> 直接把长期令牌塞进 `<img src>` 会把它暴露在浏览器历史与日志里 ——
> 推荐用 `/api/stream-url` 换一个短时签名地址：

```bash
TOKEN=$(python -c "import re;print(re.search(r'token\s*=\s*\"([^\"]+)\"',open('config.toml').read()).group(1))")
curl "http://localhost:8080/api/stream-url?channel=233666&token=$TOKEN"
# → {"ok":true,"channel":"233666","url":"/?stream=233666&exp=1796000600&sig=...",
#    "expiresAt":1796000600,"ttl":300}
```

签名地址的有效期由 `security.stream_sign_ttl` 控制（默认 300 秒）。
`GET /streams`、`GET /health` 不鉴权，只暴露通道名与统计数字，不含画面。

### 第三步：`video` 信令长什么样

`video` 消息只在状态**发生变化**时广播（不是每帧都推），所以可以放心地全量打印：

```json
{ "type": "video", "payload": {
    "deviceId": "camera.living", "kind": "camera", "channel": "233666",
    "url": "/?stream=233666", "streaming": true, "live": true, "viewers": 1,
    "pan": 275, "motion": false, "armed": true,
    "fps": 8, "idleFps": 2, "frame": 1796000000123, "occupancy": "away" } }
```

```json
{ "type": "video", "payload": {
    "deviceId": "lock.entry", "kind": "lock", "channel": "233667",
    "url": "/?stream=233667", "streaming": false, "live": false, "viewers": 0,
    "locked": true, "person": "林晓", "result": "pass",
    "fps": 2, "idleFps": 2, "frame": 1796000000123 } }
```

**如果你只想显示文字状态**、不接画面，用信令就够了：

```js
if (msg.type === "video" && msg.payload.kind === "camera") {
  const p = msg.payload;
  el.textContent = `云台 ${p.pan}° · ${p.armed ? "布防" : "撤防"}`
    + (p.motion ? " · 检测到移动" : "")
    + ` · ${p.live ? "推流中" : "待机"}`;
}
```

### 接真实摄像头

**你的客户端一行都不用改。** 服务端把渲染源换成真实的 RTSP 拉流 → JPEG 转码，通道名、`video` 信令格式、`<img>` 播放方式全部不变。这就是信令与像素分离的价值——客户端只认"通道地址"，不关心像素从哪来。

### 反过来：把浏览器摄像头推给网关

如果网关本身没有摄像头（比如跑在服务器上），可以让**打开网页的那台设备**把自己的摄像头推上去。
「设置 → 摄像头」卡片里选「浏览器摄像头」，或直接发一条 `setup` 报文：

```js
ws.send(JSON.stringify({
  v: "1.0.0", type: "setup", id: "s-1", ts: Date.now(),
  payload: { password: "<初始化口令>", name: "", address: "",
             lockSource: { kind: "browser", index: 0, url: "" } }
}));
```

然后按下面的节奏把帧 POST 上去（大门锁执行延迟 1.5s，**先等 `streaming` 变成 `true` 再推帧**）：

```js
// 1. 打开推流
ws.send(JSON.stringify({
  v: "1.0.0", type: "command", id: "c-1", ts: Date.now(),
  payload: { deviceId: "lock.entry", action: "stream", params: { on: true } }
}));

// 2. 抓帧并 POST（像素走 HTTP，与 MJPEG 同一个分工）
const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
const video = Object.assign(document.createElement("video"), { srcObject: stream, muted: true });
await video.play();
const canvas = document.createElement("canvas");

setInterval(async () => {
  if (video.readyState < 2) return;
  canvas.width = 640;
  canvas.height = Math.round(640 * video.videoHeight / video.videoWidth);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.72));
  await fetch("/api/lock/frame", { method: "POST", headers: { "Content-Type": "image/jpeg" }, body: blob });
}, 500);   // 2fps，与大门锁默认推流帧率一致
```

| 接口 | 说明 |
|---|---|
| `POST /api/lock/frame` | 收一帧 JPEG/PNG，`Content-Type: image/jpeg`。成功 `{"ok":true,"frames":N,"connected":true}`；来源不是 `browser` 或推流没开时 `400` + `{"ok":false,"error":"..."}` |
| `GET /health` → `lock.source` | `{ kind:"browser", opened, frameCount, lastFrameAt, error, target }`。`opened` 的含义是「最近 3 秒收到过帧」，断流会自动变回 `false`，画面回落静态底图 |
| `GET /health` → `lock.face` | `{ ready, reason, backend, threshold, users, enrolled }`，`backend` 会告诉你实际加载的是哪个模型；`enrolled` 是带来源标签的用户清单 |

> 网关**不会**对每一帧跑识别（2fps 推帧逐帧推理纯属浪费）：
> 推上来的帧只负责「喂画面」，真正的人脸识别仍由 `_loop_lock_vision` 每秒一次统一驱动，
> 两条路径共用同一份 `_last_vision` 状态。

### 人脸登记库的读写接口

设置页「人脸锁」卡片用的就是这三个接口，第三方客户端也可以直接调：

| 接口 | 说明 |
|---|---|
| `GET /api/lock/faces` | 登记库现状：`{ ready, reason, backend, threshold, users, enrolled }`。开放读取 |
| `POST /api/lock/enroll?name=<名字>` | body 是图片（JPEG/PNG），需请求头 `X-Setup-Password`。名字留空自动命名，重名自动加序号 |
| `DELETE /api/lock/enroll?name=<名字>` | 删除该用户（连磁盘上的人脸图一起删，重启不复活），同样需 `X-Setup-Password` |

```js
// 登记一张照片
const blob = fileInput.files[0];
await fetch(`/api/lock/enroll?name=${encodeURIComponent("张三")}`, {
  method: "POST",
  headers: { "Content-Type": blob.type, "X-Setup-Password": "<初始化口令>" },
  body: blob,
});
```

> 口令放在**请求头**而不是 URL：URL 会留在浏览器历史、代理日志和 `Referer` 里。
> 读接口开放是因为内容与 `/health` 的 `lock.face` 同源，不含额外隐私。

---

## 九、初始化设置（写入物理位置）

给别墅写入真实地理位置，网关据此取当地天气预报作为室外温度初值。

### 9.1 口令从哪来

口令不能硬编码在源码里、也不能原样出现在前端资源中 —— 那样任何人打开 F12 就能看到。
所以这里是**哈希校验**：

| 项 | 说明 |
|---|---|
| 存储形式 | PBKDF2-SHA256，格式 `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>` |
| 比对方式 | 常数时间比较（`hmac.compare_digest`），防时序侧信道 |
| 首次启动 | 未配置哈希时**自动生成随机口令**，打印到服务端日志（只打印一次） |
| 生成命令 | `python -m server.hash_password --random`（或交互式输入自定义口令） |
| 明文去向 | 不落任何文件、不进前端资源、不在日志里重复打印 |

首次启动的日志长这样（口令只出现这一次）：

```
==============================================================
MSHouse 镜像家居 · 网关已启动
==============================================================
访问令牌：3f9c1a...
  请把它填进 config.toml 的 security.token 以固定下来；
  也可以用环境变量 MSHOUSE_TOKEN 覆盖。
初始化设置口令（本次启动生成，仅显示一次）：7Qm4-Xr2Bk9LdPw
  请立即保存。要换固定口令，运行：python -m server.hash_password
  然后把输出的哈希填进 config.toml 的 security.setup_password_hash。
==============================================================
```

拿到口令后，把哈希写进 `config.toml` 固定下来：

```toml
[security]
token = "3f9c1a..."                     # 也可以只用环境变量 MSHOUSE_TOKEN
setup_password_hash = "pbkdf2_sha256$200000$<salt_b64>$<hash_b64>"
```

### 9.2 发起设置

```js
ws.send(JSON.stringify({
  v: "1.0.0", type: "setup", id: "cfg-1", ts: Date.now(),
  payload: { password: "<初始化口令>", lat: 30.2741, lon: 120.1551, name: "杭州 · 西湖" },
}));
```

成功后：

1. 收到 `ack`，`payload.site` 是写入结果，`payload.outdoor` 是按预报取的室外温湿度
2. 收到 `setup` 广播（所有客户端都能感知位置变了）
3. 收到 `snapshot`（全量刷新）
4. 室内传感器与空调回风温按室外值重新起算，避免定位后室内还停在默认值

口令错误或经纬度越界 → `ack.payload.ok === false`，
`errorInfo.code === "SETUP_DENIED"`，**不改变任何状态**。

```json
{ "type": "ack", "ref": "cfg-1", "payload": { "ok": false,
  "errorInfo": { "code": "SETUP_DENIED", "message": "初始化口令不正确", "details": [] },
  "error": "初始化口令不正确" } }
```

> **为什么这条也要拦**：初始化设置会改写全屋的物理位置基准，属于
> **敏感操作**。旧版把口令印在前端里，等于没有口令。

---

## 十、用浏览器控制台调试

`demo.html` 把客户端实例挂在了 `window` 上，打开开发者工具就能直接调：

```js
client.command("light.living", "set", { power: true, brightness: 60 })
client.command("camera.living", "pan", { pan: 90 })
client.setScene("away")
client.devices.get("sensor.living").state      // 看客厅温湿度当前值
client.devices.get("sensor.living").derived    // 看派生量（THI / comfort）
client.faults.get("light.bath")                // 看某台设备的故障列表
client.seq; client.dropped                     // 已收到的最大序号 / 跳号次数
client.resync()                                 // 发现跳号时拉一次全量对齐
[...client.devices.values()].filter(d => d.type === "sensor").map(d => [d.name, d.state.temperature])
```

另外几个 HTTP 端点也可以直接用 `curl` 看：

```bash
curl http://localhost:8080/health            # 健康检查 + 连接数 + 背压/限流统计
curl http://localhost:8080/api/model         # 当前全量影子（与 snapshot 同结构）
curl http://localhost:8080/api/diagnostics   # 诊断：故障、限流、背压、生效配置
curl http://localhost:8080/streams           # 全部视频通道的实时状态
```

`/api/diagnostics` 的输出很适合排查「为什么不推了」：

```json
{ "ok": true, "version": "1.0.0",
  "stats": { "clients": 1, "authedClients": 1, "outboxDropped": 0, "outboxCoalesced": 12,
             "commandsTotal": 34, "rateLimited": 0 },
  "faults": { "light.bath": [{ "code": "LAMP_BURNOUT", "severity": "error" }] },
  "config": { "commandRateLimit": 20, "coalesceWindowMs": 150, "allowAnonymous": false } }
```

---

## 十一、设备 ID 速查

| ID | 名称 | 房间 | 类型 |
|---|---|---|---|
| `light.living` | 客厅主灯 | 客厅 | light |
| `light.kitchen` | 厨房灯 | 厨房 | light |
| `light.bedroom` | 卧室灯 | 主卧 | light |
| `light.bath` | 卫生间灯 | 卫生间 | light |
| `sensor.living` | 客厅温湿度 | 客厅 | sensor |
| `sensor.kitchen` | 厨房温湿度 | 厨房 | sensor |
| `sensor.bedroom` | 卧室温湿度 | 主卧 | sensor |
| `sensor.bath` | 卫生间温湿度 | 卫生间 | sensor |
| `ac.living` | 客厅空调 | 客厅 | ac |
| `ac.bedroom` | 卧室空调 | 主卧 | ac |
| `camera.living` | 客厅云台摄像头 | 客厅 | camera |
| `lock.entry` | 大门人脸锁 | 门厅 | lock |

**流通道**：`camera.living` → `233666`，`lock.entry` → `233667`；播放地址 `/?stream=<通道名>`（见第八节）

**房间 id**：`living` / `kitchen` / `bedroom` / `bath` / `entry`
**楼层**：`floor` 为 `1` 或 `2`

---

## 十二、动手练习

按难度递增，都可以在 `demo.html` 的控制台里直接做：

1. **读**：把所有传感器的温湿度打成一个表格，每 5 秒刷新一次
2. **控**：写一个"一键观影"——客厅灯 12%、厨房关、空调 24°C 低风（不用场景接口，自己发 3 条 command）
3. **判**：当客厅温度 > 28°C 且空调未开时，自动开空调制冷 26°C
4. **看**：把 `video` 流信令接成一条实时折线图，横轴是时间、纵轴是云台角度
5. **扩**：给 `demo.html` 加一个"离线检测"——超过 10 秒没收到任何 `state` 就标红提示（提示：注意 `snapshot` 里也有全量，别误判）
6. **播**：写一个 `<img src="/?stream=233666">`，再发一条 `stream` 指令，观察画面从待机切到实景——**页面不刷新**

---

## 十三、常见问题

**连不上，浏览器控制台报 `WebSocket connection failed`**

- 服务没起：`curl http://localhost:8080/health` 应该返回 `{"ok":true,...}`
- 路径不对：端点必须是 `/ws`，不是 `/`
- 页面是 HTTPS 而网关是 HTTP：浏览器会拦混合内容。要么把网关也上 HTTPS（用 `wss://`），要么用 HTTP 页面打开 demo

**连上后立刻断开，close code `4401`**

没带令牌，或令牌不对。令牌在网关启动日志里；三种携带方式见 3.1。
用 demo 客户端的话，把它填进右上角的令牌框。

**连上后立刻断开，close code `4426`**

协议主版本不匹配。检查客户端信封里的 `v` 是否与网关主版本一致（网关当前为 `1.0.0`）。
把它改成 `"1.0.0"` 即可 —— 放行新旧混用会让「界面上某些值永远不动」，比直接报错难查得多。

**连上了但设备列表是空的**

两种可能：① 你只处理了 `state` 没处理 `snapshot` —— **全量在 `snapshot` 里**，
认证通过后只推一次；② 你还处在未认证状态（`hello.payload.authed === false`），
网关不会向未认证连接下发影子。

**传感器报文很久才来一条，是不是掉线了？**

不是。传感器按**变化上报**（对齐 Zigbee 的 `ReportableChange`）：
温度变化 ≥0.2 °C 或湿度变化 ≥1 % 才发一条，最长 600 秒保活一次。
稳态下几十秒没有报文是正常的。判断在线请用 `online` 字段或 `/api/diagnostics`，
不要用「多久没收到报文」。

**界面状态不更新**

先确认你处理的是 `state` 而不是 `ack`。`ack` 只回给你，`state` 才是广播 —— 靠 `ack` 更新界面的话，别的客户端改了状态你看不到。

**改完状态，本地显示变成了 `undefined`**

你把 `stateKind: "delta"` 的增量报文当成全量用了。`delta` 只含**本次变化**的字段，
必须与本地状态合并；只有 `stateKind: "full"` 才能直接替换。见 4.2。

**命令回了 `ok:true` 但设备没变**

看 `ack.payload.status`：如果是 `ACCEPTED`，说明网关**已受理**、设备还在执行中，
真正生效要看随后那条 `stateKind: "confirmed"` 的 `state`。
如果一直没等到 `confirmed`，设备可能离线或超时（`DEVICE_TIMEOUT`）。

**改了状态但界面没变**

检查 `deviceId` 有没有拼错。网关对不存在的设备返回 `ack.ok=false` + `errorInfo.code = DEVICE_NOT_FOUND`，不会静默忽略 —— 所以看报文面板就能定位。

**拖动滑杆时命令刷屏**

网关侧已经做了**抖动合并**：连发 8 条亮度命令，只有最后一条会真正执行、
只有一条 `confirmed` 广播。但你仍会收到 8 条 `ack`（受理回执）。
`demo.html` 里还额外做了 70ms 防抖（见 `slider()` 里的 `setTimeout`），双重保险。

**收到 `fault` 报文怎么办**

那是设备故障上报（灯珠损坏、滤网脏堵、电量低…）。`payload.severity` 是
`warn` / `error`。教学场景下可以用 `POST /api/simulate` 手动触发，见 6.2。

**`<img src="/?stream=233666">` 一直是待机画面**

说明设备没有推流。发一条 `stream` 指令（`params: { on: true }`）即可；注意摄像头还需要 `power` 为开。
如果返回 401，是**流通道也需要鉴权**：用 `?token=<令牌>` 或签名地址
（`GET /api/stream-url?channel=233666&token=<令牌>`），见第八节。

**收到的 `error` 报文里 `code` 是 `RATE_LIMITED`**

命令发太快了（默认 10 条/秒）。退避重试即可，连接不会被断开。

---

## 十四、这份文档对应的代码

| 文件 | 作用 |
|---|---|
| `public/demo.html` | 本文的配套客户端，单文件零依赖，`TwinClient` 类可整段拷走 |
| `scripts/selfcheck.py` | 用 Python 跑一遍完整交互（**44 项**），也是"如何对接网关"的最小范例 |
| `server/model.py` | 物模型与协议常量（**唯一真源**，前端文件由它生成） |
| `server/security.py` | 口令哈希 / 常数时间比较 / 流签名 URL |
| `server/validate.py` | 参数校验、能力位、结构化错误 |
| `server/derived.py` | 派生量（THI / 舒适度），带算法版本 |
| `shared/model.js` | 前端物模型，由 `scripts/export_model.py` 生成，**勿手改** |
| `server/gateway.py` | 网关实现，想看服务端怎么处理命令就读这里 |
| `server/stream.py` | MJPEG 流通道：订阅管理、按需渲染、待机帧、背压保护 |
| `server/main.py` | FastAPI 入口：静态资源 / 流通道 / WebSocket |
| `server/render/` | 服务端软渲染器与画面合成（客厅 / 门厅 / 待机），numpy + Pillow |
| `README.md` | 整体架构与教学说明 |
