# RAG 核心引擎 —— 路线 C 混合式
#
#  用户提问
#    → [1] Query 改写 (LLM)
#    → [2] 本地向量检索 (Qdrant)
#    → [3] 相关度判定：阈值粗筛 + LLM 精判
#          ├─ 够 → 直接 RAG 回答
#          └─ 不够 → Tavily 联网 → 综合回答
#    → [4] 流式生成 + 引用标注
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import KnowledgeBase
from app.providers.embedding.base import get_embedding
from app.providers.llm.base import BaseLLM, ChatMessage, get_llm, get_llm
from app.providers.vector.base import SearchHit, get_vector_store
from app.providers.websearch.base import SearchResult, get_web_search

logger = logging.getLogger(__name__)


# ===========================================================================
#  数据结构
# ===========================================================================
@dataclass
class RetrievedChunk:
    content: str
    score: float
    doc_id: str = ""
    filename: str = ""
    chunk_id: str = ""
    chunk_index: int = 0

    def to_source(self) -> dict:
        return {
            "type": "kb",
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "filename": self.filename,
            "score": round(self.score, 4),
            "snippet": self.content[:200],
        }


@dataclass
class WebResult:
    title: str
    url: str
    content: str

    def to_source(self) -> dict:
        return {
            "type": "web",
            "title": self.title,
            "url": self.url,
            "snippet": self.content[:200],
        }


@dataclass
class RAGContext:
    """一次问答的完整上下文，便于调试与审计"""

    original_query: str
    rewritten_query: str = ""
    chunks: list[RetrievedChunk] = field(default_factory=list)
    web_results: list[WebResult] = field(default_factory=list)
    used_web_search: bool = False
    kb_hit: bool = False
    judge_reason: str = ""
    kb_hit_debug: bool = False
    rewritten_queries: list[str] = field(default_factory=list)

    def sources(self) -> list[dict]:
        out = [c.to_source() for c in self.chunks]
        out.extend(w.to_source() for w in self.web_results)
        return out


# ===========================================================================
#  提示词
# ===========================================================================
REWRITE_PROMPT = """你是一个查询改写助手。请将用户的问题改写为更适合知识库检索的形式。

要求：
1. 补全指代（如"它"、"这个"替换为明确主语）
2. 去除口语化表达，保留核心检索意图
3. 如果问题包含多个子问题，用换行分隔成多个独立查询
4. 只输出改写后的查询文本，不要任何解释、编号或前后缀
5. 如果问题本身已经很清晰，原样输出

用户问题：{query}

改写后的查询："""

JUDGE_PROMPT = """你是一个检索结果相关性判断助手。

用户问题：{query}

知识库检索到的内容片段：
{context}

请判断：以上片段是否包含足以回答用户问题的信息？

只输出一个 JSON，格式为：
{{"relevant": true/false, "reason": "简短理由"}}

判定标准：
- 片段包含回答问题所需的事实信息 → true
- 片段只是主题相近但无法回答问题 → false
- 片段为空或完全无关 → false"""

RAG_PROMPT = """你是一个严谨的企业知识库问答助手。请基于下面提供的资料回答用户问题。

【知识库资料】
{kb_context}

{web_block}

【回答要求】
1. 优先使用知识库资料回答，这是最权威的依据
2. 如果知识库资料不足但联网资料有帮助，可以结合使用，但要说明信息来自网络
3. 引用资料时用 [1] [2] 这样的编号标注来源
4. 如果资料完全没有相关信息，明确说明"根据现有资料无法回答该问题"，不要编造
5. 回答要条理清晰，使用中文
6. 不要提及"资料片段""上下文"等技术性词汇，自然地组织回答

用户问题：{query}

回答："""

WEB_BLOCK_TEMPLATE = """
【联网搜索结果】
{web_context}
"""


