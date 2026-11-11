"""Talk to the receptionist in text: same prompt, tools and state as a phone call, no audio.

Used by `make chat` (try the brain from the terminal) and by the Phase 6 eval suite, where a
simulated caller types instead of speaks.

    async with TextSession(call) as session:
        reply = await session.send("Hi, can I book a haircut tomorrow?")
        print(reply.text, reply.tool_calls)

Pipeline: user aggregator -> LLM -> ReplyCollector -> assistant aggregator. The collector
watches responses and tool calls to know when the agent has finished its turn: no response
streaming, no tool results outstanding, and no LLM re-run pending after a tool result.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pipecat.frames.frames import (
    EndFrame,
    Frame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.workers.runner import WorkerRunner

from receptionist_agent.brain.prompt import greeting
from receptionist_agent.brain.state import CallState
from receptionist_agent.brain.tools import ReceptionistTools, register_tools, tools_schema
from receptionist_shared.tenant import TenantConfig

SETTLE_SECS = 0.15  # quiet time after the last event before we call the turn finished
TURN_TIMEOUT_SECS = 60.0


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: Any = None


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    ended: bool = False


class ReplyCollector(FrameProcessor):
    """Collects the agent's text and tool calls for one caller turn."""

    def __init__(self) -> None:
        super().__init__()
        self._text: list[str] = []
        self._calls: list[ToolCall] = []
        self._in_response = 0
        self._pending_calls = 0
        self._rerun_expected = False
        self._saw_response = False
        self._done = asyncio.Event()
        self._settle: asyncio.TimerHandle | None = None

    def reset(self) -> None:
        self._text, self._calls = [], []
        self._saw_response = False
        self._done.clear()

    def _cancel_settle(self) -> None:
        if self._settle is not None:
            self._settle.cancel()
            self._settle = None

    def _maybe_done(self) -> None:
        self._cancel_settle()
        idle = self._in_response == 0 and self._pending_calls == 0 and not self._rerun_expected
        if idle and self._saw_response:
            self._settle = asyncio.get_running_loop().call_later(SETTLE_SECS, self._done.set)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            self._observe(frame)
        await self.push_frame(frame, direction)

    def _observe(self, frame: Frame) -> None:
        if isinstance(frame, LLMFullResponseStartFrame):
            self._cancel_settle()
            self._in_response += 1
            self._rerun_expected = False
            self._saw_response = True
        elif isinstance(frame, LLMTextFrame):
            self._text.append(frame.text)
        elif isinstance(frame, FunctionCallsStartedFrame):
            self._cancel_settle()
            self._pending_calls += len(frame.function_calls)
            for fc in frame.function_calls:
                self._calls.append(ToolCall(fc.function_name, dict(fc.arguments)))
        elif isinstance(frame, FunctionCallResultFrame):
            self._pending_calls = max(0, self._pending_calls - 1)
            for call in reversed(self._calls):
                if call.name == frame.function_name and call.result is None:
                    call.result = frame.result
                    break
            run_llm = frame.run_llm
            if frame.properties is not None and frame.properties.run_llm is not None:
                run_llm = frame.properties.run_llm
            if self._pending_calls == 0 and run_llm is not False:
                self._rerun_expected = True
            self._maybe_done()
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._in_response = max(0, self._in_response - 1)
            self._maybe_done()

    async def wait(self, limit_secs: float) -> Reply:
        await asyncio.wait_for(self._done.wait(), limit_secs)
        return Reply(text="".join(self._text).strip(), tool_calls=list(self._calls))


class TextSession:
    def __init__(
        self,
        *,
        tenant: TenantConfig,
        llm: LLMService[Any],
        tools: ReceptionistTools,
        state: CallState,
    ) -> None:
        self.tenant = tenant
        self.state = state
        self.context = LLMContext(tools=tools_schema())
        register_tools(llm, tools)
        aggregators = LLMContextAggregatorPair(self.context)
        self._collector = ReplyCollector()
        self._worker = PipelineWorker(
            Pipeline([aggregators.user(), llm, self._collector, aggregators.assistant()]),
            cancel_on_idle_timeout=False,
        )
        self._runner = WorkerRunner(handle_sigint=False)
        self._run_task: asyncio.Task[None] | None = None

    @property
    def greeting(self) -> str:
        return greeting(self.tenant)

    async def start(self) -> None:
        await self._runner.add_workers(self._worker)
        self._run_task = asyncio.create_task(self._runner.run())
        # The greeting goes into the context as if spoken, like on a real call.
        self.context.add_message({"role": "assistant", "content": self.greeting})
        await asyncio.sleep(0)

    async def send(self, text: str, *, limit_secs: float = TURN_TIMEOUT_SECS) -> Reply:
        if self.state.ended:
            return Reply(text="", ended=True)
        self._collector.reset()
        await self._worker.queue_frame(
            LLMMessagesAppendFrame(messages=[{"role": "user", "content": text}], run_llm=True)
        )
        reply = await self._collector.wait(limit_secs)
        reply.ended = self.state.ended
        return reply

    async def close(self) -> None:
        if self._run_task is None:
            return
        if not self._run_task.done():
            await self._worker.queue_frame(EndFrame())
            try:
                await asyncio.wait_for(self._run_task, 5)
            except TimeoutError:
                await self._worker.cancel()
        self._run_task = None

    async def __aenter__(self) -> TextSession:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


__all__ = ["Reply", "ReplyCollector", "TextSession", "ToolCall"]
