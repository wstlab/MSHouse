# 把 MSHouse 接入 Home Assistant

用一座**虚拟别墅**当 Home Assistant 的受控对象：不买一颗灯泡、不接一个传感器，
就能把「灯 / 空调 / 门锁 / 摄像头 / 场景」全部跑起来，用来练自动化、练仪表盘、
练语音助手 —— 而所有设备都是真的在响应，不是假的静态实体。

---

## 一、为什么是 MQTT 桥接

三条路可以走，取舍很明确：

| 方案 | 做法 | 评价 |
|---|---|---|
| **MQTT Discovery**（本方案） | 桥接器把设备影子映射成 MQTT 主题，HA 自动发现实体 | ✅ 不用写 HA 自定义集成，不用在 HA 里堆 YAML，调试只要 `mosquitto_sub` |
| HA 自定义集成 | 写一个 `custom_components/mshouse`，用 WebSocket 直连网关 | 最"原生"，但要维护 config_flow、coordinator、各平台实体，代码量是桥接的 5 倍以上 |
| REST 轮询 | HA 的 `rest` 平台定时拉 `/api/model` | 简单但只能读不能写，轮询有延迟，且 HA 官方已不推荐 |

选 MQTT 还有个隐性好处：**MQTT 本身就是物联网教学的核心协议**。这个桥接器顺带把
「私有协议网关 → 标准协议网关」的翻译过程完整展示了一遍，正好是数字孪生课里
"协议适配层"的真实案例。

---

## 二、架构

```
┌──────────────┐   WebSocket    ┌──────────────┐   MQTT    ┌──────────────┐
│  MSHouse     │  JSON 报文     │  ha_bridge   │  主题     │    Home      │
│  网关 :8080  │ ←───────────→  │   (本模块)   │ ←──────→  │  Assistant   │
└──────────────┘                └──────────────┘           └──────────────┘
        │                                                        │
        │  HTTP / MJPEG（画面不走 MQTT）                          │
        └────────────────────────────────────────────────────────┘
                        camera: platform: mjpeg
```

**桥接器是零侵入的**：它只是一个普通的 WebSocket 客户端，和 `public/demo.html`
地位完全相同。`server/` 目录下的代码一行都不用改。这恰好验证了网关最初的设计
——"WebSocket 报文格式 + 标准 MJPEG"是两个公开契约，谁都能当客户端。

---

## 三、三步接入

### 1. 准备一个 MQTT Broker

已有 Mosquitto 就跳过。没有的话：

```bash
# 局域网内快速起一个（教学用，允许匿名）
mosquitto -c /etc/mosquitto/mosquitto.conf
```

HA 侧需要装 **Mosquitto broker** 加载项，或让 HA 指向你已有的 broker。

### 2. 启动 MSHouse 与桥接器

```bash
# 终端 A：网关
cd mshouse-py
pip install -r requirements.txt
python -m server.main

# 终端 B：桥接器
pip install paho-mqtt websockets
python integrations/ha_bridge.py --mqtt-host 127.0.0.1
```

桥接器支持的参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--ws` | `ws://127.0.0.1:8080/ws` | MSHouse 网关地址 |
| `--mqtt-host` / `--mqtt-port` | `127.0.0.1` / `1883` | Broker 地址 |
| `--mqtt-username` / `--mqtt-password` | 空 | 需要认证时填 |
| `--mqtt-tls` | 关 | 启用 TLS（自签证书不校验） |
| `--prefix` | `mshouse` | MQTT 主题前缀 |
| `--print-camera-yaml` | — | 打印摄像头 YAML 片段后退出 |

也支持同名环境变量：`MSHOUSE_WS`、`MQTT_HOST`、`MQTT_PORT`、`MQTT_USERNAME`、`MQTT_PASSWORD`。

### 3. 在 HA 里添加 MQTT 集成

**设置 → 设备与服务 → 添加集成 → MQTT**，填 broker 地址即可。

连接成功后，HA 会自动收到桥接器发布的 Discovery 配置，**34 个实体自动出现**，
不需要写任何 YAML。用 `mosquitto_sub` 可以确认：

```bash
mosquitto_sub -h 127.0.0.1 -t 'homeassistant/#' -v | head
```

---

## 四、实体映射表

MSHouse 的 12 台设备会展开成 34 个 HA 实体，并且**按房间自动归入对应区域**
（靠 Discovery 里的 `suggested_area` 字段，HA 会自动创建区域）。

### 灯（4 个 `light` 实体）

| MSHouse 设备 | HA 实体 | 能力 |
|---|---|---|
| `light.living` 客厅主灯 | `light.mshouse_light_living` | 开关、亮度 0–100、色温 2700–6500 K |
| `light.kitchen` 厨房灯 | `light.mshouse_light_kitchen` | 同上 |
| `light.bedroom` 卧室灯 | `light.mshouse_light_bedroom` | 同上 |
| `light.bath` 卫生间灯 | `light.mshouse_light_bath` | 同上 |

