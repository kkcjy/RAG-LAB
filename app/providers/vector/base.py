# 向量库抽象层
# 业务代码只依赖 BaseVectorStore；底层可换 Qdrant / pgvector / Milvus
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class VectorRecord:
    """一条待入库的向量记录"""

    id: str
    vector: List[float]
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchHit:
    """一条检索结果"""

    id: str
    score: float
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def content(self) -> str:
        return self.payload.get("content", "")

    @property
    def doc_id(self) -> Optional[str]:
        return self.payload.get("doc_id")

    @property
    def filename(self) -> str:
        return self.payload.get("filename", "")


class BaseVectorStore(abc.ABC):
    """向量库统一接口"""

    @abc.abstractmethod
    async def ensure_collection(self, name: str, dim: int, recreate: bool = False) -> None:
        """确保集合存在"""

    @abc.abstractmethod
    async def upsert(self, collection: str, records: List[VectorRecord]) -> int:
        """插入或更新向量，返回成功条数"""

    @abc.abstractmethod
    async def search(
        self,
        collection: str,
        vector: List[float],
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[SearchHit]:
        """向量检索"""

    @abc.abstractmethod
    async def delete(self, collection: str, ids: Optional[List[str]] = None,
                     filters: Optional[Dict[str, Any]] = None) -> int:
        """按 id 或过滤器删除"""

    @abc.abstractmethod
    async def drop_collection(self, name: str) -> None:
        """删除整个集合（删知识库时用）"""

    @abc.abstractmethod
    async def count(self, collection: str) -> int:
        """统计集合内向量条数"""

    async def health_check(self) -> bool:
        try:
            await self.count("__health_check__")
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("向量库健康检查失败: %s", e)
            return False


# ---------------------------------------------------------------------------
#  工厂 —— 依赖倒置：业务代码只 import 这里
#  换后端（Qdrant / pgvector / Milvus）只需改这个函数，业务代码零改动
# ---------------------------------------------------------------------------
_vector_store: Optional["BaseVectorStore"] = None


def get_vector_store() -> "BaseVectorStore":
    global _vector_store
    if _vector_store is not None:
        return _vector_store

    from app.core.config import settings  # 延迟导入，避免循环依赖

    backend = getattr(settings, "vector_backend", "qdrant")

    if backend == "qdrant":
        from app.providers.vector.qdrant_store import QdrantVectorStore

        _vector_store = QdrantVectorStore(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            api_key=settings.qdrant_api_key,
        )
    else:  # pragma: no cover — 未来扩展点
        raise ValueError(f"未知的向量库后端: {backend}（当前仅支持 qdrant）")

    return _vector_store
