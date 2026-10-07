#!/usr/bin/env python3
"""
从 server/model.py 生成前端使用的 shared/model.js。

为什么需要它：物模型本来就要被前后端同时使用。
  · Node 版可以直接把同一个 .js 文件既 import 给后端、又托管给浏览器；
  · Python 后端没法给浏览器吐 ES Module，如果手工再抄一份 JS，两边必然漂移。

所以这里把 Python 定义当作唯一真源，用生成器产出前端文件。
改了物模型（加设备、改通道名）之后跑一次：

    python scripts/export_model.py

自检脚本 scripts/selfcheck.py 也会校验生成物是否与真源一致。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import model as M  # noqa: E402

OUT = ROOT / "shared" / "model.js"


def js_value(v) -> str:
    """把 Python 字面量转成 JS 字面量（字符串用双引号，中文不转义）。"""
    return json.dumps(v, ensure_ascii=False)


def build() -> str:
    rooms = ",\n".join(
        "  " + json.dumps(r.to_dict(), ensure_ascii=False) for r in M.ROOMS
    )
    devices = ",\n".join(
        "  " + json.dumps(d.to_dict(), ensure_ascii=False, indent=2).replace("\n", "\n  ")
        for d in M.DEVICE_CATALOG
    )
    scenes = ",\n".join(
        f"  {json.dumps(key, ensure_ascii=False)}: " + json.dumps(val, ensure_ascii=False)
        for key, val in M.SCENES.items()
    )
    msg_fields = "\n".join(
        f'  {name}: "{getattr(M.MSG, name)}",'
        for name in ("HELLO", "SNAPSHOT", "COMMAND", "ACK", "STATE", "EVENT",
                     "SCENE", "VIDEO", "ERROR", "PING", "PONG", "SETUP")
    )

    return f"""/**
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

export const PROTOCOL_VERSION = {js_value(M.PROTOCOL_VERSION)};

/** 初始化设置口令。教学演示用，真实项目应换成服务端哈希校验。 */
export const SETUP_PASSWORD = {js_value(M.SETUP_PASSWORD)};

export const MSG = {{
{msg_fields}
}};

export const ROOMS = [
{rooms},
];

export const DEVICE_CATALOG = [
{devices},
];

export const SCENES = {{
{scenes},
}};

export function envelope(type, payload = {{}}, extra = {{}}) {{
  return {{
    v: PROTOCOL_VERSION,
    type,
    ts: Date.now(),
    ...extra,
    payload,
  }};
}}

export function deviceById(id) {{
  return DEVICE_CATALOG.find((d) => d.id === id) || null;
}}

/** 按视频流通道名反查设备 */
export function deviceByChannel(channel) {{
  const key = String(channel);
  return DEVICE_CATALOG.find((d) => d.channel === key) || null;
}}

export function roomById(id) {{
  return ROOMS.find((r) => r.id === id) || null;
}}
"""


def main() -> int:
    text = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"✓ 已生成 {OUT.relative_to(ROOT)}（{len(text)} 字节，{len(text.splitlines())} 行）")
    print(f"  设备 {len(M.DEVICE_CATALOG)} 台 · 房间 {len(M.ROOMS)} 个 · 场景 {len(M.SCENES)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
