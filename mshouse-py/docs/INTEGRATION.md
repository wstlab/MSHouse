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

> 后端是 Python（FastAPI + asyncio），前端是 Three.js —— 两层通过标准协议对话，
> 所以本指南里的客户端代码与后端语言无关。

**第 2 步：打开 demo 客户端**

浏览器访问 `http://localhost:8080/demo.html`。页面**默认不连接**——确认右上角服务器地址是 `http://localhost:8080`，点「连接」才开始建链。断开后不会自动重连，再点一次即重新连上。

**第 3 步：认识界面**

连上后顶部状态灯变绿，左侧设备区分成两个标签：

| 标签 | 设备 | 作用 |
|---|---|---|
| **感知** | 4 台温湿度 + 客厅摄像头 + 大门人脸锁 | 勾选某台设备，它的报文（读数 / 流信令 / 识别记录）才会出现在右侧报文面板；**默认全部未勾选**，未勾选的会被过滤 |
| **执行** | 4 盏灯 + 2 台空调 + 摄像头 + 人脸锁 | 每台卡片带一份**报文样例**，展开后可「复制」或「填入执行框」 |

摄像头与人脸锁同时出现在两个标签里——它们既推流（感知）也接受命令（执行）。

**第 4 步：完整走一遍链路**

1. 到「感知」标签勾选**客厅温湿度** → 右侧报文面板立刻开始滚动 `state` 报文
2. 切到「执行」标签，展开**客厅主灯**的「报文样例」→ 点「填入执行框」
3. 到右侧「发送报文」卡片点**发送** → 报文面板出现一条 `↑ 发出`，紧跟着 `ack` 和 `state`；卡片上的状态行同步变成「开 · 亮度 80%」
4. 也可以直接在执行框里手写报文，`v` / `id` / `ts` 缺失会自动补齐：

```json
{ "type": "command", "payload": { "deviceId": "light.living", "action": "set", "params": { "power": true } } }
```

执行框不限于 `command`，`scene` / `snapshot` / `ping` / `setup` 都能发。

**换服务器地址**：直接改右上角输入框，或在 URL 上加参数：

```
http://localhost:8080/demo.html?server=192.168.1.20:8080
```

地址会记在 `localStorage`，下次打开自动填回。接受 `http://host:port`、`host:port`、`ws://...` 三种写法。

---

## 二、最小可运行客户端（20 行）

`demo.html` 里的 `TwinClient` 类是完整实现。如果你只想在浏览器控制台里试试，这些就够了：

```js
const ws = new WebSocket("ws://localhost:8080/ws");

ws.onopen = () => {
  // 读状态：打开客厅灯，亮度 60%
  ws.send(JSON.stringify({
    v: "1.0", type: "command", id: "c-1", ts: Date.now(),
    payload: { deviceId: "light.living", action: "set", params: { power: true, brightness: 60 } },
  }));
};

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  // 服务端建链后会主动推 hello + snapshot（全量影子），之后是增量 state
  if (msg.type === "snapshot") {
    for (const d of msg.payload.devices) {
      if (d.type === "sensor") console.log(d.name, d.state.temperature, d.state.humidity);
    }
  }
  if (msg.type === "state" && msg.payload.deviceId === "light.living") {
    console.log("客厅灯现在是：", msg.payload.state);
  }
};
```

**Python 客户端**同样简单（`scripts/selfcheck.py` 是完整范例，可以直接当范例读）：

```python
import asyncio
import json

import websockets


async def main():
    async with websockets.connect("ws://localhost:8080/ws") as ws:
        await ws.send(json.dumps({
            "v": "1.0", "type": "command", "id": "c-1",
            "payload": {"deviceId": "light.living", "action": "set",
                        "params": {"power": True, "brightness": 60}},
        }))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "snapshot":
                for d in msg["payload"]["devices"]:
                    if d["type"] == "sensor":
                        print(d["name"], d["state"]["temperature"], d["state"]["humidity"])
            elif msg["type"] == "state" and msg["payload"]["deviceId"] == "light.living":
                print("客厅灯现在是：", msg["payload"]["state"])


asyncio.run(main())
```

---

## 三、连接与握手

| 项 | 值 |
|---|---|
| 端点 | `ws://<host>:<port>/ws`（HTTPS 页面下用 `wss://`） |
| 子协议 | 无 |
| 鉴权 | 无（教学项目；真实系统应在升级请求里带 Token） |
| 编码 | UTF-8 JSON 文本帧 |

**建链后不需要发任何东西**，服务端会依次主动推三条：

1. `hello` — 网关标识与协议版本
2. `snapshot` — **全量**设备影子（12 台设备 + 场景定义 + 最近事件 + 站点/室外信息）
3. `event` — 一条 info 级接入事件

