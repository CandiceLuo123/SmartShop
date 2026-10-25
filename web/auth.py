# -*- coding: utf-8 -*-
"""
认证：PBKDF2 密码哈希 + HMAC 签名 Token（无第三方依赖）。
Token 形如 base64url(payload).hex(hmac)，有效期 7 天。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_FILE = ROOT / ".web_secret"
TOKEN_TTL = 7 * 24 * 3600


def _load_secret() -> bytes:
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes().strip()
    secret = secrets.token_hex(32).encode()
    SECRET_FILE.write_bytes(secret)
    return secret


SECRET = _load_secret()


def new_salt() -> str:
    return secrets.token_hex(16)


def hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode(), 120_000)
    return digest.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password, salt), expected_hash)


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def make_token(user_id: int) -> str:
    payload = _b64e(json.dumps(
        {"uid": user_id, "exp": int(time.time()) + TOKEN_TTL}).encode())
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str):
    """返回 user_id；无效/过期返回 None。"""
    try:
        payload, sig = token.split(".", 1)
        expect = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        data = json.loads(_b64d(payload))
        if data.get("exp", 0) < time.time():
            return None
        return int(data["uid"])
    except Exception:  # noqa: BLE001 —— 任何畸形 token 一律拒绝
        return None
