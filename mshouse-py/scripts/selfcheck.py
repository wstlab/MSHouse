#!/usr/bin/env python3
"""
协议自检：以真实 WebSocket 客户端身份跑一遍完整交互。

教学用途：这份脚本本身就是「如何对接网关」的最小示例。

    python scripts/selfcheck.py [ws://host:port/ws]

与 Node 版的差异：用 asyncio + websockets 重写，消息等待改成「消息总线 +
条件变量」，语义与原来的 once() 一致（只匹配注册之后到达的报文）。
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.model import DEVICE_CATALOG, envelope  # noqa: E402

WS_URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8080/ws"
HTTP_BASE = WS_URL.replace("wss://", "https://").replace("ws://", "http://").rsplit("/ws", 1)[0]

results: list[tuple[str, bool, str]] = []
_seq = 1


def next_id() -> str:
    global _seq
    v = f"chk-{_seq}"
    _seq += 1
    return v


def ok(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, bool(passed), detail))
    mark = "✓" if passed else "✗"
    print(f"  {mark} {name}" + (f"  {detail}" if detail else ""))


class Bus:
    """消息总线：多个等待者可以同时等不同类型/条件的报文。"""

    def __init__(self) -> None:
        self._msgs: list[dict] = []
        self._cond = asyncio.Condition()

    async def push(self, msg: dict) -> None:
        async with self._cond:
            self._msgs.append(msg)
            self._cond.notify_all()

    async def wait_for(self, type_: str, predicate=None, timeout: float = 3.0,
                       since: int | None = None) -> dict:
        """
        等待一条指定类型的报文。

        默认只匹配「调用之后」到达的消息（与 Node 版 once 的注册语义一致）。
        传 since=0 则从头扫描缓冲区 —— 用于服务端在建链瞬间就推来的 hello / snapshot，
        那两条报文会早于 wait_for 的调用时刻到达。
        """
        start = len(self._msgs) if since is None else since
        deadline = time.monotonic() + timeout
        while True:
            async with self._cond:
                for m in self._msgs[start:]:
                    if m.get("type") == type_ and (predicate is None or predicate(m)):
                        return m
                remain = deadline - time.monotonic()
                if remain <= 0:
                    raise TimeoutError(f"等待 {type_} 超时")
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remain)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"等待 {type_} 超时")


async def pull_frame(client: httpx.AsyncClient, path: str, timeout: float = 6.0) -> dict:
    """从 MJPEG 流通道拉一段数据，验证像素通道真的在出图。"""
    try:
        async with client.stream("GET", HTTP_BASE + path, timeout=timeout) as res:
            ctype = res.headers.get("content-type", "")
            total = 0
            async for chunk in res.aiter_bytes():
                total += len(chunk)
                if total >= 2000:
                    return {"ok": True, "bytes": total, "contentType": ctype}
            return {"ok": total >= 2000, "bytes": total, "contentType": ctype}
    except Exception:
        return {"ok": False, "bytes": 0, "contentType": ""}


async def get_json(client: httpx.AsyncClient, path: str):
    try:
        res = await client.get(HTTP_BASE + path, timeout=6.0)
        return res.json()
    except Exception:
        return None


async def main() -> int:
    print(f"\nMSHouse 镜像家居 · 协议自检（Python 版） → {WS_URL}\n")

    async with httpx.AsyncClient() as http:
        async with websockets.connect(WS_URL, max_size=None) as ws:
            bus = Bus()

            async def reader() -> None:
                try:
                    async for raw in ws:
                        try:
                            await bus.push(json.loads(raw))
                        except Exception:
                            continue
                except Exception:
                    return

            reader_task = asyncio.create_task(reader())
            ok("WebSocket 建链", True)

            async def send(msg: dict) -> None:
                await ws.send(json.dumps(msg, ensure_ascii=False))

            # 1. 握手 + 全量影子（这两条在握手瞬间就推来了，所以要从头扫缓冲区）
            hello = await bus.wait_for("hello", since=0)
            snap = await bus.wait_for("snapshot", since=0)
            ok("收到 hello 握手", (hello.get("payload") or {}).get("protocol") == "mshouse/1.0",
               (hello.get("payload") or {}).get("name", ""))
            devices = (snap.get("payload") or {}).get("devices") or []
            ok("收到全量设备影子", isinstance(devices, list), f"{len(devices)} 台设备")
            ok("设备数量与物模型一致", len(devices) == len(DEVICE_CATALOG),
               f"期望 {len(DEVICE_CATALOG)}，实际 {len(devices)}")
            ok("快照包含场景定义", len((snap.get("payload") or {}).get("scenes") or {}) >= 3)

            # 2. 下发命令 → ack + state 推送
            ack_t = asyncio.create_task(bus.wait_for("ack", lambda m: m.get("ref") == "cmd-light"))
            state_t = asyncio.create_task(bus.wait_for(
                "state", lambda m: (m.get("payload") or {}).get("deviceId") == "light.living"))
            await send(envelope("command", {"deviceId": "light.living", "action": "set",
                                            "params": {"power": True, "brightness": 42}}, id="cmd-light"))
            ack, st = await asyncio.gather(ack_t, state_t)
            ok("命令返回 ack", (ack.get("payload") or {}).get("ok") is True)
            st_state = (st.get("payload") or {}).get("state") or {}
            ok("状态变更被推送", st_state.get("power") is True and st_state.get("brightness") == 42,
               f"brightness={st_state.get('brightness')}")

            # 3. 场景联动
            scene_t = asyncio.create_task(bus.wait_for("scene"))
            snap_after_t = asyncio.create_task(bus.wait_for(
                "snapshot", lambda m: (m.get("payload") or {}).get("scene") == "sleep"))
            await send(envelope("scene", {"sceneId": "sleep"}, id="cmd-scene"))
            scene_msg, snap_after = await asyncio.gather(scene_t, snap_after_t)

            after_devices = (snap_after.get("payload") or {}).get("devices") or []
            lights = {d["id"]: d["state"] for d in after_devices if d.get("type") == "light"}
            ok("场景广播生效", (scene_msg.get("payload") or {}).get("sceneId") == "sleep",
               (scene_msg.get("payload") or {}).get("name", ""))
            ok("睡眠模式：客厅/厨房灯关闭",
               lights["light.living"]["power"] is False and lights["light.kitchen"]["power"] is False)
            ok("睡眠模式：卧室留夜灯",
               lights["light.bedroom"]["power"] is True and lights["light.bedroom"]["brightness"] <= 15,
               f"brightness={lights['light.bedroom']['brightness']}")
            lock = next((d for d in after_devices if d["id"] == "lock.entry"), None)
            ok("睡眠模式：大门上锁", bool(lock and lock["state"]["locked"] is True))

            # 4. 门锁远程控制：开锁 → 状态广播与事件；再上锁
            unlock_t = asyncio.create_task(bus.wait_for(
                "state", lambda m: (m.get("payload") or {}).get("deviceId") == "lock.entry"
                and ((m.get("payload") or {}).get("state") or {}).get("locked") is False))
            unlock_evt = asyncio.create_task(bus.wait_for(
                "event", lambda m: (m.get("payload") or {}).get("source") == "lock.entry"))
            await send(envelope("command", {"deviceId": "lock.entry", "action": "unlock",
                                            "params": {}}, id="cmd-unlock"))
            unlocked, unlock_event = await asyncio.gather(unlock_t, unlock_evt)
            us = (unlocked.get("payload") or {}).get("state") or {}
            ok("远程开锁生效", us.get("locked") is False)
            ok("开锁产生事件", (unlock_event.get("payload") or {}).get("level") == "ok",
               (unlock_event.get("payload") or {}).get("message", ""))

            relock_t = asyncio.create_task(bus.wait_for("ack", lambda m: m.get("ref") == "cmd-relock"))
            relock_state = asyncio.create_task(bus.wait_for(
                "state", lambda m: (m.get("payload") or {}).get("deviceId") == "lock.entry"
                and ((m.get("payload") or {}).get("state") or {}).get("locked") is True))
            await send(envelope("command", {"deviceId": "lock.entry", "action": "lock", "params": {}},
                                id="cmd-relock"))
            ok("远程上锁成功", ((await relock_t).get("payload") or {}).get("ok") is True)
            ok("上锁状态已广播", (((await relock_state).get("payload") or {}).get("state") or {}).get("locked") is True)

            # 已下线的模拟人脸动作应被拒绝（人脸改由上传画面 / 实时摄像头识别驱动）
            gone_face_t = asyncio.create_task(bus.wait_for("ack", lambda m: m.get("ref") == "cmd-face-gone"))
            await send(envelope("command", {"deviceId": "lock.entry", "action": "face",
                                            "params": {"faceId": "resident.lin"}}, id="cmd-face-gone"))
            ok("模拟人脸动作已移除", ((await gone_face_t).get("payload") or {}).get("ok") is False)

            # 5. 云台控制
            pan_t = asyncio.create_task(bus.wait_for(
                "state", lambda m: (m.get("payload") or {}).get("deviceId") == "camera.living"
                and ((m.get("payload") or {}).get("state") or {}).get("pan") == 275))
            await send(envelope("command", {"deviceId": "camera.living", "action": "pan",
                                            "params": {"pan": 275}}, id="cmd-pan"))
            pan_state = ((await pan_t).get("payload") or {}).get("state") or {}
            ok("云台角度可控", pan_state.get("pan") == 275)

            # 6. 视频流：信令走 WebSocket，像素走独立的 HTTP 通道
            # 先回到关流状态 —— 信令只在状态「变化」时广播，上一轮残留 streaming=true 会导致本次不广播
            pre_off = asyncio.create_task(bus.wait_for("ack", lambda m: m.get("ref") == "cmd-cam-pre"))
            await send(envelope("command", {"deviceId": "camera.living", "action": "stream",
                                            "params": {"on": False}}, id="cmd-cam-pre"))
            await pre_off

            live_t = asyncio.create_task(bus.wait_for(
                "video", lambda m: (m.get("payload") or {}).get("deviceId") == "camera.living"
                and (m.get("payload") or {}).get("live") is True, timeout=5.0))
            cam_on = asyncio.create_task(bus.wait_for("ack", lambda m: m.get("ref") == "cmd-cam"))
            await send(envelope("command", {"deviceId": "camera.living", "action": "stream",
                                            "params": {"on": True}}, id="cmd-cam"))
            cam_ack = await cam_on
            ok("stream 指令被接受", (cam_ack.get("payload") or {}).get("ok") is True,
               f"通道 {(cam_ack.get('payload') or {}).get('stream', {}).get('channel')}")

            sig = (await live_t).get("payload") or {}
            ok("流信令广播 live=true",
               sig.get("channel") == "233666" and sig.get("streaming") is True,
               f"viewers={sig.get('viewers')} fps={sig.get('fps')}")

            frame = await pull_frame(http, "/?stream=233666")
            ok("MJPEG 流通道可用", "multipart/x-mixed-replace" in frame["contentType"],
               frame["contentType"].split(";")[0])
            ok("流里能取到 JPEG 帧", frame["bytes"] > 1000, f"{frame['bytes']} 字节")

            listed = await get_json(http, "/streams")
            ch = next((s for s in (listed or {}).get("streams", []) if s.get("channel") == "233666"), None)
            ok("/streams 反映推流状态", bool(ch and ch.get("live") is True),
               f"viewers={(ch or {}).get('viewers')}")

            cam_off = asyncio.create_task(bus.wait_for("ack", lambda m: m.get("ref") == "cmd-cam-off"))
            await send(envelope("command", {"deviceId": "camera.living", "action": "stream",
                                            "params": {"on": False}}, id="cmd-cam-off"))
            off_payload = (await cam_off).get("payload") or {}
            ok("stream off 生效", (off_payload.get("stream") or {}).get("live") is False)

            # 7. 错误处理
            bad_device = asyncio.create_task(bus.wait_for("ack", lambda m: m.get("ref") == "cmd-bad"))
            await send(envelope("command", {"deviceId": "no.such.device", "action": "set", "params": {}},
                                id="cmd-bad"))
            ok("未知设备被拒绝", ((await bad_device).get("payload") or {}).get("ok") is False)

            bad_type = asyncio.create_task(bus.wait_for(
                "error", lambda m: (m.get("payload") or {}).get("code") == "UNKNOWN_TYPE"))
            await send(envelope("teleport", {}, id="cmd-unknown"))
            ok("未知消息类型返回 error",
               ((await bad_type).get("payload") or {}).get("code") == "UNKNOWN_TYPE")

            bad_json_t = asyncio.create_task(bus.wait_for(
                "error", lambda m: (m.get("payload") or {}).get("code") == "BAD_JSON"))
            await ws.send("这不是 JSON")
            ok("非法 JSON 被拦截",
               ((await bad_json_t).get("payload") or {}).get("code") == "BAD_JSON")

            # 8. 心跳
            pong_t = asyncio.create_task(bus.wait_for("pong"))
            await send(envelope("ping", {"at": 1}, id="cmd-ping"))
            ok("ping/pong 正常", ((await pong_t).get("payload") or {}).get("echo", {}).get("at") == 1)

            # 9. 传感器只读
            ro_t = asyncio.create_task(bus.wait_for("ack", lambda m: m.get("ref") == "cmd-ro"))
            await send(envelope("command", {"deviceId": "sensor.living", "action": "set",
                                            "params": {"temperature": 99}}, id="cmd-ro"))
            ok("传感器拒写（只读遥测）", ((await ro_t).get("payload") or {}).get("ok") is False)

            # 10. 初始化设置：口令错误应拒绝，正确口令写入位置并改写室外温度
            bad_setup = asyncio.create_task(bus.wait_for("ack", lambda m: m.get("ref") == "cmd-setup-bad"))
            await send(envelope("setup", {"password": "wrong", "lat": 30.27, "lon": 120.15, "name": "测试"},
                                id="cmd-setup-bad"))
            ok("错误口令拒绝初始化", ((await bad_setup).get("payload") or {}).get("ok") is False)

            setup_ack_t = asyncio.create_task(bus.wait_for("ack", lambda m: m.get("ref") == "cmd-setup", timeout=12.0))
            setup_push_t = asyncio.create_task(bus.wait_for("setup", timeout=12.0))
            await send(envelope("setup", {"password": "villa", "lat": 30.2741, "lon": 120.1551,
                                          "name": "杭州西湖"}, id="cmd-setup"))
            setup_done, setup_msg = await asyncio.gather(setup_ack_t, setup_push_t)
            sp = setup_msg.get("payload") or {}
            site = sp.get("site") or {}
            outdoor = sp.get("outdoor") or {}
            ok("正确口令写入经纬度",
               (setup_done.get("payload") or {}).get("ok") is True
               and site.get("lat") == 30.2741 and site.get("lon") == 120.1551,
               f"{site.get('name')} {outdoor.get('temp')}°C")
            ok("室外温度来自预报而非默认值",
               outdoor.get("source") == "forecast" and isinstance(outdoor.get("temp"), (int, float))
               and math.isfinite(outdoor.get("temp", float("nan"))))

            # 收尾：恢复离家模式
            await send(envelope("scene", {"sceneId": "away"}, id="cmd-reset"))
            await asyncio.sleep(0.3)

            reader_task.cancel()

    failed = [r for r in results if not r[1]]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项\n")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except TimeoutError as err:
        print(f"\n自检中断：{err}\n")
        raise SystemExit(1)
    except (ConnectionRefusedError, OSError) as err:
        print(f"\n自检中断：无法连接 {WS_URL} —— {err}\n")
        raise SystemExit(1)