```json
{
  "v": "1.0",
  "type": "snapshot",
  "ts": 1796000000000,
  "payload": {
    "scene": "home",
    "occupancy": "home",
    "outdoor": { "temp": 24.7, "humidity": 91, "source": "forecast", "place": "杭州西湖" },
    "site": { "configured": true, "name": "杭州西湖", "lat": 30.27, "lon": 120.16,
              "weather": { "label": "多云", "code": 2, "wind": 8.4 } },
    "devices": [
      { "id": "light.living", "type": "light", "name": "客厅主灯", "room": "living",
        "floor": 1, "capabilities": ["switch","brightness","colorTemp"],
        "online": true, "updatedAt": 1796000000000,
        "state": { "power": true, "brightness": 85, "colorTemp": 3800 } }
    ],
    "scenes": { "home": { "id": "home", "name": "回家模式", "hint": "…" } },
    "recentEvents": [ { "id": "evt-12", "ts": 1796000000000, "level": "ok",
                        "source": "scene", "message": "已切换到回家模式" } ]
  }
}
```

**关键约定**：所有消息都是同一个信封 `{ v, type, id?, ts, payload }`。`v` 是协议版本，留作演进；`id` 用于把回执关联回请求。

---

## 四、读传感器

传感器是**只读遥测**设备，你只能读，不能写（写会被拒，见第七节）。

**两种读法**：

**① 被动接收**（推荐）——网关每 2 秒推一次全部传感器的 `state`：

```js
ws.onmessage = (e) => {
  const { type, payload } = JSON.parse(e.data);
  if (type === "state" && payload.type === "sensor") {
    console.log(payload.deviceId, payload.state.temperature, payload.state.humidity);
  }
};
```

**② 主动拉全量** —— 想立刻对账时：

```js
ws.send(JSON.stringify({ v: "1.0", type: "snapshot", id: "sync-1", ts: Date.now(), payload: {} }));
```

**传感器状态字段**：

| 字段 | 含义 | 说明 |
|---|---|---|
| `temperature` | 温度 °C | 受同房间空调、厨房灶台、卫生间热水影响 |
| `humidity` | 相对湿度 % | 受室外湿度与洗浴水汽影响 |
| `comfort` | 舒适度 | `舒适` / `一般` / `偏闷`，由温湿度共同判定 |

**室外环境**（不在设备列表里，是网关级数据）：

```js
// 来自 snapshot.payload.outdoor 或 setup 消息
{ temp: 24.7, humidity: 91, source: "forecast", place: "杭州西湖" }
```

`source` 为 `forecast` 时表示已按初始化设置的地理位置取过真实预报；为 `default` 时是网关的默认值。

---

## 五、控制设备

**一条命令的完整形状**：

```json
{
  "v": "1.0",
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

网关回一条 `ack`（`ref` 指向你的 `id`），成功后紧接着广播一条 `state`：

```json
{ "type": "ack", "id": "srv-9", "ref": "c-8",
  "payload": { "ok": true, "deviceId": "light.living", "action": "set",
               "error": null, "state": { "power": true, "brightness": 60, "colorTemp": 3800 } } }
```

```json
{ "type": "state", "payload": { "deviceId": "light.living", "type": "light",
                                "room": "living", "online": true,
                                "state": { "power": true, "brightness": 60, "colorTemp": 3800 } } }
```

> **注意**：`ack` 是**给你一个人的回执**，`state` 是**广播给所有人的**。所以多个客户端同时在线时，你的界面更新应该由 `state` 驱动，而不是由 `ack` 驱动 —— 否则别人改的状态你看不到。

### 各类型设备的可用动作

| 设备类型 | action | params | 说明 |
|---|---|---|---|
| **light** | `on` / `off` / `toggle` | — | 开关 |
| | `set` | `{ power, brightness, colorTemp }` | 任意组合。`brightness` 0–100，`colorTemp` 2700–6500K |
| **ac** | `on` / `off` / `toggle` | — | 开关 |
| | `set` | `{ power, mode, targetTemp, fan }` | `mode`: `cool`/`heat`/`fan`/`dry`/`auto`；`targetTemp` 16–30；`fan`: `low`/`mid`/`high`/`auto` |
| **camera** | `set` | `{ power, streaming, armed }` | 开关与布防 |
| | `pan` | `{ pan }` | 绝对角度，0–359，自动取模 |
| | `nudge` | `{ delta }` | 相对转动，如 `{ delta: 15 }` / `{ delta: -15 }` |
| | `arm` | `{ armed }` | 布防开关 |
| **lock** | `lock` / `unlock` | `{ person? }` | 上锁 / 开锁 |
| | `face` | `{ faceId }` | 模拟人脸识别，见下 |
| **sensor** | — | — | **只读**，任何写命令返回 `ok:false` |

**可用的 `faceId`**：

| faceId | 姓名 | 角色 | 结果 |
|---|---|---|---|
| `resident.lin` | 林晓 | 住户 | ✅ 通过并自动开锁 |
| `resident.chen` | 陈舟 | 住户 | ✅ 通过并自动开锁 |
| `guest.unknown` | 未登记访客 | 访客 | ❌ 拒绝，门保持锁定 |

**常用代码片段**：

```js
const cmd = (deviceId, action, params = {}) => ws.send(JSON.stringify({
  v: "1.0", type: "command", id: `c-${Date.now()}`, ts: Date.now(),
  payload: { deviceId, action, params },
}));

