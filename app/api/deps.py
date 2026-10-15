# FastAPI 依赖：认证、权限
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.db import KBPermission, KnowledgeBase, User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="认证失败，请重新登录",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exc

    payload = decode_access_token(token)
    if not payload:
        raise credentials_exc

    user_id = payload.get("sub")
    if not user_id:
        raise credentials_exc

    try:
        uid = uuid.UUID(str(user_id))
    except ValueError:
        raise credentials_exc from None

    user = await db.get(User, uid)
    if user is None:
        raise credentials_exc
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账户已被禁用")

    return user


async def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


async def get_accessible_kb(
    kb_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBase:
    """校验用户对该知识库有读权限"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.status != "active":
        raise HTTPException(status_code=404, detail="知识库不存在")

    if user.role == "admin" or kb.is_public or kb.owner_id == user.id:
        return kb

    perm = (
        await db.execute(
            select(KBPermission).where(
                KBPermission.user_id == user.id,
                KBPermission.kb_id == kb.id,
                KBPermission.can_read.is_(True),
            )
        )
    ).scalar_one_or_none()

    if perm is None:
        raise HTTPException(status_code=403, detail="无权访问该知识库")

    return kb


async def get_writable_kb(
    kb_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBase:
    """校验用户对该知识库有写权限"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.status != "active":
        raise HTTPException(status_code=404, detail="知识库不存在")

    if user.role == "admin" or kb.owner_id == user.id:
        return kb

    perm = (
        await db.execute(
            select(KBPermission).where(
                KBPermission.user_id == user.id,
                KBPermission.kb_id == kb.id,
                KBPermission.can_write.is_(True),
            )
        )
    ).scalar_one_or_none()

    if perm is None:
        raise HTTPException(status_code=403, detail="无权修改该知识库")

    return kb


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""
