/**
 * MSHouse 镜像家居 · 物模型与协议常量
 * Mirror Space · 智能家居数字孪生教学场景
 *
 * ⚠️ 本文件由 scripts/export_model.py 从 server/model.py 自动生成，请勿手工修改。
 *    要改设备、通道名或场景，请编辑 server/model.py，然后运行：
 *
 *        python scripts/export_model.py
 *
 *    这样前后端永远共用同一份定义，不会出现两处漂移。
 */

export const PROTOCOL_VERSION = "2.0";

export const MSG = {
  HELLO: "hello",
  SNAPSHOT: "snapshot",
  COMMAND: "command",
  ACK: "ack",
  STATE: "state",
  EVENT: "event",
  SCENE: "scene",
  VIDEO: "video",
  ERROR: "error",
  PING: "ping",
  PONG: "pong",
  SETUP: "setup",
  SETUP_AUTH: "setupAuth",
  FACE_ENROLL: "faceEnroll",
  RESYNC: "resync",
  FAULT: "fault",
  OTA: "ota",
};

/** state 报文的语义：delta 增量 / full 全量 / confirmed 执行确认 */
export const STATE_KIND = {
  DELTA: "delta",
  FULL: "full",
  CONFIRMED: "confirmed",
};

/** 结构化错误码。客户端按 code 分支，不要去匹配中文文案。 */
export const ERROR_CODES = {
  BAD_JSON: "BAD_JSON",
  BAD_ENVELOPE: "BAD_ENVELOPE",
  UNKNOWN_TYPE: "UNKNOWN_TYPE",
  UNAUTHORIZED: "UNAUTHORIZED",
  FORBIDDEN: "FORBIDDEN",
  PROTOCOL_MISMATCH: "PROTOCOL_MISMATCH",
  DEVICE_NOT_FOUND: "DEVICE_NOT_FOUND",
  ACTION_NOT_SUPPORTED: "ACTION_NOT_SUPPORTED",
  UNSUPPORTED_CAPABILITY: "UNSUPPORTED_CAPABILITY",
  INVALID_PARAM: "INVALID_PARAM",
  READ_ONLY_DEVICE: "READ_ONLY_DEVICE",
  DEVICE_OFFLINE: "DEVICE_OFFLINE",
  DEVICE_TIMEOUT: "DEVICE_TIMEOUT",
  RATE_LIMITED: "RATE_LIMITED",
  SETUP_DENIED: "SETUP_DENIED",
  INTERNAL: "INTERNAL",
};

/** THI 温湿指数分级（机器可读；中文映射在 ui.js 里） */
export const COMFORT = {
  COOL: "cool",
  COMFORTABLE: "comfortable",
  WARM: "warm",
  HOT: "hot",
};

/** 上报参数（对应 Zigbee 的 ReportableChange / Min / MaxReportingInterval） */
export const REPORTING = {
  "sensor": {
    "temperature": {
      "reportableChange": 0.2,
      "minInterval": 10,
      "maxInterval": 600
    },
    "humidity": {
      "reportableChange": 1.0,
      "minInterval": 10,
      "maxInterval": 600
    },
    "battery": {
      "reportableChange": 1.0,
      "minInterval": 300,
      "maxInterval": 3600
    },
    "rssi": {
      "reportableChange": 3.0,
      "minInterval": 60,
      "maxInterval": 1800
    }
  },
  "ac": {
    "indoorTemp": {
      "reportableChange": 0.3,
      "minInterval": 15,
      "maxInterval": 900
    }
  },
  "lock": {
    "battery": {
      "reportableChange": 1.0,
      "minInterval": 300,
      "maxInterval": 3600
    }
  }
};

export const ROOMS = [
  {"id": "living", "name": "客厅", "floor": 1, "zone": "公共区"},
  {"id": "kitchen", "name": "厨房", "floor": 1, "zone": "公共区"},
  {"id": "bedroom", "name": "主卧", "floor": 2, "zone": "私密区"},
  {"id": "bath", "name": "卫生间", "floor": 2, "zone": "私密区"},
  {"id": "entry", "name": "门厅", "floor": 1, "zone": "安防区"},
];

