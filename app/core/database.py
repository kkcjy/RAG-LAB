# 数据库连接与会话管理
from __future__ import annotations

import logging
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.db import Base

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入用"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# 已发布版本新增的列。
# create_all 只建缺失的表、不会给已存在的表加列，所以新增列必须在这里补一次。
# 每条都必须是幂等的（IF NOT EXISTS）：老库启动时补齐，新库由 create_all
# 直接建好、此处是空操作。
_MIGRATIONS = (
    # 2026-10-17 token 版本号，用于改密码/改权限后立即吊销旧 JWT
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
)


async def init_db() -> None:
    """建表 + 补齐新增列。

    采用 create_all 直接建表（幂等，已存在则跳过）。
    requirements 里带了 alembic 但本项目暂未产出版本化迁移脚本，
    新列通过 _MIGRATIONS 显式补齐。
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for sql in _MIGRATIONS:
            await conn.execute(text(sql))
    logger.info("数据库表已就绪")


async def close_db() -> None:
    await engine.dispose()
