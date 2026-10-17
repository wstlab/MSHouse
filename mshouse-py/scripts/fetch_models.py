"""
下载大门锁人脸识别用的 OpenCV Zoo 模型
======================================

仓库里已经内置了这两个文件（models/ 目录），正常情况下**不需要运行本脚本**。
只有在下面两种情况才用得上：

  * 你把 models/ 删掉了 / 换了一台干净的机器，想重新拉一份；
  * 想换成 FP32 原版 SFace（体积大 3 倍多，精度提升很小）。

用法：
    python scripts/fetch_models.py            # 只补齐缺失的模型
    python scripts/fetch_models.py --force    # 全部重新下载
    python scripts/fetch_models.py --fp32     # 额外下载 FP32 版 SFace

模型来源（OpenCV 官方 opencv_zoo 仓库）：
    https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
    https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface

许可：两者均随 OpenCV Zoo 以 Apache-2.0 发布，可随本项目一起分发。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"

BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"

# 文件名 → (下载地址, 字节数, 说明)
MODELS: dict[str, tuple[str, int, str]] = {
    "face_detection_yunet_2023mar.onnx": (
        f"{BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        232589,
        "YuNet 人脸检测（同时给出 5 点关键点，供 SFace 做对齐）",
    ),
    "face_recognition_sface_2021dec_int8bq.onnx": (
        f"{BASE}/face_recognition_sface/face_recognition_sface_2021dec_int8bq.onnx",
        10667852,
        "SFace 人脸识别 · INT8 块量化版（默认，体积小、精度几乎无损）",
    ),
    "face_recognition_sface_2021dec.onnx": (
        f"{BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        0,  # 大小随版本变化，只校验非空
        "SFace 人脸识别 · FP32 原版（可选，--fp32 时才下载）",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def download(name: str, url: str, expect_size: int, force: bool) -> bool:
    target = MODELS_DIR / name
    if target.exists() and not force:
        if expect_size and target.stat().st_size != expect_size:
            print(f"  ! {name} 已存在但大小不符（{target.stat().st_size} ≠ {expect_size}），重新下载")
        else:
            print(f"  = {name} 已存在，跳过")
            return True
    print(f"  ↓ {name}")
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as out:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
    except Exception as err:
        tmp.unlink(missing_ok=True)
        print(f"  ✗ 下载失败：{err}")
        return False

    size = tmp.stat().st_size
    if expect_size and size != expect_size:
        tmp.unlink(missing_ok=True)
        print(f"  ✗ 大小不符（拿到 {size}，期望 {expect_size}），已丢弃")
        return False
    if size < 1024:
        tmp.unlink(missing_ok=True)
        print(f"  ✗ 文件过小（{size} 字节），已丢弃")
        return False

    tmp.replace(target)
    print(f"  ✓ {name}（{size / 1024 / 1024:.2f} MB，sha256 {sha256(target)}…）")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 MSHouse 人脸识别模型")
    parser.add_argument("--force", action="store_true", help="即使已存在也重新下载")
    parser.add_argument("--fp32", action="store_true", help="同时下载 FP32 版 SFace")
    args = parser.parse_args()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"模型目录：{MODELS_DIR}")

    wanted = [
        name for name in MODELS
        if args.fp32 or not name.endswith("_2021dec.onnx")
    ]
    ok = True
    for name in wanted:
        url, size, note = MODELS[name]
        print(f"\n{note}")
        ok = download(name, url, size, args.force) and ok

    print()
    if ok:
        print("完成。重启网关即可生效：python -m server.main")
        return 0
    print("部分模型下载失败 —— 人脸识别会退回 Haar + 手工特征的旧方案（精度较差）。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
