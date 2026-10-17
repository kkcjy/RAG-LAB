# 认证路由
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models.db import User
from app.schemas.api import (
    ChangePasswordRequest,
    LoginRequest,
    TokenResponse,
    UserOut,
)
from app.utils.audit import write_audit

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/login", response_model=TokenResponse, summary="用户登录")
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()

    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if user.status != "active":
        raise HTTPException(status_code=403, detail="账户已被禁用，请联系管理员")

    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(
        subject=str(user.id), role=user.role, token_version=user.token_version or 0
    )

    await write_audit(
        db,
        action="login",
        user_id=user.id,
        username=user.username,
        ip=client_ip(request),
    )

    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut, summary="获取当前用户信息")
async def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)


@router.post("/change-password", summary="修改自己的密码")
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码错误")

    user.password_hash = hash_password(payload.new_password)
    # 递增 token 版本，使改密前签发的 token 立即失效（否则旧 token 还能用到过期）
    user.token_version = (user.token_version or 0) + 1
    await db.commit()

    await write_audit(db, action="change_password", user_id=user.id, username=user.username)

    return {"message": "密码修改成功，请重新登录"}
