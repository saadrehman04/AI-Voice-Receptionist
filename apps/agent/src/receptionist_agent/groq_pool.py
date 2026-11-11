"""Groq LLM service whose HTTP client falls back across models (see `llm_fallback`).

Kept in its own module so importing `providers` doesn't import the Groq/OpenAI SDKs until
a Groq LLM is actually built.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pipecat.services.groq.llm import GroqLLMService

from receptionist_agent.llm_fallback import FallbackTransport, LlmTarget

# The transport already moved on to the next model on a 429; one more client-level retry
# covers a blip on the last target without long `retry-after` waits.
CLIENT_MAX_RETRIES = 1


class PooledGroqLLMService(GroqLLMService):
    def __init__(
        self,
        *,
        targets: Sequence[LlmTarget],
        network: httpx2.AsyncBaseTransport | None = None,  # tests pass a mock here
        **kwargs: Any,
    ) -> None:
        # Set before super().__init__, which calls create_client.
        self.fallback = FallbackTransport(targets, inner=network)
        super().__init__(**kwargs)

    def create_client(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        organization: str | None = None,
        project: str | None = None,
        default_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            project=project,
            default_headers=default_headers,
            max_retries=CLIENT_MAX_RETRIES,
            http_client=DefaultAsyncHttpxClient(transport=self.fallback),
        )
