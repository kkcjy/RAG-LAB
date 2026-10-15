# OpenAI 兼容协议的 LLM 实现
# DeepSeek、vLLM、硅基流动、OpenAI 本身 全都遵循 OpenAI 的 /chat/completions 协议，
# 因此用同一个实现类 + 不同的 base_url / api_key 即可覆盖全部场景。
from __future__ import annotations

import json
from typing import AsyncIterator, List, Optional

import httpx

from app.core.config import settings
from app.providers.llm.base import BaseLLM, ChatMessage, LLMResponse


class OpenAICompatLLM(BaseLLM):
    """
    适用于：
      - DeepSeek      https://api.deepseek.com
      - vLLM 本地      http://localhost:8001/v1
      - 硅基流动       https://api.siliconflow.cn/v1
      - OpenAI        https://api.openai.com/v1
    """

    def __init__(
        self,
        model: str,
        api_key: Optional[str],
        base_url: str,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: int = 120,
    ):
        super().__init__(model, temperature, max_tokens, timeout)
        self.api_key = api_key or "not-needed"
        # 统一归一化 base_url，确保以 /v1 结尾（DeepSeek 的 /v1 可选，但带上更稳）
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: List[ChatMessage],
        temperature: Optional[float],
        max_tokens: Optional[int],
        stream: bool,
        **kwargs,
    ) -> dict:
        payload = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "stream": stream,
        }
        payload.update(kwargs)
        return payload

    async def chat(
        self,
        messages: List[ChatMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        payload = self._payload(messages, temperature, max_tokens, stream=False, **kwargs)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        usage = data.get("usage", {}) or {}
        return LLMResponse(
            content=choice["message"].get("content") or "",
            model=data.get("model", self.model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            finish_reason=choice.get("finish_reason", ""),
            raw=data,
        )

    async def stream_chat(
        self,
        messages: List[ChatMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        payload = self._payload(messages, temperature, max_tokens, stream=True, **kwargs)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