cmd("light.bedroom", "set", { power: true, brightness: 8, colorTemp: 2700 });  // 卧室夜灯
cmd("ac.living", "set", { power: true, mode: "cool", targetTemp: 25 });       // 客厅制冷 25°C
cmd("camera.living", "pan", { pan: 180 });                                    // 云台转 180°
cmd("camera.living", "nudge", { delta: -15 });                                // 向左微调 15°
cmd("lock.entry", "face", { faceId: "resident.lin" });                        // 刷脸开锁
cmd("lock.entry", "lock");                                                    // 远程上锁
```

### 场景模式

场景是**服务端的批量操作**，客户端只发一个 id，网关负责改一堆设备并广播结果：

```js
ws.send(JSON.stringify({ v: "1.0", type: "scene", id: "s-1", ts: Date.now(),
                         payload: { sceneId: "away" } }));
```

| sceneId | 名称 | 效果 |
|---|---|---|
| `home` | 回家模式 | 客厅/厨房灯亮，客厅空调制冷 25°C，摄像头撤防，门锁开 |
| `away` | 离家模式 | 全屋灯与空调关闭，摄像头布防并归零，门锁上锁 |
| `sleep` | 睡眠模式 | 只留卧室 8% 夜灯，卧室空调 26°C 低风，摄像头布防转 180° |
| `movie` | 观影模式 | 客厅调暗至 12%，厨房关闭，空调 24°C 低风 |

成功后收到 `scene`（通知）+ `snapshot`（全量，保证多客户端一致）+ `event`（日志）。

---

## 六、消息类型总表

| 方向 | type | payload 要点 |
|---|---|---|
| S→C | `hello` | `{ role, name, protocol }` |
| S→C | `snapshot` | 全量影子：`devices` / `scenes` / `scene` / `occupancy` / `outdoor` / `site` / `recentEvents` |
| S→C | `state` | `{ deviceId, type, room, online, state }`，单设备增量 |
| S→C | `event` | `{ id, ts, level, source, message }`，`level` ∈ `info`/`ok`/`warn`/`alarm` |
| S→C | `scene` | `{ sceneId, name, occupancy, source }` |
| S→C | `video` | 流信令：通道 / 推流状态 / 云台 / 观众数 / 帧率，见第八节 |
| S→C | `setup` | `{ site, outdoor }`，位置被改写后广播 |
| S→C | `ack` | `{ ok, deviceId, action, error, state }`，`ref` 关联请求 `id` |
| S→C | `error` | `{ code, message }`，`code` ∈ `BAD_JSON`/`BAD_ENVELOPE`/`UNKNOWN_TYPE` |
| S→C | `pong` | `{ echo }` |
| C→S | `command` | 设备控制 |
| C→S | `scene` | 场景切换 |
| C→S | `snapshot` | 主动拉全量 |
| C→S | `setup` | 初始化定位（需口令），见第九节 |
| C→S | `ping` | 心跳 |

**心跳**：服务端不主动断开空闲连接，但建议每 30–60 秒发一次 `ping` 保活，也用来测往返延迟：

```js
const t0 = performance.now();
cmd... // 或直接：
ws.send(JSON.stringify({ v: "1.0", type: "ping", id: "p-1", ts: Date.now(), payload: { at: t0 } }));
// 收到 pong 后：console.log("RTT", performance.now() - payload.echo.at, "ms");
```

---

## 七、错误处理

**两类错误，处理方式不同**：

**① 协议级错误 → `error` 消息**（你的报文本身有问题）

| code | 触发条件 |
|---|---|
| `BAD_JSON` | 发过去的不是合法 JSON |
| `BAD_ENVELOPE` | 缺 `type` 字段 |
| `UNKNOWN_TYPE` | `type` 不认识 |

```js
if (msg.type === "error") {
  console.error(`协议错误 ${msg.payload.code}：${msg.payload.message}`);
}
```

**② 业务级失败 → `ack.payload.ok === false`**（报文合法，但设备不买账）

```js
if (msg.type === "ack" && msg.payload.ok === false) {
  console.warn("命令被拒绝：", msg.payload.error);
}
```

常见拒绝原因：

- `设备不存在` —— `deviceId` 拼错
- `传感器为只读遥测设备` —— 试图写传感器
- `动作 xxx 不适用于 sensor` —— 动作与设备类型不匹配

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
  v: "1.0", type: "command", id: "cmd-stream-1", ts: Date.now(),
  payload: { deviceId: "camera.living", action: "stream", params: { on: true } }
}));
```