# ===========================================================================
#  引擎
# ===========================================================================
class RAGEngine:
    """
    RAG 引擎。

    LLM 采用延迟初始化：检索链路（向量化、检索、阈值判定）不依赖 LLM，
    因此即使 LLM 未配置，检索功能仍然可用。只有真正需要生成/改写/精判时
    才会构造 LLM 实例，并在那时给出明确错误。
    """

    def __init__(self, llm: Optional[BaseLLM] = None):
        self._llm = llm
        self.embedder = get_embedding()
        self.store = get_vector_store()
        self.web_search = get_web_search()

    @property
    def llm(self) -> BaseLLM:
        """按需构造 LLM；未配置时抛出带说明的错误"""
        if self._llm is None:
            try:
                self._llm = get_llm()
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    f"LLM 未就绪，无法生成回答：{e}。"
                    f"请在 .env 中配置 API key（当前 LLM_PROVIDER={settings.llm_provider}），"
                    f"或仅使用检索功能。"
                ) from e
        return self._llm

    @property
    def llm_ready(self) -> bool:
        """LLM 是否可用（不抛异常，供上层判断降级）"""
        try:
            _ = self.llm
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # [1] Query 改写
    # ------------------------------------------------------------------
    async def rewrite_query(self, query: str) -> list[str]:
        if not settings.query_rewrite_enabled:
            return [query]

        if not self.llm_ready:
            return [query]

        try:
            resp = await self.llm.chat(
                [ChatMessage(role="user", content=REWRITE_PROMPT.format(query=query))],
                temperature=0.0,
                max_tokens=256,
            )
            text = (resp.content or "").strip()
            if not text:
                return [query]

            # 清理可能的前缀
            text = re.sub(r"^(改写后的?查询[:：]?|查询[:：]?)\s*", "", text, flags=re.MULTILINE)
            queries = [q.strip(" -•\t") for q in text.split("\n") if q.strip(" -•\t")]

            # 去重且保序，限定最多 3 条
            seen = set()
            uniq = []
            for q in queries:
                if q not in seen and len(q) > 1:
                    seen.add(q)
                    uniq.append(q)

            result = uniq[:3] or [query]
            if result != [query]:
                logger.debug("Query 改写: %r → %r", query, result)
            return result

        except Exception as e:  # noqa: BLE001
            logger.warning("Query 改写失败，回退原问题: %s", e)
            return [query]

    # ------------------------------------------------------------------
    # [2] 本地检索
    # ------------------------------------------------------------------
    async def retrieve_local(
        self,
        queries: list[str],
        kb: KnowledgeBase,
        top_k: Optional[int] = None,
    ) -> list[RetrievedChunk]:
        top_k = top_k or settings.retrieval_top_k
        merged: dict[str, RetrievedChunk] = {}

        for q in queries:
            try:
                vec = await self.embedder.embed_query(q)
            except Exception as e:  # noqa: BLE001
                logger.error("查询向量化失败: %s", e)
                continue

            hits: list[SearchHit] = await self.store.search(
                collection=kb.collection_name,
                vector=vec,
                top_k=top_k,
                filters={"kb_id": str(kb.id)},
            )

            for h in hits:
                cid = h.id
                if cid not in merged or h.score > merged[cid].score:
                    merged[cid] = RetrievedChunk(
                        content=h.content,
                        score=h.score,
                        doc_id=h.payload.get("doc_id", ""),
                        filename=h.payload.get("filename", ""),
                        chunk_id=h.id,
                        chunk_index=h.payload.get("chunk_index", 0),
                    )

        # 按分数排序，取前 top_k
        ordered = sorted(merged.values(), key=lambda c: c.score, reverse=True)
        return ordered[:top_k]

    # ------------------------------------------------------------------
    # [3] 相关度判定
    # ------------------------------------------------------------------
    async def judge_relevance(
        self,
        query: str,
        chunks: list[RetrievedChunk],
    ) -> tuple[bool, str]:
        # 3.1 阈值粗筛
        if not chunks:
            return False, "知识库无检索结果"

        best = chunks[0].score
        threshold = settings.retrieval_score_threshold

        if best < threshold:
            return False, f"最高相似度 {best:.3f} 低于阈值 {threshold}"

        # 3.2 LLM 精判（可开关）
        if not settings.retrieval_llm_judge:
            return True, f"相似度 {best:.3f} 达到阈值（未启用 LLM 精判）"

        # LLM 不可用时退回阈值判定，保证检索链路仍可用
        if not self.llm_ready:
            return True, f"相似度 {best:.3f} 达到阈值（LLM 未就绪，跳过精判）"

        context = "\n\n".join(
            f"[{i + 1}] {c.content[:600]}" for i, c in enumerate(chunks[:5])
        )
        try:
            resp = await self.llm.chat(
                [
                    ChatMessage(
                        role="user",
                        content=JUDGE_PROMPT.format(query=query, context=context),
                    )
                ],
                temperature=0.0,
                max_tokens=128,
            )
            text = (resp.content or "").strip()
            # 容错解析 JSON
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                relevant = bool(data.get("relevant", False))
                reason = str(data.get("reason", ""))[:200]
                return relevant, reason or ("LLM 判定相关" if relevant else "LLM 判定不相关")
            # 解析失败，保守放行（已有阈值兜底）
            return True, "LLM 精判解析失败，按阈值判定为相关"
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM 精判失败，回退阈值判定: %s", e)
            return True, f"LLM 精判异常({e})，按阈值判定为相关"

    # ------------------------------------------------------------------
    # [3.5] 联网搜索
    # ------------------------------------------------------------------
    async def search_web(self, query: str) -> list[WebResult]:
        results: list[SearchResult] = await self.web_search.search(
            query, max_results=settings.tavily_max_results
        )
        return [WebResult(title=r.title, url=r.url, content=r.content) for r in results]

    # ------------------------------------------------------------------
    # 构建提示词
    # ------------------------------------------------------------------
    def _build_messages(self, ctx: RAGContext) -> list[ChatMessage]:
        kb_parts = []
        for i, c in enumerate(ctx.chunks):
            src = f"《{c.filename}》" if c.filename else "知识库"
            kb_parts.append(f"[{i + 1}] （来源：{src}）\n{c.content}")
        kb_context = "\n\n".join(kb_parts) if kb_parts else "（知识库中未找到相关内容）"

        web_block = ""
        if ctx.web_results:
            web_parts = []
            for i, w in enumerate(ctx.web_results):
                idx = len(ctx.chunks) + i + 1
                web_parts.append(f"[{idx}] {w.title}\n来源: {w.url}\n{w.content}")
            web_block = WEB_BLOCK_TEMPLATE.format(web_context="\n\n".join(web_parts))

        prompt = RAG_PROMPT.format(
            kb_context=kb_context,
            web_block=web_block,
            query=ctx.original_query,
        )
        return [
            ChatMessage(
                role="system",
                content="你是一个严谨的企业知识库问答助手，严格基于给定资料回答，不编造信息。",
            ),
            ChatMessage(role="user", content=prompt),
        ]

    # ------------------------------------------------------------------
    # 完整流程（非流式）
    # ------------------------------------------------------------------
    async def answer(
        self,
        query: str,
        kb: KnowledgeBase,
        enable_web: bool = True,
    ) -> tuple[str, RAGContext]:
        ctx = await self._prepare(query, kb, enable_web)
        messages = self._build_messages(ctx)
        resp = await self.llm.chat(messages)
        return resp.content, ctx

    # ------------------------------------------------------------------
    # 完整流程（流式）
    # ------------------------------------------------------------------
    async def stream_answer(
        self,
        query: str,
        kb: KnowledgeBase,
        enable_web: bool = True,
    ) -> AsyncIterator[dict]:
        """
        产出事件流：
          {"type": "status",   "data": {...}}   阶段提示
          {"type": "sources",  "data": [...]}   引用来源
          {"type": "delta",    "data": "文本"}  回答增量
          {"type": "done",     "data": {...}}   结束统计
          {"type": "error",    "data": "..."}   错误
        """
        started = time.time()

        yield {"type": "status", "data": {"stage": "rewrite", "message": "正在理解问题..."}}
        ctx = await self._prepare(query, kb, enable_web, on_status=None)

        yield {
            "type": "status",
            "data": {
                "stage": "retrieve",
                "message": f"知识库命中 {len(ctx.chunks)} 段" + ("，已联网补充" if ctx.used_web_search else ""),
                "kb_hit": ctx.kb_hit,
                "used_web": ctx.used_web_search,
                "rewritten_query": ctx.rewritten_query,
            },
        }

        sources = ctx.sources()
        if sources:
            yield {"type": "sources", "data": sources}

        yield {"type": "status", "data": {"stage": "generate", "message": "正在生成回答..."}}

        messages = self._build_messages(ctx)
        try:
            async for piece in self.llm.stream_chat(messages):
                yield {"type": "delta", "data": piece}
        except Exception as e:  # noqa: BLE001
            logger.exception("生成回答失败")
            yield {"type": "error", "data": f"生成回答失败: {e}"}

        yield {
            "type": "done",
            "data": {
                "latency_ms": round((time.time() - started) * 1000, 2),
                "kb_hit": ctx.kb_hit,
                "used_web_search": ctx.used_web_search,
                "chunk_count": len(ctx.chunks),
                "web_count": len(ctx.web_results),
            },
        }

    # ------------------------------------------------------------------
    # 共用前置流程
    # ------------------------------------------------------------------
    async def _prepare(
        self,
        query: str,
        kb: KnowledgeBase,
        enable_web: bool,
        on_status: Any = None,
    ) -> RAGContext:
        ctx = RAGContext(original_query=query)

        # [1] 改写
        queries = await self.rewrite_query(query)
        ctx.rewritten_queries = queries
        ctx.rewritten_query = " | ".join(queries) if queries != [query] else ""

        # [2] 检索
        chunks = await self.retrieve_local(queries, kb)
        ctx.chunks = chunks

        # [3] 判定
        relevant, reason = await self.judge_relevance(query, chunks)
        ctx.kb_hit = relevant
        ctx.judge_reason = reason

        if not relevant:
            # 未命中：丢弃低质量片段，避免干扰
            ctx.chunks = []
            logger.info("知识库未命中(%s)，进入联网流程", reason)

        # [3.5] 联网兜底
        if enable_web and not relevant and settings.web_search_auto_fallback:
            search_query = queries[0] if queries else query
            try:
                web = await self.search_web(search_query)
                ctx.web_results = web
                ctx.used_web_search = bool(web)
            except Exception as e:  # noqa: BLE001
                logger.error("联网搜索失败: %s", e)

        return ctx


_engine: Optional[RAGEngine] = None


def get_rag_engine() -> RAGEngine:
    global _engine
    if _engine is None:
        _engine = RAGEngine()
    return _engine
