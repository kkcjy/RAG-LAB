# 审计日志工具
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import AuditLog

logger = logging.getLogger(__name__)


async def write_audit(
    db: AsyncSession,
    action: str,
    user_id: Optional[uuid.UUID] = None,
    username: Optional[str] = None,
    resource: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    ip: Optional[str] = None,
    commit: bool = True,
) -> None:
    try:
        db.add(
            AuditLog(
                user_id=user_id,
                username=username,
                action=action,
                resource=resource,
                detail=detail or {},
                ip_address=ip,
            )
        )
        if commit:
            await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("写审计日志失败: %s", e)
        await db.rollback()
