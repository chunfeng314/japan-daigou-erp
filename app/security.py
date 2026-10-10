"""口令哈希与会话令牌。

为什么不用 bcrypt：本项目 10 人规模、内网/私有部署，stdlib 的
PBKDF2-HMAC-SHA256（20 万次迭代）已足够，且**零第三方依赖**，
部署到东京那台机器时少一个编译型依赖就少一类坑。
将来要换 argon2/bcrypt，只需替换本文件的两个函数，格式里已带算法标识。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 200_000
SALT_BYTES = 16

_serializer: URLSafeTimedSerializer | None = None


# --------------------------------------------------------------------------- #
# 口令
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """返回 `pbkdf2_sha256$迭代次数$盐$哈希`。"""
    if not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_hex(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"{ALGORITHM}${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间比对，避免时序侧信道。"""
    if not password or not stored:
        return False
    try:
        algorithm, iterations, salt, expected = stored.split("$")
    except ValueError:
        return False
    if algorithm != ALGORITHM:
        return False
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


# --------------------------------------------------------------------------- #
# 会话（签名 cookie；无服务端会话表，10 人规模够用且重启不掉线）
# --------------------------------------------------------------------------- #
def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="erp-session-v1")
    return _serializer


def reset_serializer() -> None:
    """仅供测试：配置变更后重建。"""
    global _serializer
    _serializer = None


def create_session_token(user_id: int) -> str:
    return _get_serializer().dumps({"uid": user_id})


def read_session_token(token: str) -> int | None:
    try:
        data = _get_serializer().loads(token, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return int(uid) if isinstance(uid, int) else None