### 温湿度（12 个 `sensor` 实体）

每台传感器展开成 3 个实体（温度 / 湿度 / 舒适度），共 4 组：

| MSHouse 设备 | 实体 |
|---|---|
| `sensor.living` 客厅温湿度 | `..._living_temperature`、`..._humidity`、`..._comfort` |
| `sensor.kitchen` 厨房温湿度 | `..._kitchen_*` |
| `sensor.bedroom` 卧室温湿度 | `..._bedroom_*` |
| `sensor.bath` 卫生间温湿度 | `..._bath_*` |

温湿度带 `device_class` 和 `state_class`，可以直接进 HA 的历史统计与长短期图表。

### 空调（2 个 `climate` 实体）

| MSHouse 设备 | HA 实体 | 能力 |
|---|---|---|
| `ac.living` 客厅空调 | `climate.mshouse_ac_living` | 开关、模式、目标温度 16–30 ℃、风速 |
| `ac.bedroom` 卧室空调 | `climate.mshouse_ac_bedroom` | 同上 |

> **名称翻译**：MSHouse 的模式用 `fan`、风速用 `mid`，HA 的标准枚举是 `fan_only`
> 和 `medium`。桥接层负责双向翻译，两边都不用妥协。这是最容易踩的坑 —— 网关对
> 不认识的枚举值是**静默忽略**的，如果直接透传，HA 里点一下会"看起来成功、实际没生效"。

### 摄像头（1 个 `switch` + 1 个 `number` + 1 个 `binary_sensor`）

| 实体 | 说明 |
|---|---|
| `switch.mshouse_camera_living_stream` | 推流开关。开 = 通道切实景，关 = 回到待机画面 |
| `number.mshouse_camera_living_pan` | 云台角度 0–355°，步进 5° |
| `binary_sensor.mshouse_camera_living_motion` | 移动侦测 |

画面本身**不走 MQTT**（HA 的 MQTT 集成不支持 `camera` 实体），用 YAML 直连，
见下一节。

### 门锁（1 个 `lock` + 2 个 `sensor` + 1 个 `button`）

| 实体 | 说明 |
|---|---|
| `lock.mshouse_lock_entry` | 上锁 / 开锁 |
| `button.mshouse_lock_entry_face` | 触发一次人脸识别开锁（住户通过、陌生人拒绝） |
| `sensor.mshouse_lock_entry_battery` | 电量（诊断类） |
| `sensor.mshouse_lock_entry_visitor` | 最近访客（诊断类） |

### 场景与网关（5 个 `button` + 5 个 `sensor`）

| 实体 | 说明 |
|---|---|
| `button.mshouse_scene_home` | 回家模式 |
| `button.mshouse_scene_away` | 离家模式 |
| `button.mshouse_scene_sleep` | 睡眠模式 |
| `button.mshouse_scene_movie` | 观影模式 |
| `sensor.mshouse_gateway_scene` | 当前场景（文本） |
| `sensor.mshouse_gateway_occupancy` | 在家状态：有人 / 无人 / 睡眠 |
| `sensor.mshouse_gateway_outdoor_temp` | 室外温度（初始化后来自真实天气预报） |
| `sensor.mshouse_gateway_outdoor_humidity` | 室外湿度 |
| `sensor.mshouse_gateway_place` | 所在地名 |

---

## 五、摄像头：唯一需要写 YAML 的地方

HA 的 MQTT 集成不支持 `camera` 实体，所以画面走 YAML 直连 —— 好在 MSHouse 输出的是
**标准 MJPEG**（`multipart/x-mixed-replace`），正好是 HA `mjpeg` 平台的原生输入。

先生成片段：

```bash
python integrations/ha_bridge.py --print-camera-yaml http://192.168.1.20:8080
```

把输出粘进 `configuration.yaml`（或 `packages/` 下），重启 HA：

```yaml
camera:
  - platform: mjpeg
    name: 客厅云台摄像头
    unique_id: mshouse_camera_living
    mjpeg_url: http://192.168.1.20:8080/?stream=233666
    still_image_url: http://192.168.1.20:8080/?stream=233666
  - platform: mjpeg
    name: 大门人脸锁
    unique_id: mshouse_lock_entry_cam
    mjpeg_url: http://192.168.1.20:8080/?stream=233667
    still_image_url: http://192.168.1.20:8080/?stream=233667
```

> 把 `192.168.1.20:8080` 换成 HA 能访问到的 MSHouse 地址。HA 和 MSHouse 不在同一台
> 机器上时，别写 `127.0.0.1`。

**一个天然契合点**：MSHouse 的通道是**按需渲染**的 —— 只有存在订阅者时才启动帧循环。
HA 打开摄像头卡片 → 建立 MJPEG 长连接 → 网关开始渲染；关掉卡片 → 连接断开 → 渲染停止。
CPU 占用随"有没有人看"自动归零，和真实 IP 摄像头的行为完全一致。

---

## 六、自动化示例