`ack` 里会带上通道的最新状态：

```json
{ "type": "ack", "ref": "cmd-stream-1", "payload": { "ok": true,
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

---

## 九、初始化设置（写入物理位置）

给别墅写入真实地理位置，网关据此取当地天气预报作为室外温度初值。**需要口令**（默认 `villa`，定义在 `server/model.py` 的 `SETUP_PASSWORD`）。

```js
ws.send(JSON.stringify({
  v: "1.0", type: "setup", id: "cfg-1", ts: Date.now(),
  payload: { password: "villa", lat: 30.2741, lon: 120.1551, name: "杭州 · 西湖" },
}));
```

成功后：

1. 收到 `ack`，`payload.site` 是写入结果，`payload.outdoor` 是按预报取的室外温湿度
2. 收到 `setup` 广播（所有客户端都能感知位置变了）
3. 收到 `snapshot`（全量刷新）
4. 室内传感器与空调回风温按室外值重新起算，避免定位后室内还停在默认值

口令错误或经纬度越界 → `ack.payload.ok === false`，**不改变任何状态**。

---

## 十、用浏览器控制台调试

`demo.html` 把客户端实例挂在了 `window` 上，打开开发者工具就能直接调：

```js
client.command("light.living", "set", { power: true, brightness: 60 })
client.command("camera.living", "pan", { pan: 90 })
client.setScene("away")
client.devices.get("sensor.living").state      // 看客厅温湿度当前值
client.resync()                                 // 拉一次全量
[...client.devices.values()].filter(d => d.type === "sensor").map(d => [d.name, d.state.temperature])
```

另外两个 HTTP 端点也可以直接用 `curl` 看：

```bash
curl http://localhost:8080/health      # 健康检查 + 当前连接数
curl http://localhost:8080/api/model   # 当前全量影子（与 snapshot 同结构）
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

**连上了但设备列表是空的**

你大概只处理了 `state` 没处理 `snapshot`。**全量在 `snapshot` 里**，建链后只推一次；`state` 只带变化的那一台设备。

**界面状态不更新**

先确认你处理的是 `state` 而不是 `ack`。`ack` 只回给你，`state` 才是广播 —— 靠 `ack` 更新界面的话，别的客户端改了状态你看不到。

**改了状态但界面没变**

检查 `deviceId` 有没有拼错。网关对不存在的设备返回 `ack.ok=false`，不会静默忽略 —— 所以看报文面板就能定位。

**拖动滑杆时命令刷屏**

`demo.html` 里做了 70ms 防抖（见 `slider()` 里的 `setTimeout`）。真实项目建议再加节流 + 合并，避免一条命令一个报文。

**`<img src="/?stream=233666">` 一直是待机画面**

说明设备没有推流。发一条 `stream` 指令（`params: { on: true }`）即可；也可以用 `curl http://localhost:8080/streams` 看通道的 `live` 状态。注意摄像头还需要 `power` 为开。

---

## 十四、这份文档对应的代码

| 文件 | 作用 |
|---|---|
| `public/demo.html` | 本文的配套客户端，单文件零依赖，`TwinClient` 类可整段拷走 |
| `scripts/selfcheck.py` | 用 Python 跑一遍完整交互（31 项），也是"如何对接网关"的最小范例 |
| `server/model.py` | 物模型与协议常量（**唯一真源**，前端文件由它生成） |
| `shared/model.js` | 前端物模型，由 `scripts/export_model.py` 生成，**勿手改** |
| `server/gateway.py` | 网关实现，想看服务端怎么处理命令就读这里 |
| `server/stream.py` | MJPEG 流通道：订阅管理、按需渲染、待机帧、背压保护 |
| `server/main.py` | FastAPI 入口：静态资源 / 流通道 / WebSocket |
| `server/render/` | 服务端软渲染器与画面合成（客厅 / 门厅 / 待机），numpy + Pillow |
| `README.md` | 整体架构与教学说明 |
