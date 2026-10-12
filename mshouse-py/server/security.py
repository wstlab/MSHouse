"""
鉴权工具（v2.0 新增）

为什么单独成模块：
早期版本把口令 `"villa"` 明文写在 model.py 里，并被 export_model.py
导出到前端资源 shared/model.js —— 任何打开网页的人按 F12 就能看到口令。
而且 /ws 完全不校验凭据，能连上就能开门锁。

本模块集中处理三件事：
  1. 口令哈希（不存明文，用 PBKDF2-SHA256）
  2. 令牌比较（常数时间，避免时序侧信道）
  3. 视频流签名 URL（让 <img src> 这种没法带 header 的场景也能鉴权）

常数时间比较的必要性：普通的 `a == b` 会在第一个不同的字符处提前返回，
攻击者可以通过测量响应时间逐字符猜出令牌。hmac.compare_digest 不受此影响。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from . import model as M


# ------------------------------------------------------------------ #
# 口令哈希
# ------------------------------------------------------------------ #

def hash_password(password: str, *, iterations: int = M.SETUP_PASSWORD_ITERATIONS) -> str:
    """
    生成口令哈希，格式：pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>

    自带算法名与迭代次数，便于日后提高迭代次数时仍能校验老哈希
    （校验时按存储里记录的参数算，而不是按当前默认值）。
    """
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join((
        M.SETUP_PASSWORD_ALGO,
        str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ))


def verify_password(password: str, stored: str) -> bool:
    """校验口令。stored 为空或格式非法时一律返回 False（失败关闭）。"""
    if not stored:
        return False
    try:
        algo, iterations_text, salt_b64, hash_b64 = stored.split("$")
        if algo != M.SETUP_PASSWORD_ALGO:
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def generate_password(length: int = 16) -> str:
    """生成一次性随机口令（首次启动时打印到日志，由使用者保存）。"""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_token(nbytes: int = 32) -> str:
    """生成访问令牌。用 secrets 而非 random —— 后者是可预测的伪随机。"""
    return secrets.token_urlsafe(nbytes)


# ------------------------------------------------------------------ #
# 比较
# ------------------------------------------------------------------ #

def equals(a: str, b: str) -> bool:
    """常数时间字符串比较。任一侧为空都直接判否。"""
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ------------------------------------------------------------------ #
# 视频流签名 URL
#
# 浏览器的 <img src="..."> 没法自定义请求头，所以流通道不能只靠
# Authorization 头鉴权。这里给出签名 URL 方案：
#   /?stream=233667&exp=<过期时间戳>&sig=<HMAC>
# 签名覆盖通道名与过期时间，改任何一项签名都会失效。
# 有效期短（默认 300 秒）+ 绑定通道，即使 URL 泄漏影响也有限。
# ------------------------------------------------------------------ #

def sign_stream(channel: str, expires_at: int, secret: str) -> str:
    """为「通道 + 过期时间」生成签名。"""
    message = f"{channel}:{expires_at}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_stream(channel: str, expires_at: int, sig: str, secret: str) -> tuple[bool, str]:
    """
    校验流签名。返回 (是否通过, 失败原因)。

    失败原因分三种，便于排查是「签名不对」还是「链接过期」——
    真实系统的鉴权日志也需要区分这两者。
    """
    if not sig:
        return False, "missing_signature"
    if expires_at <= 0:
        return False, "missing_expiry"
    if time.time() > expires_at:
        return False, "expired"
    expected = sign_stream(channel, expires_at, secret)
    if not hmac.compare_digest(expected, sig):
        return False, "bad_signature"
    return True, ""
