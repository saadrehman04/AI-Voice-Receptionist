"""The evals' own LLM clients (simulated caller and judge), separate from the agent's.

Free tiers limit each *model* separately, so the three roles use different models (D29):
the agent runs on Groq gpt-oss-120b (falling back to Gemini 3.5 Flash-Lite); the caller uses
Gemini 3.1 Flash-Lite first; the judge uses Gemini 3.5 Flash first. Everything goes through the
same `FallbackTransport`, and when every model is busy `EvalLLM` waits and retries instead of
failing the scenario.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
from loguru import logger

from receptionist_agent.llm_fallback import (
    GROQ_CHAT_URL,
    FallbackTransport,
    LlmTarget,
    gemini_target,
    groq_target,
)
from receptionist_shared import Settings

CALLER_MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite")
JUDGE_MODELS = ("gemini-3.5-flash", "gemini-3.1-flash-lite")
RETRY_WAITS_SECS = (5.0, 15.0, 30.0, 60.0)  # when every model said 429


class EvalLLMError(RuntimeError):
    pass


class RateLimitedError(EvalLLMError):
    """The free tiers said no for long enough that this scenario can't be judged fairly."""


def eval_targets(settings: Settings, models: tuple[str, ...] = CALLER_MODELS) -> list[LlmTarget]:
    targets: list[LlmTarget] = []
    if settings.gemini_api_key is not None and settings.gemini_api_key.get_secret_value():
        key = settings.gemini_api_key.get_secret_value()
        targets += [gemini_target(model, key) for model in models]
    if settings.groq_api_key is not None and settings.groq_api_key.get_secret_value():
        targets.append(
            groq_target(
                "openai/gpt-oss-20b",
                settings.groq_api_key.get_secret_value(),
                reasoning_effort="low",
            )
        )
    if not targets:
        raise EvalLLMError("evals need GEMINI_API_KEY or GROQ_API_KEY for the caller and judge")
    return targets


class EvalLLM:
    def __init__(
        self,
        targets: list[LlmTarget],
        *,
        network: httpx2.AsyncBaseTransport | None = None,
        retry_waits: tuple[float, ...] = RETRY_WAITS_SECS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.transport = FallbackTransport(targets, inner=network)
        self._client = httpx2.AsyncClient(transport=self.transport, timeout=60)
        self._retry_waits = retry_waits
        self._sleep = sleep
        self.requests = 0
        self.rate_limited_waits = 0

    async def chat(
        self, messages: list[dict[str, str]], *, json_mode: bool = False, temperature: float = 0.7
    ) -> str:
        body: dict[str, Any] = {
            "model": "set-by-transport",
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        self.requests += 1
        response = await self._client.post(GROQ_CHAT_URL, json=body)
        for wait in self._retry_waits:
            if response.status_code != 429:
                break
            self.rate_limited_waits += 1
            logger.info("every eval model is rate-limited; waiting", secs=wait)
            await self._sleep(wait)
            response = await self._client.post(GROQ_CHAT_URL, json=body)
        if response.status_code == 429:
            raise RateLimitedError("every eval model stayed rate-limited")
        if response.status_code != 200:
            raise EvalLLMError(f"eval LLM failed: HTTP {response.status_code}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise EvalLLMError("eval LLM returned no message") from exc
        return str(content or "").strip()

    async def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        raw = await self.chat(messages, json_mode=True, temperature=0)
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("judge returned non-JSON", head=raw[:80])
            raise EvalLLMError("judge returned non-JSON") from None
        if not isinstance(data, dict):
            raise EvalLLMError("judge returned JSON that is not an object")
        return data

    async def aclose(self) -> None:
        await self._client.aclose()
