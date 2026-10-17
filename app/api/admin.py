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

    # 过滤掉显式为 null 的字段：status/role 是 NOT NULL 列，直接 setattr(None)
    # 会触发 IntegrityError；同时也会绕过上面针对自己的降级/禁用保护。
    changes = {
        k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None
    }
    if not changes:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")

    for field, value in changes.items():
        setattr(user, field, value)

    # 角色或状态变更时递增 token 版本，使该用户手里的旧 token 立即按新权限失效
    if {"role", "status"} & changes.keys():
        user.token_version = (user.token_version or 0) + 1

    await db.commit()
    await db.refresh(user)

    await write_audit(
        db,
        action="update_user",
        user_id=admin.id,
        username=admin.username,
        resource=user.username,
        detail=changes,
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
    # 递增 token 版本：重置密码的常见场景就是账号被盗，
    # 必须让攻击者手里的旧 token 立刻失效，而不只是等它自然过期
    user.token_version = (user.token_version or 0) + 1
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

    # 该用户名下的知识库移交给执行删除的管理员。
    # 若不处理，owner_id 会被置 NULL（外键 SET NULL），
    # 知识库变成"无主库"——普通用户失去入口且无人能管理。
    owned_kbs = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.owner_id == user.id))
    ).scalars().all()
    for kb in owned_kbs:
        kb.owner_id = admin.id

    await db.delete(user)
    await db.commit()

    await write_audit(
        db,
        action="delete_user",
        user_id=admin.id,
        username=admin.username,
        resource=username,
        detail={"transferred_kbs": [kb.name for kb in owned_kbs]},
        ip=client_ip(request),
    )

    msg = f"用户 {username} 已删除"
    if owned_kbs:
        msg += f"，其名下 {len(owned_kbs)} 个知识库已移交给 {admin.username}"
    return {"message": msg, "transferred_kbs": len(owned_kbs)}


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
