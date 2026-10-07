#!/usr/bin/env python3
"""
MSHouse 摄像头快照 → MQTT（补齐 HA 的 camera 实体）

背景
----
HA 自2024 起逐步把「平台式」相机（mjpeg / generic / mqtt platform）移出核心，
到 2026.9 只剩 `mqtt` 集成自带的 camera 组件（components/mqtt/camera.py），
它做的事很简单：**订阅一个 MQTT 主题，收到 JPEG 字节就更新画面**。

所以本模块做的事只有一件：从网关的 MJPEG 长连接里抽出单帧 JPEG，
按固定间隔发到 `mshouse/<deviceId>/snapshot` 主题（base64，不带retain）。

原ha_bridge.py 注释里写「摄像头画面走 YAML mjpeg」的建议在 2026.x 已经失效，
那条路走不通了。本模块是替代方案。

用法
----
    # 随ha_bridge.py 一起跑（推荐，共享 MQTT 连接）
    python integrations/ha_camera.py --mqtt-host 127.0.0.1

    # 单独跑（调试用）
    python integrations/ha_camera.py --channel 233666 --name 客厅云台摄像头
"""

from __future__ import annotations

import argparse
import base64
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from server.model import DEVICE_CATALOG
except Exception:  # pragma: no cover
    DEVICE_CATALOG = []

LOG = logging.getLogger("ha_camera")

# 从 multipart/x-mixed-replace 里取出第一段完整的 JPEG
JPEG_RE = re.compile(rb"\xff\xd8\xff.*?\xff\xd9", re.DOTALL)

# 设备 id -> (中文名, 流通道)
CAMERAS: dict[str, tuple[str, str]] = {
    "camera.living": ("客厅云台摄像头", "233666"),
    "lock.entry": ("大门人脸锁", "233667"),
}


def grab_jpeg(base_url: str, channel: str, timeout: float = 8.0) -> bytes | None:
    """从 MJPEG 长连接里抓一帧 JPEG。

    MJPEG 是 multipart 长连接，正常会一直挂着；这里只读第一帧就断开，
    对网关无副作用（它会在客户端断开后自动降帧率）。
    """
    url = f"{base_url.rstrip('/')}/?stream={channel}"
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as c:
            with c.stream("GET", url) as r:
                if r.status_code != 200:
                    LOG.warning("%s 流返回 HTTP %s", channel, r.status_code)
                    return None
                buf = b""
                for chunk in r.iter_bytes(8192):
                    buf += chunk
                    m = JPEG_RE.search(buf)
                    if m:
                        return m.group(0)
                    if len(buf) > 4 * 1024 * 1024:
                        break
    except Exception as exc:
        LOG.warning("抓帧失败 %s：%s", channel, exc)
    return None


class CameraPublisher:
    """按固定间隔为每个摄像头抓帧并发到 MQTT。"""

    def __init__(
        self,
        base_url: str,
        mqtt_host: str,
        mqtt_port: int = 1883,
        prefix: str = "mshouse",
        interval: float = 2.0,
    ) -> None:
        self.base_url = base_url
        self.prefix = prefix
        self.interval = interval
        self._stop = threading.Event()
        self._mqtt = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="mshouse-ha-camera"
        )
        self._mqtt.on_connect = self._on_connect
        self._mqtt.connect_async(mqtt_host, mqtt_port, keepalive=30)
        self._mqtt.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            LOG.error("MQTT 连接失败：%s", reason_code)
            return
        LOG.info("已连接 MQTT broker")

    def _publish_camera_config(self, dev_id: str, name: str) -> None:
        """发布 MQTT Discovery 的 camera 配置。"""
        topic = f"{self.prefix}/{dev_id}/snapshot"
        payload = {
            "name": name,
            "unique_id": f"mshouse_camera_{dev_id.replace('.', '_')}",
            "topic": topic,
            # HA 的 mqtt camera 收到 base64 时按这个标记解码
            "image_encoding": "b64",
            "device": {
                "identifiers": [f"mshouse_{dev_id.replace('.', '_')}"],
                "name": name,
                "manufacturer": "Mirror Space",
                "model": "MSHouse 虚拟设备",
            },
        }
        cfg_topic = f"homeassistant/camera/mshouse_camera_{dev_id.replace('.', '_')}/config"
        self._mqtt.publish(
            cfg_topic, mqtt.json.dumps(payload) if hasattr(mqtt, "json") else
            __import__("json").dumps(payload, ensure_ascii=False),
            qos=1, retain=True,
        )
        LOG.info("已发布 camera Discovery：%s", name)

    def _run_one(self, dev_id: str, name: str, channel: str) -> None:
        topic = f"{self.prefix}/{dev_id}/snapshot"
        self._publish_camera_config(dev_id, name)
        misses = 0
        while not self._stop.is_set():
            jpg = grab_jpeg(self.base_url, channel)
            if jpg:
                b64 = base64.b64encode(jpg).decode("ascii")
                # 快照不 retain：避免 broker 里堆历史帧，也避免 HA 重启后拿到过期画面
                self._mqtt.publish(topic, b64, qos=0, retain=False)
                misses = 0
            else:
                misses += 1
                if misses % 10 == 1:
                    LOG.warning("%s 连续取帧失败（%d 次）", name, misses)
            self._stop.wait(self.interval)

    def start(self) -> None:
        threads = []
        for dev_id, (name, channel) in CAMERAS.items():
            t = threading.Thread(
                target=self._run_one, args=(dev_id, name, channel),
                daemon=True, name=f"cam-{dev_id}",
            )
            t.start()
            threads.append(t)
        LOG.info("已启动 %d 路摄像头快照发布，间隔 %.1fs", len(threads), self.interval)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="MSHouse 摄像头快照 → MQTT")
    ap.add_argument("--base-url", default="http://127.0.0.1:8081")
    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--prefix", default="mshouse")
    ap.add_argument("--interval", type=float, default=2.0, help="抓帧间隔秒数")
    ap.add_argument("--channel", help="只抓这一路（调试用），如233666")
    ap.add_argument("--once", action="store_true", help="只抓一帧就退出")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.once:
        ch = args.channel or next(iter(CAMERAS.values()))[1]
        jpg = grab_jpeg(args.base_url, ch)
        if jpg:
            print(f"✅ 抓到 JPEG：{len(jpg)} 字节，magic={jpg[:3].hex()}")
            print(f"   base64 长度 {len(base64.b64encode(jpg))}")
            return 0
        print("❌ 抓帧失败")
        return 1

    pub = CameraPublisher(
        base_url=args.base_url,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        prefix=args.prefix,
        interval=args.interval,
    )
    pub.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        LOG.info("退出中……")
        pub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())