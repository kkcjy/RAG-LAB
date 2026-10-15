# Qdrant 向量库实现
# 单容器部署，REST + gRPC，支持 HNSW 索引与富元数据过滤
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.providers.vector.base import BaseVectorStore, SearchHit, VectorRecord

logger = logging.getLogger(__name__)


class QdrantVectorStore(BaseVectorStore):
    """
    使用 qdrant-client 的异步接口。
    集合名 = 知识库标识（kb_<id>），一个知识库一个集合，天然隔离。
    """

    def __init__(
        self,
        host: str,
        port: int,
        api_key: Optional[str] = None,
        prefer_grpc: bool = False,
    ):
        self.host = host
        self.port = port
        self.api_key = api_key or None
        self.prefer_grpc = prefer_grpc
        self._client = None

    # ------------------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            from qdrant_client import AsyncQdrantClient

            self._client = AsyncQdrantClient(
                host=self.host,
                port=self.port,
                api_key=self.api_key,
                prefer_grpc=self.prefer_grpc,
                timeout=30,
            )
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    async def ensure_collection(self, name: str, dim: int, recreate: bool = False) -> None:
        from qdrant_client.models import Distance, VectorParams

        client = self._get_client()
        exists = await client.collection_exists(name)

        if exists and recreate:
            logger.warning("集合 %s 已存在，按请求重建", name)
            await client.delete_collection(name)
            exists = False

        if not exists:
            await client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            logger.info("已创建 Qdrant 集合 %s (dim=%d)", name, dim)
            # 为常用过滤字段建索引，加速过滤检索
            for field_name in ("doc_id", "kb_id"):
                try:
                    await client.create_payload_index(
                        collection_name=name,
                        field_name=field_name,
                        field_schema="keyword",
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug("为 %s.%s 建索引失败（可忽略）: %s", name, field_name, e)

    async def upsert(self, collection: str, records: List[VectorRecord]) -> int:
        from qdrant_client.models import PointStruct

        if not records:
            return 0
        client = self._get_client()
        points = [
            PointStruct(id=r.id, vector=r.vector, payload=r.payload)
            for r in records
        ]
        # 分批，避免单次请求过大
        batch = 128
        for i in range(0, len(points), batch):
            await client.upsert(collection_name=collection, points=points[i:i + batch], wait=True)
        return len(points)

    async def search(
        self,
        collection: str,
        vector: List[float],
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[SearchHit]:
        client = self._get_client()

        qfilter = None
        if filters:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            must = [
                FieldCondition(key=k, match=MatchValue(value=v))
                for k, v in filters.items()
                if v is not None
            ]
            if must:
                qfilter = Filter(must=must)

        try:
            result = await client.query_points(
                collection_name=collection,
                query=vector,
                limit=top_k,
                score_threshold=score_threshold,
                query_filter=qfilter,
                with_payload=True,
            )
            points = result.points
        except Exception as e:  # noqa: BLE001
            logger.error("Qdrant 检索失败: %s", e)
            return []

        return [
            SearchHit(id=str(p.id), score=float(p.score), payload=p.payload or {})
            for p in points
        ]

    async def delete(
        self,
        collection: str,
        ids: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> int:
        from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList

        client = self._get_client()

        if ids:
            await client.delete(
                collection_name=collection,
                points_selector=PointIdsList(points=ids),
                wait=True,
            )
            return len(ids)

        if filters:
            must = [
                FieldCondition(key=k, match=MatchValue(value=v))
                for k, v in filters.items()
                if v is not None
            ]
            if must:
                await client.delete(
                    collection_name=collection,
                    points_selector=Filter(must=must),
                    wait=True,
                )
                return 1
        return 0

    async def drop_collection(self, name: str) -> None:
        client = self._get_client()
        if await client.collection_exists(name):
            await client.delete_collection(name)
            logger.info("已删除 Qdrant 集合 %s", name)

    async def count(self, collection: str) -> int:
        client = self._get_client()
        try:
            if not await client.collection_exists(collection):
                return 0
            info = await client.count(collection_name=collection, exact=True)
            return int(info.count)
        except Exception:  # noqa: BLE001
            return 0

    async def health_check(self) -> bool:
        try:
            client = self._get_client()
            await client.get_collections()
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("Qdrant 健康检查失败: %s", e)
            return False

    async def list_collections(self) -> list[str]:
        client = self._get_client()
        try:
            res = await client.get_collections()
            return [c.name for c in res.collections]
        except Exception:  # noqa: BLE001
            return []

    async def collection_info(self, name: str) -> dict:
        client = self._get_client()
        try:
            if not await client.collection_exists(name):
                return {}
            info = await client.get_collection(name)
            return {
                "points_count": info.points_count,
                "vectors_count": getattr(info, "vectors_count", None),
                "status": str(info.status),
            }
        except Exception:  # noqa: BLE001
            return {}
