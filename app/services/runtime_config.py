# 运行时配置覆盖
# admin 后台修改的配置存在 system_config 表，启动和更新时覆盖到 settings 对象上，
# 使阈值/开关调整无需重启容器即可生效。
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)

# 允许在后台动态调整的配置项（key → (settings 属性名, 类型)）
OVERRIDABLE: dict[str, tuple[str, type]] = {
    "retrieval_score_threshold": ("retrieval_score_threshold", float),
    "retrieval_top_k": ("retrieval_top_k", int),
    "retrieval_llm_judge": ("retrieval_llm_judge", bool),
    "query_rewrite_enabled": ("query_rewrite_enabled", bool),
    "web_search_auto_fallback": ("web_search_auto_fallback", bool),
    "chunk_size": ("chunk_size", int),
    "chunk_overlap": ("chunk_overlap", int),
    "tavily_max_results": ("tavily_max_results", int),
    "llm_temperature": ("llm_temperature", float),
    "llm_max_tokens": ("llm_max_tokens", int),
    "max_upload_size_mb": ("max_upload_size_mb", int),
}


def _cast(value: str, typ: type):
    if typ is bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}
    if typ is int:
        return int(float(value))
    if typ is float:
        return float(value)
    return value


async def seed_default_config(db: AsyncSession) -> None:
    """首次启动时把当前 settings 值写入 system_config，作为可视化的默认值"""
    from app.models.db import SystemConfig

    existing = (
        await db.execute(select(SystemConfig.key))
    ).scalars().all()
    existing_set = set(existing)

    added = 0
    descriptions = {
        "retrieval_score_threshold": "检索相似度阈值，低于此值判定知识库未命中",
        "retrieval_top_k": "向量检索返回的片段数量",
        "retrieval_llm_judge": "是否启用 LLM 精判相关性",
        "query_rewrite_enabled": "是否启用查询改写",
        "web_search_auto_fallback": "知识库未命中时是否自动联网搜索",
        "chunk_size": "文档分块大小（字符）",
        "chunk_overlap": "分块重叠字符数",
        "tavily_max_results": "联网搜索返回结果数",
        "llm_temperature": "LLM 温度参数",
        "llm_max_tokens": "LLM 最大输出 token",
        "max_upload_size_mb": "单文件上传大小上限（MB）",
    }

    for key, (attr, _typ) in OVERRIDABLE.items():
        if key in existing_set:
            continue
        value = getattr(settings, attr, None)
        if value is None:
            continue
        value_type = "bool" if isinstance(value, bool) else (
            "int" if isinstance(value, int) else "float" if isinstance(value, float) else "str"
        )
        db.add(
            SystemConfig(
                key=key,
                value=str(value),
                value_type=value_type,
                description=descriptions.get(key),
            )
        )
        added += 1

    if added:
        await db.commit()
        logger.info("已写入 %d 条默认系统配置", added)


async def apply_runtime_overrides(db: AsyncSession) -> int:
    """把数据库里的配置覆盖到 settings 对象"""
    from app.models.db import SystemConfig

    rows = (await db.execute(select(SystemConfig))).scalars().all()
    count = 0
    for row in rows:
        spec = OVERRIDABLE.get(row.key)
        if spec is None:
            continue
        attr, typ = spec
        try:
            setattr(settings, attr, _cast(row.value, typ))
            count += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("应用配置 %s=%s 失败: %s", row.key, row.value, e)
    logger.info("已应用 %d 条运行时配置", count)
    return count
