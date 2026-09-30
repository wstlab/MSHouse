/**
 * 协议层（浏览器侧）
 * 直接复用 shared/model.js —— 前后端共用同一份物模型与信封格式，避免两处定义漂移。
 * 服务端把 /shared 目录一并静态托管，所以浏览器能直接 import。
 */
export {
  PROTOCOL_VERSION,
  MSG,
  ROOMS,
  DEVICE_CATALOG,
  SCENES,
  envelope,
  deviceById,
  roomById,
} from "/shared/model.js";

let seq = 1;

/** 客户端本地生成的消息 ID，用于把 ack 关联回请求 */
export function nextId(prefix = "ui") {
  return `${prefix}-${Date.now().toString(36)}-${seq++}`;
}
