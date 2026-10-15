# 问答路由 —— RAG 主入口，支持 SSE 流式
from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_accessible_kb, get_current_user
from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.models.db import ChatMessage, ChatSession, KnowledgeBase, User
from app.schemas.api import AskRequest, AskResponse, MessageOut, SessionOut, SourceOut
from app.services.rag_engine import get_rag_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["问答"])


# ===========================================================================
#  非流式问答
# ===========================================================================
@router.post("/ask", response_model=AskResponse, summary="提问（非流式，返回完整回答）")
async def ask(
    payload: AskRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.kb_id is None:
        raise HTTPException(status_code=400, detail="请指定知识库 kb_id")

    kb = await _resolve_kb(payload.kb_id, user, db)

    session = await _get_or_create_session(db, user, kb, payload.session_id, payload.query)

    started = time.time()
    engine = get_rag_engine()
    try:
        answer_text, ctx = await engine.answer(payload.query, kb, enable_web=payload.enable_web)
    except RuntimeError as e:
        # LLM 未配置等可预期的运行时问题 → 503 并给出可操作提示
        logger.warning("问答不可用: %s", e)
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("问答失败")
        raise HTTPException(status_code=500, detail=f"问答失败: {e}") from e

    latency = (time.time() - started) * 1000

    # 落库
    await _save_exchange(db, session, payload.query, answer_text, ctx, latency)

    return AskResponse(
        session_id=session.id,
        answer=answer_text,
        sources=[SourceOut(**s) for s in ctx.sources()],
        used_web_search=ctx.used_web_search,
        kb_hit=ctx.kb_hit,
        rewritten_query=ctx.rewritten_query or None,
        latency_ms=round(latency, 2),
    )


# ===========================================================================
#  流式问答（SSE）
# ===========================================================================
@router.post("/stream", summary="提问（SSE 流式）")
async def ask_stream(
    payload: AskRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.kb_id is None:
        raise HTTPException(status_code=400, detail="请指定知识库 kb_id")

    kb = await _resolve_kb(payload.kb_id, user, db)
    session = await _get_or_create_session(db, user, kb, payload.session_id, payload.query)
    session_id = session.id
    user_id = user.id
    query = payload.query
    enable_web = payload.enable_web

    # 先记下用户消息
    db.add(ChatMessage(session_id=session_id, role="user", content=query))
    session.message_count = (session.message_count or 0) + 1
    await db.commit()

    async def event_gen():
        full_answer = []
        sources: list[dict] = []
        meta: dict = {}
        started = time.time()

        yield _sse({"type": "session", "data": {"session_id": str(session_id)}})

        engine = get_rag_engine()
        try:
            async for event in engine.stream_answer(query, kb, enable_web=enable_web):
                etype = event["type"]
                if etype == "delta":
                    full_answer.append(event["data"])
                elif etype == "sources":
                    sources = event["data"]
                elif etype == "done":
                    meta = event["data"]

                yield _sse(event)

                if etype == "error":
                    break
        except Exception as e:  # noqa: BLE001
            logger.exception("流式问答异常")
            yield _sse({"type": "error", "data": f"服务异常: {e}"})

        # 落库（用独立会话，避免请求会话已关闭）
        answer_text = "".join(full_answer)
        latency = (time.time() - started) * 1000
        try:
            async with AsyncSessionLocal() as s2:
                s2.add(
                    ChatMessage(
                        session_id=session_id,
                        role="assistant",
                        content=answer_text,
                        sources={"items": sources},
                        used_web_search=bool(meta.get("used_web_search", False)),
                    )
                )
                sess = await s2.get(ChatSession, session_id)
                if sess:
                    sess.message_count = (sess.message_count or 0) + 1
                    # 首轮回答后自动生成标题
                    if sess.title == "新会话":
                        sess.title = query[:40]
                await s2.commit()
        except Exception:  # noqa: BLE001
            logger.exception("保存问答消息失败")

        yield _sse({"type": "finish", "data": {"session_id": str(session_id), "latency_ms": round(latency, 2)}})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ===========================================================================
#  纯检索（不依赖 LLM，用于调试与检索质量评估）
# ===========================================================================
@router.post("/retrieve", summary="仅检索（不生成回答，无需 LLM）")
async def retrieve_only(
    payload: AskRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    只做向量检索并返回命中的片段与相似度，不调用 LLM。
    用途：调试检索质量、评估阈值设置、LLM 不可用时的降级检索。
    """
    if payload.kb_id is None:
        raise HTTPException(status_code=400, detail="请指定知识库 kb_id")

    kb = await _resolve_kb(payload.kb_id, user, db)
    engine = get_rag_engine()

    queries = await engine.rewrite_query(payload.query)
    chunks = await engine.retrieve_local(queries, kb)
    relevant, reason = await engine.judge_relevance(payload.query, chunks)

    return {
        "query": payload.query,
        "rewritten_queries": queries,
        "kb_hit": relevant,
        "judge_reason": reason,
        "hit_count": len(chunks),
        "results": [
            {
                "chunk_id": c.chunk_id,
                "filename": c.filename,
                "score": round(c.score, 4),
                "content": c.content[:500],
            }
            for c in chunks
        ],
    }


# ===========================================================================
#  会话管理
# ===========================================================================
@router.get("/sessions", summary="我的会话列表")
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ChatSession).where(ChatSession.user_id == user.id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(ChatSession.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return {
        "total": total,
        "items": [SessionOut.model_validate(s) for s in rows],
    }


@router.get("/sessions/{session_id}", summary="会话消息历史")
async def get_session(
    session_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")

    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at)
        )
    ).scalars().all()

    return {
        "session": SessionOut.model_validate(session),
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "sources": (m.sources or {}).get("items", []),
                "used_web_search": m.used_web_search,
                "created_at": m.created_at.isoformat(),
            }
            for m in rows
        ],
    }


@router.delete("/sessions/{session_id}", summary="删除会话")
async def delete_session(
    session_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")

    await db.delete(session)
    await db.commit()
    return {"message": "会话已删除"}


@router.delete("/sessions", summary="清空我的所有会话")
async def clear_sessions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(delete(ChatSession).where(ChatSession.user_id == user.id))
    await db.commit()
    return {"message": "已清空全部会话"}


# ===========================================================================
#  内部工具
# ===========================================================================
async def _resolve_kb(kb_id: uuid.UUID, user: User, db: AsyncSession) -> KnowledgeBase:
    from app.api.deps import get_accessible_kb as _gak

    return await _gak(kb_id, user, db)


async def _get_or_create_session(
    db: AsyncSession,
    user: User,
    kb: KnowledgeBase,
    session_id: uuid.UUID | None,
    first_query: str,
) -> ChatSession:
    if session_id:
        session = await db.get(ChatSession, session_id)
        if session is None or session.user_id != user.id:
            raise HTTPException(status_code=404, detail="会话不存在")
        return session

    session = ChatSession(
        user_id=user.id,
        kb_id=kb.id,
        title=first_query[:40] or "新会话",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _save_exchange(
    db: AsyncSession,
    session: ChatSession,
    query: str,
    answer: str,
    ctx,
    latency: float,
) -> None:
    db.add(ChatMessage(session_id=session.id, role="user", content=query))
    db.add(
        ChatMessage(
            session_id=session.id,
            role="assistant",
            content=answer,
            sources={"items": ctx.sources()},
            used_web_search=ctx.used_web_search,
            rewritten_query=ctx.rewritten_query or None,
        )
    )
    session.message_count = (session.message_count or 0) + 2
    if session.title == "新会话":
        session.title = query[:40]
    await db.commit()
