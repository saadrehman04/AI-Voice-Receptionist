"""Test helpers shared by unit tests and the eval suite.

`ScriptedLLM` is a real Pipecat OpenAI-compatible LLM service whose completions come from a
Python function instead of the network. Everything around it (context, tool registration,
function-call frames, result handling, re-running after tools) is Pipecat's real code, so tests
exercise the same paths as a live call, deterministically and offline.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from pipecat.frames.frames import FunctionCallFromLLM, LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService


@dataclass
class Say:
    text: str


@dataclass
class CallTools:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    text: str = ""  # optional words before the tool calls (e.g. a goodbye before end_call)


Step = Say | CallTools
Script = Callable[[Sequence[dict[str, Any]]], Step]


class ScriptedLLM(OpenAILLMService):
    """An LLM whose next move is `script(messages)`; records every context it was shown."""

    def __init__(self, script: Script) -> None:
        super().__init__(api_key="scripted", settings=OpenAILLMService.Settings(model="scripted"))
        self._script = script
        self.seen: list[list[dict[str, Any]]] = []

    async def _process_context(self, context: LLMContext) -> None:
        raw = context.get_messages()
        messages = [cast(dict[str, Any], m) for m in raw if isinstance(m, dict)]
        self.seen.append(messages)
        step = self._script(messages)
        if isinstance(step, Say):
            await self.push_frame(LLMTextFrame(step.text))
            return
        if step.text:
            await self.push_frame(LLMTextFrame(step.text))
        await self.run_function_calls(
            [
                FunctionCallFromLLM(
                    function_name=name,
                    tool_call_id=f"call_{uuid.uuid4().hex[:8]}",
                    arguments=args,
                    context=context,
                )
                for name, args in step.calls
            ]
        )


def last_tool_result(messages: Sequence[dict[str, Any]]) -> str | None:
    """Content of the most recent tool message in an OpenAI-format message list."""
    for message in reversed(messages):
        if message.get("role") == "tool":
            content = message.get("content")
            return content if isinstance(content, str) else str(content)
    return None


def last_user_text(messages: Sequence[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else str(content)
    return ""
