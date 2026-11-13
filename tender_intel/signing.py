"""请求签名：HMAC-SHA256(method\\npath\\ntimestamp\\nnonce)。

mock 站和爬虫共用同一套算法，secret 按站点配置。
"""
from __future__ import annotations

import hashlib
import hmac
import time


def build_sign(secret: str, method: str, path: str, timestamp: str, nonce: str) -> str:
    payload = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_headers(secret: str, method: str, path: str, nonce: str,
                 ts: float | None = None) -> dict[str, str]:
    ts = time.time() if ts is None else ts
    ts_str = str(int(ts))
    return {
        "X-Timestamp": ts_str,
        "X-Nonce": nonce,
        "X-Sign": build_sign(secret, method, path, ts_str, nonce),
    }


def verify_sign(secret: str, method: str, path: str, timestamp: str, nonce: str,
                signature: str, now: float | None = None,
                window: int = 300) -> tuple[bool, str]:
    now = time.time() if now is None else now
    try:
        if abs(now - int(timestamp)) > window:
            return False, "timestamp expired"
    except (TypeError, ValueError):
        return False, "bad timestamp"
    expected = build_sign(secret, method, path, timestamp, nonce)
    if not hmac.compare_digest(expected, signature or ""):
        return False, "bad signature"
    return True, "ok"
