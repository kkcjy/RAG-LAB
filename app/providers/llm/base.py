# LLM Provider 抽象层
# 目标：业务代码只依赖 BaseLLM 接口，切换 API / 本地 vLLM 无需改动业务逻辑
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional

from app.core.config import settings


@dataclass
class ChatMessage:
    role: str  # system | user | assistant
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class LLMResponse:
    content: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""
    raw: Dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BaseLLM(abc.ABC):
    """所有 LLM 实现的统一接口"""

    def __init__(self, model: str, temperature: float, max_tokens: int, timeout: int):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    @abc.abstractmethod
    async def chat(
        self,
        messages: List[ChatMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        """一次性返回完整回答"""
        raise NotImplementedError

    @abc.abstractmethod
    async def stream_chat(
        self,
        messages: List[ChatMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        """流式返回（SSE 用）"""
        raise NotImplementedError
        yield ""  # pragma: no cover  — 让类型检查器识别这是 async generator

    async def health_check(self) -> bool:
        """探活，用于 /health 接口"""
        try:
            await self.chat([ChatMessage(role="user", content="ping")], max_tokens=5)
            return True
        except Exception:
            return False

    @property
    def display_name(self) -> str:
        return f"{self.__class__.__name__}({self.model})"


# ---------------------------------------------------------------------------
#  工厂 —— 依赖倒置：业务代码只 import 这里
#  API ←→ 本地 vLLM 的切换点：改 .env 的 LLM_PROVIDER 即可，代码零改动
# ---------------------------------------------------------------------------
_llm_instance: Optional[BaseLLM] = None


def get_llm() -> BaseLLM:
    global _llm_instance
    if _llm_instance is not None:
        return _llm_instance

    from app.core.config import settings
    from app.providers.llm.openai_compat import OpenAICompatLLM

    provider = settings.llm_provider

    if provider == "deepseek":
        if not settings.deepseek_api_key:
            raise ValueError("LLM_PROVIDER=deepseek 但未配置 DEEPSEEK_API_KEY")
        _llm_instance = OpenAICompatLLM(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout,
        )

    elif provider == "vllm_local":
        if not settings.vllm_base_url:
            raise ValueError("LLM_PROVIDER=vllm_local 但未配置 VLLM_BASE_URL")
        _llm_instance = OpenAICompatLLM(
            model=settings.vllm_model or "local-model",
            api_key="not-needed",
            base_url=settings.vllm_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout,
        )

    elif provider == "openai_compatible":
        if not settings.openai_compat_base_url:
            raise ValueError("LLM_PROVIDER=openai_compatible 但未配置 OPENAI_COMPAT_BASE_URL")
        _llm_instance = OpenAICompatLLM(
            model=settings.openai_compat_model or "gpt-4o-mini",
            api_key=settings.openai_compat_api_key,
            base_url=settings.openai_compat_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout,
        )

    else:
        raise ValueError(f"未知的 LLM_PROVIDER: {provider}")

    return _llm_instance


def reset_llm() -> None:
    """配置变更后重建实例（admin 后台改配置时调用）"""
    global _llm_instance
    _llm_instance = None