### 示例 1：门锁开了就亮玄关灯（MQTT 触发器）

```yaml
automation:
  - alias: 门锁打开时开客厅灯
    trigger:
      - platform: state
        entity_id: lock.mshouse_lock_entry
        to: "unlocked"
    action:
      - service: light.turn_on
        target:
          entity_id: light.mshouse_light_living
        data:
          brightness_pct: 80
```

### 示例 2：监听安防事件（原生 MQTT 触发器）

桥接器会把网关的 `event` 报文原样转发到 `mshouse/event`，包含安防告警、场景切换等：

```yaml
automation:
  - alias: 摄像头检测到移动时通知
    trigger:
      - platform: mqtt
        topic: mshouse/event
    condition:
      - condition: template
        value_template: "{{ trigger.payload_json.level == 'alarm' }}"
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "{{ trigger.payload_json.message }}"
```

事件负载形如：

```json
{"id":"evt-42","ts":1791361787585,"level":"alarm",
 "source":"camera.living","message":"客厅摄像头检测到移动（布防中）"}
```

`level` 取值：`ok` / `info` / `warn` / `alarm`。

### 示例 3：用 HA 做「人在家才开空调」

这类逻辑在 HA 里写比在 MSHouse 里写更合适 —— MSHouse 只负责"设备怎么响应"，
"什么时候该响应"交给 HA 的自动化引擎。这正是分层设计的意义。

```yaml
automation:
  - alias: 有人且室外超过 30 度时开空调
    trigger:
      - platform: numeric_state
        entity_id: sensor.mshouse_gateway_outdoor_temp
        above: 30
        for: "00:05:00"
    condition:
      - condition: state
        entity_id: sensor.mshouse_gateway_occupancy
        state: "有人"
    action:
      - service: climate.set_temperature
        target:
          entity_id: climate.mshouse_ac_living
        data:
          temperature: 26
          hvac_mode: cool
```

---

## 七、常见问题

**Q：实体没出现？**
按顺序排查：① `mosquitto_sub -t 'homeassistant/#' -v` 能看到配置说明桥在正常工作；
② 确认 HA 的 MQTT 集成已连接、且 Discovery 前缀是 `homeassistant`（桥接器默认值）；
③ 看 HA 日志里 MQTT 集成的报错。

**Q：HA 里能控制，但设备没反应？**
大概率是**名称映射**问题。用 `mosquitto_sub -t 'mshouse/#' -v` 看命令发出去后状态有没有回写。
空调的模式/风速是唯一需要翻译的字段，如果自己改了代码，重点检查 `AC_MODE_HA_TO_MS` 和
`AC_FAN_HA_TO_MS` 两张表。

**Q：实体显示「不可用」？**
桥接器挂了。它用了 MQTT 遗嘱（LWT），异常退出时 broker 会代为广播 `mshouse/bridge/status = offline`，
HA 立刻把实体置为不可用 —— 这样自动化不会拿着过期数据做判断。重启桥接器即可恢复。

**Q：HA 重启后实体还在吗？**
在。Discovery 配置和状态都是 `retain=True` 发布的，broker 会保留，HA 启动时立刻拿到。

**Q：MSHouse 重启后 HA 会怎样？**
桥接器会自动重连网关（3 秒起、指数退避到 30 秒），重连后收到新的 `snapshot` 并重新同步全部状态。
期间 MQTT 侧标记为 offline。

**Q：可以接到 HomeKit / 小米 / 天猫精灵吗？**
可以。HA 的 **HomeKit Bridge** 集成能把这些实体再桥接给 Apple 家庭，小米/天猫的集成同理。
也就是说 MSHouse → MQTT → HA → HomeKit 这条链是通的，全程零硬件。

**Q：能改成别的前缀吗？**
`--prefix mshouse` 改主题前缀，`--discovery-prefix` 改 Discovery 前缀（一般不用动）。

---

## 八、把桥接器做成常驻服务

教学演示可以手动跑；长期运行建议交给 systemd：

```ini
# /etc/systemd/system/mshouse-ha-bridge.service
[Unit]
Description=MSHouse to Home Assistant MQTT bridge
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/mshouse-py
Environment=MQTT_HOST=192.168.1.10
ExecStart=/usr/bin/python3 integrations/ha_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`Restart=always` 配合桥接器自身的重连逻辑，基本不需要人工干预。

---

## 附：主题结构一览

```
homeassistant/<component>/<object_id>/config   ← HA 自动发现配置（retained）
mshouse/<device_id>/state                      ← 设备状态（retained）
mshouse/<device_id>/set                        ← 设备命令
mshouse/<device_id>/<sub>/set                  ← 子命令（mode / temp / fan / stream / pan / face）
mshouse/scene/<scene_id>/set                   ← 场景触发
mshouse/gateway/state                          ← 网关级状态（场景、在家状态、室外天气）
mshouse/event                                  ← 事件流（安防告警、场景切换）
mshouse/bridge/status                          ← 桥在线状态（LWT）
```
