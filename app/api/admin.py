# 管理端路由：用户管理 + 系统配置 + 审计日志
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, get_current_admin
from app.core.database import get_db
from app.core.security import hash_password
from app.models.db import AuditLog, KBPermission, KnowledgeBase, User
from app.schemas.api import (
    ConfigItem,
    ConfigUpdate,
    ResetPasswordRequest,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.utils.audit import write_audit

router = APIRouter(prefix="/api/admin", tags=["管理端"], dependencies=[Depends(get_current_admin)])


# ===========================================================================
#  用户管理
# ===========================================================================
@router.get("/users", summary="用户列表（分页 + 搜索）")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    keyword: str | None = Query(None, description="按用户名/邮箱模糊搜索"),
    role: str | None = Query(None),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.email.ilike(like)))
    if role:
        stmt = stmt.where(User.role == role)
    if status:
        stmt = stmt.where(User.status == status)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(User.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [UserOut.model_validate(u) for u in rows],
    }


@router.post("/users", response_model=UserOut, summary="新增用户")
async def create_user(
    payload: UserCreate,
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    exists = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        email=payload.email,
        display_name=payload.display_name,
        role=payload.role,
        status="active",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    await write_audit(
        db,
        action="create_user",
        user_id=admin.id,
        username=admin.username,
        resource=user.username,
        detail={"role": user.role},
        ip=client_ip(request),
    )
    return UserOut.model_validate(user)


@router.get("/users/{user_id}", response_model=UserOut, summary="用户详情")
async def get_user(user_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserOut, summary="修改用户")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 防止管理员把自己的权限降级或禁用，导致锁死
    if user.id == admin.id:
        if payload.role is not None and payload.role != "admin":
            raise HTTPException(status_code=400, detail="不能修改自己的管理员角色")
        if payload.status is not None and payload.status != "active":
            raise HTTPException(status_code=400, detail="不能禁用自己的账户")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)

    await db.commit()
    await db.refresh(user)

    await write_audit(
        db,
        action="update_user",
        user_id=admin.id,
        username=admin.username,
        resource=user.username,
        detail=payload.model_dump(exclude_unset=True),
        ip=client_ip(request),
    )
    return UserOut.model_validate(user)


@router.post("/users/{user_id}/reset-password", summary="重置用户密码")
async def reset_password(
    user_id: uuid.UUID,
    payload: ResetPasswordRequest,
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    user.password_hash = hash_password(payload.new_password)
    await db.commit()

    await write_audit(
        db,
        action="reset_password",
        user_id=admin.id,
        username=admin.username,
        resource=user.username,
        ip=client_ip(request),
    )
    return {"message": f"已重置 {user.username} 的密码"}


@router.delete("/users/{user_id}", summary="删除用户")
async def delete_user(
    user_id: uuid.UUID,
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="不能删除自己的账户")

    username = user.username
    await db.delete(user)
    await db.commit()

    await write_audit(
        db,
        action="delete_user",
        user_id=admin.id,
        username=admin.username,
        resource=username,
        ip=client_ip(request),
    )
    return {"message": f"用户 {username} 已删除"}


# ===========================================================================
#  系统配置
# ===========================================================================
@router.get("/config", summary="读取系统配置")
async def get_config(db: AsyncSession = Depends(get_db)):
    from app.models.db import SystemConfig

    rows = (await db.execute(select(SystemConfig).order_by(SystemConfig.key))).scalars().all()
    return {
        "items": [
            ConfigItem(
                key=r.key, value=r.value, value_type=r.value_type, description=r.description
            )
            for r in rows
        ]
    }


@router.put("/config", summary="更新系统配置")
async def update_config(
    payload: ConfigUpdate,
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.models.db import SystemConfig

    updated = []
    for key, value in payload.items.items():
        row = await db.get(SystemConfig, key)
        if row is None:
            db.add(SystemConfig(key=key, value=str(value), value_type="str"))
        else:
            row.value = str(value)
        updated.append(key)

    await db.commit()

    # 让运行时配置立即生效
    from app.services.runtime_config import apply_runtime_overrides

    await apply_runtime_overrides(db)

    await write_audit(
        db,
        action="update_config",
        user_id=admin.id,
        username=admin.username,
        detail=payload.items,
        ip=client_ip(request),
    )
    return {"message": "配置已更新", "updated": updated}


# ===========================================================================
#  审计日志
# ===========================================================================
@router.get("/audit-logs", summary="审计日志")
async def audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    action: str | None = Query(None),
    username: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if username:
        stmt = stmt.where(AuditLog.username == username)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(AuditLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": str(r.id),
                "username": r.username,
                "action": r.action,
                "resource": r.resource,
                "detail": r.detail,
                "ip_address": r.ip_address,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.get("/stats", summary="系统总览统计")
async def stats(db: AsyncSession = Depends(get_db)):
    from app.models.db import ChatMessage, ChatSession, Chunk, Document, KnowledgeBase

    user_count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    active_users = (
        await db.execute(select(func.count()).select_from(User).where(User.status == "active"))
    ).scalar_one()
    kb_count = (await db.execute(select(func.count()).select_from(KnowledgeBase))).scalar_one()
    doc_count = (await db.execute(select(func.count()).select_from(Document))).scalar_one()
    chunk_count = (await db.execute(select(func.count()).select_from(Chunk))).scalar_one()
    session_count = (await db.execute(select(func.count()).select_from(ChatSession))).scalar_one()
    msg_count = (await db.execute(select(func.count()).select_from(ChatMessage))).scalar_one()
    web_used = (
        await db.execute(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.used_web_search.is_(True))
        )
    ).scalar_one()

    return {
        "users": {"total": user_count, "active": active_users},
        "knowledge_bases": kb_count,
        "documents": doc_count,
        "chunks": chunk_count,
        "chat_sessions": session_count,
        "chat_messages": msg_count,
        "web_search_used": web_used,
    }