export const DEVICE_CATALOG = [
  {
    "id": "light.living",
    "type": "light",
    "name": "客厅主灯",
    "room": "living",
    "floor": 1,
    "capabilities": [
      "switch",
      "brightness",
      "colorTemp"
    ]
  },
  {
    "id": "light.kitchen",
    "type": "light",
    "name": "厨房灯",
    "room": "kitchen",
    "floor": 1,
    "capabilities": [
      "switch",
      "brightness",
      "colorTemp"
    ]
  },
  {
    "id": "light.bedroom",
    "type": "light",
    "name": "卧室灯",
    "room": "bedroom",
    "floor": 2,
    "capabilities": [
      "switch",
      "brightness",
      "colorTemp"
    ]
  },
  {
    "id": "light.bath",
    "type": "light",
    "name": "卫生间灯",
    "room": "bath",
    "floor": 2,
    "capabilities": [
      "switch",
      "brightness",
      "colorTemp"
    ]
  },
  {
    "id": "sensor.living",
    "type": "sensor",
    "name": "客厅温湿度",
    "room": "living",
    "floor": 1,
    "capabilities": [
      "telemetry"
    ]
  },
  {
    "id": "sensor.kitchen",
    "type": "sensor",
    "name": "厨房温湿度",
    "room": "kitchen",
    "floor": 1,
    "capabilities": [
      "telemetry"
    ]
  },
  {
    "id": "sensor.bedroom",
    "type": "sensor",
    "name": "卧室温湿度",
    "room": "bedroom",
    "floor": 2,
    "capabilities": [
      "telemetry"
    ]
  },
  {
    "id": "sensor.bath",
    "type": "sensor",
    "name": "卫生间温湿度",
    "room": "bath",
    "floor": 2,
    "capabilities": [
      "telemetry"
    ]
  },
  {
    "id": "ac.living",
    "type": "ac",
    "name": "客厅空调",
    "room": "living",
    "floor": 1,
    "capabilities": [
      "switch",
      "mode",
      "targetTemp",
      "fan"
    ]
  },
  {
    "id": "ac.bedroom",
    "type": "ac",
    "name": "卧室空调",
    "room": "bedroom",
    "floor": 2,
    "capabilities": [
      "switch",
      "mode",
      "targetTemp",
      "fan"
    ]
  },
  {
    "id": "camera.living",
    "type": "camera",
    "name": "客厅云台摄像头",
    "room": "living",
    "floor": 1,
    "capabilities": [
      "switch",
      "pan",
      "stream"
    ],
    "channel": "233666"
  },
  {
    "id": "lock.entry",
    "type": "lock",
    "name": "大门人脸锁",
    "room": "entry",
    "floor": 1,
    "capabilities": [
      "lock",
      "stream"
    ],
    "channel": "233667"
  },
  {
    "id": "curtain.bedroom",
    "type": "curtain",
    "name": "主卧电动窗帘",
    "room": "bedroom",
    "floor": 2,
    "capabilities": [
      "switch",
      "position"
    ]
  },
];

export const SCENES = {
  "home": {"id": "home", "name": "回家模式", "hint": "开玄关与客厅灯，客厅空调舒适，摄像头待命"},
  "away": {"id": "away", "name": "离家模式", "hint": "全屋关灯关空调，上锁，摄像头布防"},
  "sleep": {"id": "sleep", "name": "睡眠模式", "hint": "只留卧室夜灯，卧室空调静音，大门上锁"},
  "movie": {"id": "movie", "name": "观影模式", "hint": "客厅调暗，厨房关闭，空调低风"},
};

export function envelope(type, payload = {}, extra = {}) {
  return {
    v: PROTOCOL_VERSION,
    type,
    ts: Date.now(),
    ...extra,
    payload,
  };
}

export function deviceById(id) {
  return DEVICE_CATALOG.find((d) => d.id === id) || null;
}

/** 按视频流通道名反查设备 */
export function deviceByChannel(channel) {
  const key = String(channel);
  return DEVICE_CATALOG.find((d) => d.channel === key) || null;
}

export function roomById(id) {
  return ROOMS.find((r) => r.id === id) || null;
}
