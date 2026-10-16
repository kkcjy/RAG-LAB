# 健康检查与系统信息
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.core.config import settings
from app.providers.embedding.base import get_embedding
from app.providers.llm.base import get_llm
from app.providers.vector.base import get_vector_store
from app.providers.websearch.base import get_web_search
from app.schemas.api import HealthOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["系统"])

VERSION = "0.1.0"


@router.get("/health", response_model=HealthOut, summary="健康检查")
async def health():
    """
    健康检查。任一组件不可用时降级返回，而不是抛异常 —— 
    否则未配置 API key 会导致整个服务被判为不可用。

    返回值中的 warnings 会列出配置层面的问题（如"开了联网兜底但没配 key"），
    web_search_enabled 表示联网搜索是否真正可用（而非仅声明了 provider）。
    """
    llm_provider = settings.llm_provider
    llm_model = ""
    llm_ok = False
    try:
        llm = get_llm()
        llm_model = llm.model
        llm_ok = await llm.health_check()
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 不可用: %s", e)
        llm_model = f"未就绪: {e}"

    embedder = get_embedding()
    try:
        dim = embedder.dimension
    except Exception as e:  # noqa: BLE001
        logger.warning("Embedding 不可用: %s", e)
        dim = 0

    try:
        store_ok = await get_vector_store().health_check()
    except Exception:  # noqa: BLE001
        store_ok = False

    try:
        ws = get_web_search()
        web_name = ws.display_name
        web_enabled = ws.enabled
    except Exception:  # noqa: BLE001
        web_name = "unavailable"
        web_enabled = False

    # 收集配置告警，让调用方一眼看出"看着能用其实没配"
    warnings: list[str] = []
    if not web_enabled and settings.web_search_auto_fallback:
        warnings.append(
            f"联网搜索已启用兜底(WEB_SEARCH_AUTO_FALLBACK=true)但 provider 不可用"
            f"（{settings.web_search_provider}"
            + ("，未配置 TAVILY_API_KEY" if settings.web_search_provider == "tavily" else "")
            + "），知识库未命中时无法联网补充"
        )
    if not llm_ok:
        warnings.append("LLM 未就绪，无法生成回答（检索功能仍可用）")

    # 向量库是问答的硬依赖；LLM 未配置时仍可做纯检索
    status = "ok" if (llm_ok and store_ok) else ("degraded" if store_ok else "error")

    return HealthOut(
        status=status,
        version=VERSION,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_ok=llm_ok,
        embedding_provider=settings.embedding_provider,
        embedding_dim=dim,
        vector_store_ok=store_ok,
        web_search_provider=web_name,
        web_search_enabled=web_enabled,
        warnings=warnings,
    )


@router.get("/info", summary="当前生效的核心配置（脱敏）")
async def info():
    try:
        llm_model = get_llm().model
    except Exception:  # noqa: BLE001
        llm_model = "未配置"

    return {
        "app": {"name": settings.app_name, "env": settings.app_env, "version": VERSION},
        "llm": {
            "provider": settings.llm_provider,
            "model": llm_model,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
        },
        "embedding": {
            "provider": settings.embedding_provider,
            "model": settings.bge_model_path if settings.embedding_provider == "local_bge"
            else settings.embedding_api_model,
            "device": settings.bge_device,
        },
        "retrieval": {
            "score_threshold": settings.retrieval_score_threshold,
            "top_k": settings.retrieval_top_k,
            "llm_judge": settings.retrieval_llm_judge,
            "query_rewrite": settings.query_rewrite_enabled,
            "web_fallback": settings.web_search_auto_fallback,
        },
        "web_search": {"provider": settings.web_search_provider},
        "chunking": {"size": settings.chunk_size, "overlap": settings.chunk_overlap},
        "ocr": {
            "enabled": settings.ocr_enabled,
            "max_pages": settings.ocr_max_pages,
            "dpi": settings.ocr_dpi,
        },
    }
