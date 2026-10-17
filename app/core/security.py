# 安全相关：密码哈希 + JWT
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:  # noqa: BLE001
        return False


def create_access_token(
    subject: str,
    role: str = "user",
    extra: Optional[dict[str, Any]] = None,
    expires_minutes: Optional[int] = None,
    token_version: int = 0,
) -> str:
    expire_minutes = expires_minutes or settings.access_token_expire_minutes
    now = dt.datetime.now(dt.timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        # token 版本号：用户改密码/改角色/被禁用时递增，
        # 使此前签发的 token 立即失效（见 deps.get_current_user）
        "tv": token_version,
        "iat": now,
        "exp": now + dt.timedelta(minutes=expire_minutes),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
