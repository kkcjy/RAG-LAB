# 启动引导：建默认管理员、初始化系统配置
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password
from app.models.db import User
from app.services.runtime_config import apply_runtime_overrides, seed_default_config

logger = logging.getLogger(__name__)


async def ensure_admin(db: AsyncSession) -> None:
    """确保至少存在一个管理员账户"""
    existing = (
        await db.execute(select(User).where(User.role == "admin"))
    ).scalars().first()

    if existing is not None:
        logger.info("管理员账户已存在: %s", existing.username)
        return

    admin = User(
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        email=settings.admin_email,
        role="admin",
        status="active",
        display_name="系统管理员",
    )
    db.add(admin)
    await db.commit()

    logger.warning("=" * 60)
    logger.warning("已创建默认管理员账户")
    logger.warning("  用户名: %s", settings.admin_username)
    logger.warning("  密码:   %s", settings.admin_password)
    logger.warning("  请登录后立即修改密码！")
    logger.warning("=" * 60)


async def bootstrap_all(db: AsyncSession) -> None:
    await ensure_admin(db)
    await seed_default_config(db)
    await apply_runtime_overrides(db)
