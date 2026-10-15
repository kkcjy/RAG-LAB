# 联网搜索 Provider
# 当前实现：Tavily；通过工厂函数预留其他搜索源
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    title: str
    url: str
    content: str
    score: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


class BaseWebSearch(abc.ABC):
    @abc.abstractmethod
    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        ...

    @property
    def display_name(self) -> str:
        return self.__class__.__name__


class TavilyWebSearch(BaseWebSearch):
    """Tavily —— 专为 LLM/RAG 设计的搜索 API，返回内容已做清洗，可直接喂模型"""

    def __init__(self, api_key: str, search_depth: str = "basic"):
        self.api_key = api_key
        self.search_depth = search_depth
        self.endpoint = "https://api.tavily.com/search"

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        import httpx

        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": self.search_depth,
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self.endpoint, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.error("Tavily 搜索失败: %s", e)
            return []

        results = []
        for item in data.get("results", []):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    content=item.get("content", ""),
                    score=float(item.get("score", 0.0)),
                    raw=item,
                )
            )
        return results


class NullWebSearch(BaseWebSearch):
    """占位实现：未配置搜索时使用，保证流程不崩"""

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        logger.warning("联网搜索未配置，跳过: %s", query)
        return []


# ---------------------------------------------------------------------------
_web_search: Optional[BaseWebSearch] = None


def get_web_search() -> BaseWebSearch:
    global _web_search
    if _web_search is not None:
        return _web_search

    provider = settings.web_search_provider

    if provider == "tavily" and settings.tavily_api_key:
        _web_search = TavilyWebSearch(
            api_key=settings.tavily_api_key,
            search_depth=settings.tavily_search_depth,
        )
    else:
        if provider == "tavily":
            logger.warning("WEB_SEARCH_PROVIDER=tavily 但未配置 TAVILY_API_KEY，联网搜索将不可用")
        _web_search = NullWebSearch()

    return _web_search
