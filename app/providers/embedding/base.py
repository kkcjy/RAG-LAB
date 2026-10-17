# Embedding Provider 抽象层
# 默认走本地 BGE-M3（CPU）；也支持切到 OpenAI 兼容的 embedding API
from __future__ import annotations

import abc
import logging
from typing import List, Optional

from app.core.config import settings
from app.core.errors import EmbeddingError

logger = logging.getLogger(__name__)


class BaseEmbedding(abc.ABC):
    """所有 Embedding 实现的统一接口"""

    @property
    @abc.abstractmethod
    def dimension(self) -> int:
        """向量维度，建集合时需要"""

    @abc.abstractmethod
    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """批量编码文档（入库用）"""

    @abc.abstractmethod
    async def embed_query(self, text: str) -> List[float]:
        """编码单条查询（检索用）"""

    async def health_check(self) -> bool:
        try:
            v = await self.embed_query("健康检查")
            return len(v) > 0
        except Exception as e:  # noqa: BLE001
            logger.error("Embedding 健康检查失败: %s", e)
            return False

    @property
    def display_name(self) -> str:
        return self.__class__.__name__

    async def warmup(self) -> bool:
        """预热钩子。默认空实现（远程 API 无需预热）。"""
        return True


class LocalBGEEmbedding(BaseEmbedding):
    """
    本地 BGE-M3，CPU 推理。
    模型首次调用时懒加载并驻留内存（约 2GB）。
    BGE-M3 原生维度 1024，支持中英多语言，中文效果强。
    """

    def __init__(
        self,
        model_path: str = "BAAI/bge-m3",
        device: str = "cpu",
        num_threads: int = 4,
        batch_size: int = 16,
        normalize: bool = True,
    ):
        self.model_path = model_path
        self.device = device
        self.num_threads = num_threads
        self.batch_size = batch_size
        self.normalize = normalize
        self._model = None
        self._dim: Optional[int] = None

    def _load(self):
        """懒加载模型，避免应用启动时阻塞"""
        if self._model is not None:
            return

        import os

        # HuggingFace 镜像必须在 import huggingface_hub 之前设置才生效
        endpoint = getattr(settings, "hf_endpoint", None)
        if endpoint:
            os.environ.setdefault("HF_ENDPOINT", endpoint)

        import torch
        from sentence_transformers import SentenceTransformer

        # 限制 CPU 线程数，避免吃满
        if self.device == "cpu":
            torch.set_num_threads(self.num_threads)

        logger.info("正在加载 BGE 模型: %s (device=%s)", self.model_path, self.device)
        model = SentenceTransformer(self.model_path, device=self.device)
        # 通过一次试编码确定维度，避免硬编码
        probe = model.encode(["维度探测"], normalize_embeddings=self.normalize)
        self._dim = len(probe[0])
        self._model = model

    @property
    def dimension(self) -> int:
        if self._dim is None:
            self._load()
        return self._dim  # type: ignore[return-value]

    def _encode(self, texts: List[str]) -> List[List[float]]:
        self._load()
        vecs = self._model.encode(  # type: ignore[union-attr]
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=self.normalize,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [v.tolist() for v in vecs]

    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        import asyncio

        # CPU 推理是阻塞操作，丢到线程池，避免卡住事件循环
        return await asyncio.to_thread(self._encode, texts)

    async def embed_query(self, text: str) -> List[float]:
        vecs = await self.embed_documents([text])
        return vecs[0]

    async def warmup(self) -> bool:
        """预热：提前加载模型，避免首次上传/提问时卡顿。

        在线程池中执行，不阻塞事件循环；失败不影响服务可用性。
        """
        import asyncio

        try:
            await asyncio.to_thread(self._load)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("BGE 模型预热失败（将在首次使用时重试）: %s", e)
            return False


class OpenAICompatEmbedding(BaseEmbedding):
    """走 OpenAI 兼容协议的 embedding API（硅基流动 / OpenAI / 自建）"""

    def __init__(
        self,
        model: str,
        api_key: Optional[str],
        base_url: str,
        dim: int = 0,
    ):
        self.model = model
        self.api_key = api_key or "not-needed"
        self.base_url = base_url.rstrip("/")
        # 0 表示维度未知，启动预热时探测（见 warmup）
        self._dim = dim

    @property
    def dimension(self) -> int:
        if self._dim:
            return self._dim
        raise EmbeddingError(
            f"Embedding 维度未知（模型 {self.model}）。"
            f"请在 .env 设置 EMBEDDING_API_DIM，"
            f"或先成功调用一次 embed_documents 让系统自动探测。"
        )

    async def warmup(self) -> bool:
        """探测真实维度。

        建集合需要知道维度，而 API Embedding 的维度只有响应才知道——
        此前这里硬编码 1024，维度不符时集合建错、写入被 Qdrant 拒绝。
        """
        if self._dim:
            return True
        try:
            await self.embed_query("维度探测")
            logger.info("Embedding API 维度探测完成: dim=%d", self._dim)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Embedding API 维度探测失败（首次使用时重试）: %s", e)
            return False

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        import httpx

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers=self._headers(),
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()

        items = sorted(data["data"], key=lambda x: x["index"])
        vecs = [it["embedding"] for it in items]
        if vecs:
            self._dim = len(vecs[0])
        return vecs

    async def embed_query(self, text: str) -> List[float]:
        vecs = await self.embed_documents([text])
        return vecs[0]


# ---------------------------------------------------------------------------
#  工厂 —— 依赖倒置：业务代码只 import 这里
#  本地 BGE ←→ Embedding API 的切换点
# ---------------------------------------------------------------------------
_embedding_instance: Optional[BaseEmbedding] = None


def get_embedding() -> BaseEmbedding:
    """单例，避免重复加载模型（BGE-M3 约 2GB 内存）"""
    global _embedding_instance
    if _embedding_instance is not None:
        return _embedding_instance

    from app.core.config import settings  # 延迟导入

    provider = settings.embedding_provider

    if provider == "local_bge":
        _embedding_instance = LocalBGEEmbedding(
            model_path=settings.bge_model_path,
            device=settings.bge_device,
            num_threads=settings.bge_num_threads,
            batch_size=settings.bge_batch_size,
            normalize=settings.bge_normalize,
        )
    elif provider == "openai_compatible":
        if not settings.embedding_api_base_url:
            raise ValueError("EMBEDDING_PROVIDER=openai_compatible 但未配置 EMBEDDING_API_BASE_URL")
        _embedding_instance = OpenAICompatEmbedding(
            model=settings.embedding_api_model or "BAAI/bge-m3",
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_api_base_url,
            dim=settings.embedding_api_dim,
        )
    else:
        raise ValueError(f"未知的 EMBEDDING_PROVIDER: {provider}")

    return _embedding_instance


def reset_embedding() -> None:
    global _embedding_instance
    _embedding_instance = None
