"""Latency benchmark: replay caller utterances through the real pipeline, per config.

    make bench                                   # every config in CONFIGS
    make bench ARGS="--configs baseline piper"   # some of them
    make bench ARGS="--report-only"              # rebuild the report from saved results

For each config this runs the two scripted calls in bench/scripts.yaml (20 utterances) and the
barge-in check, through Silero VAD, Smart Turn, Groq STT, the LLM (with fallback), TTS and a
fake transport (`ReplayTransport`). Time zero for a turn is when the caller's audio really ended,
so "first audio" is what a caller waits through, minus the network. Results go to
data/bench/results/*.jsonl; the report to docs/benchmarks/<date>.md.

Free-tier note: all configs share Groq's per-minute budget, so `--gap-secs` paces the turns and
the report shows which LLM answered each turn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    FunctionCallInProgressFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import LLMService
from pipecat.workers.runner import WorkerRunner

from receptionist_agent.bench.replay import ReplayTransport
from receptionist_agent.bench.synth import Synth, caller_audio
from receptionist_agent.bot import Call, build_task, prepare_call
from receptionist_agent.brain.backend import InMemoryBackend
from receptionist_agent.brain.prompt import greeting
from receptionist_agent.latency import prewarm
from receptionist_agent.llm_fallback import LlmTarget
from receptionist_shared import Settings
from receptionist_shared.logging import configure_logging

SCRIPTS = Path(__file__).with_name("scripts.yaml")
DATA = Path("data/bench")
QUIET_SECS = 1.5  # agent silent this long after speaking = turn over
TURN_TIMEOUT_SECS = 45.0
NS = 1_000_000_000

# Flag combinations to compare. Each is a set of Settings overrides on top of .env.
# A ladder: each config adds one change to the one before, so each step's effect shows.
_PHASE4 = {  # how Phases 1-4 shipped
    "tts_provider": "kokoro",
    "tts_text_chunking": "sentence",
    "filler_after_ms": 0,
    "prewarm_connections": False,
    "llm_simple_turn_model": "",
    "vad_stop_secs": 0.2,
}
_PIPER = _PHASE4 | {"tts_provider": "piper"}
_CLAUSE = _PIPER | {"tts_text_chunking": "clause"}
_FILLER = _CLAUSE | {"filler_after_ms": 400}
_PREWARM = _FILLER | {"prewarm_connections": True}
_ROUTE = _PREWARM | {"llm_simple_turn_model": "groq:openai/gpt-oss-20b"}
CONFIGS: dict[str, dict[str, Any]] = {
    "baseline": _PHASE4,
    "no-streaming": _PIPER | {"tts_text_chunking": "full"},  # what streaming buys
    "piper": _PIPER,
    "clause": _CLAUSE,
    "filler": _FILLER,
    "prewarm": _PREWARM,
    "route-simple": _ROUTE,
    "vad-0.15": _ROUTE | {"vad_stop_secs": 0.15},
}


# ---------------------------------------------------------------- observation


@dataclass
class Marks:
    """Monotonic ns of the first time each event happened since `arm()`."""

    armed_ns: int = 0
    vad_starts: list[int] = field(default_factory=list)
    vad_stops: list[int] = field(default_factory=list)
    transcripts: list[tuple[int, str]] = field(default_factory=list)
    turn_released: int | None = None
    llm_first_token: int | None = None
    tts_first_audio: int | None = None
    bot_started: int | None = None
    tool_started: list[tuple[int, str]] = field(default_factory=list)
    interruptions: list[int] = field(default_factory=list)
    llm_text: list[str] = field(default_factory=list)
    responses_done: int = 0


class BenchObserver(BaseObserver):
    def __init__(self, llm: LLMService[Any]) -> None:
        super().__init__()
        self._llm = llm
        self._seen: set[int] = set()
        self.marks = Marks()

    def arm(self) -> Marks:
        self.marks = Marks(armed_ns=time.monotonic_ns())
        return self.marks

    async def on_push_frame(self, data: FramePushed) -> None:
        now, frame, m = time.monotonic_ns(), data.frame, self.marks
        # A frame is seen once per hop; record each event at its source only.
        from_llm = data.source is self._llm
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._once_per_frame(frame, m.vad_starts, now)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._once_per_frame(frame, m.vad_stops, now)
        elif isinstance(frame, TranscriptionFrame) and self._first_hop(frame):
            m.transcripts.append((now, frame.text))
        elif isinstance(frame, UserStoppedSpeakingFrame):
            m.turn_released = m.turn_released or now
        elif isinstance(frame, LLMTextFrame) and from_llm:
            m.llm_first_token = m.llm_first_token or now
            m.llm_text.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame) and from_llm:
            m.responses_done += 1
        elif isinstance(frame, TTSAudioRawFrame):
            m.tts_first_audio = m.tts_first_audio or now
        elif isinstance(frame, BotStartedSpeakingFrame):
            m.bot_started = m.bot_started or now
        elif isinstance(frame, FunctionCallInProgressFrame) and self._first_hop(frame):
            m.tool_started.append((now, frame.function_name))
        elif isinstance(frame, InterruptionFrame) and self._first_hop(frame):
            m.interruptions.append(now)

    def _first_hop(self, frame: Frame) -> bool:
        """True the first time this frame is seen (the observer sees a frame at every hop)."""
        if frame.id in self._seen:
            return False
        self._seen.add(frame.id)
        return True

    def _once_per_frame(self, frame: Frame, into: list[int], now: int) -> None:
        if self._first_hop(frame):
            into.append(now)


# ---------------------------------------------------------------- one call


@dataclass
class TurnResult:
    config: str
    call: str
    id: str
    kind: str
    language: str
    caller_secs: float
    first_audio_ms: float | None  # caller's speech really ended -> agent audio out
    vad_stop_ms: float | None
    stt_final_ms: float | None
    turn_released_ms: float | None
    llm_first_token_ms: float | None
    tts_first_audio_ms: float | None
    tools: list[str]
    llm_targets: list[str]
    transcript: str
    reply: str
    timed_out: bool = False


def _ms(t: int | None, zero: int) -> float | None:
    return None if t is None else round((t - zero) / 1e6, 1)


class BenchCall:
    """One scripted call on a fresh pipeline."""

    def __init__(self, settings: Settings, tenant: str, *, label: str) -> None:
        self.settings, self.tenant, self.label = settings, tenant, label
        self.transport = ReplayTransport()
        self.targets_used: list[str] = []
        self.call: Call | None = None
        self.context: LLMContext | None = None
        self._runner_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> BenchCall:
        self.call = await asyncio.to_thread(
            prepare_call,
            self.settings,
            call_id=f"bench-{self.label}-{uuid.uuid4().hex[:6]}",
            tenant_slug=self.tenant,
            backend=InMemoryBackend(),
        )
        if self.call.flags.prewarm:  # as run_bot does
            self._warmup = asyncio.create_task(
                prewarm(self.call.services.stt, self.call.services.llm)
            )
        fallback = getattr(self.call.services.llm, "fallback", None)
        if fallback is not None:

            def served(target: LlmTarget, _status: int) -> None:
                self.targets_used.append(target.name)

            fallback.on_served = served
        self.observer = BenchObserver(self.call.services.llm)
        self.task, self.context = build_task(self.transport, self.call, observers=[self.observer])
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(self.task)
        self._runner_task = asyncio.create_task(runner.run())
        await asyncio.sleep(1.0)  # let StartFrame reach every processor
        self.observer.arm()
        await self.task.queue_frames([TTSSpeakFrame(greeting(self.call.tenant))])
        await self.wait_quiet(time.monotonic_ns())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._runner_task is not None and not self._runner_task.done():
            await self.task.cancel()
            try:
                await asyncio.wait_for(self._runner_task, 10)
            except TimeoutError:
                logger.warning("bench call did not stop in time")

    @property
    def ended(self) -> bool:
        return self._runner_task is not None and self._runner_task.done()

    async def wait_quiet(self, since_ns: int, limit_secs: float = TURN_TIMEOUT_SECS) -> bool:
        """Wait until the agent has spoken since `since_ns`, then gone quiet. False if it didn't."""
        deadline = time.monotonic() + limit_secs
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self.ended:
                return True
            writes = self.transport.speaker.audio_since(since_ns)
            if writes:
                last_end = writes[-1][0] + writes[-1][1]
                if time.monotonic_ns() - last_end > QUIET_SECS * NS:
                    return True
        return False

    async def turn(self, uid: str, kind: str, language: str, pcm: bytes) -> TurnResult:
        marks = self.observer.arm()
        targets_before = len(self.targets_used)
        _start, end = await self.transport.caller.play(pcm)
        finished = await self.wait_quiet(end)
        writes = self.transport.speaker.audio_since(end)
        released = marks.turn_released

        def before_release(t: int) -> bool:
            return released is None or t <= released

        vad_stop = max(filter(before_release, marks.vad_stops), default=None)
        stt = max((t for t, _ in marks.transcripts if before_release(t)), default=None)
        first_audio = writes[0][0] if writes else None
        reply = _last_message(self.context, "assistant") if self.context is not None else ""
        return TurnResult(
            config=self.label,
            call=self.label,
            id=uid,
            kind=kind,
            language=language,
            caller_secs=round(len(pcm) / 2 / 16000, 2),
            first_audio_ms=_ms(first_audio, end),
            vad_stop_ms=_ms(vad_stop, end),
            stt_final_ms=_ms(stt, end),
            turn_released_ms=_ms(released, end),
            llm_first_token_ms=_ms(marks.llm_first_token, end),
            tts_first_audio_ms=_ms(marks.tts_first_audio, end),
            tools=[name for _, name in marks.tool_started],
            llm_targets=self.targets_used[targets_before:],
            transcript=" ".join(text for _, text in marks.transcripts),
            reply=reply,
            timed_out=not finished,
        )


def _last_message(context: LLMContext, role: str) -> str:
    for m in reversed(context.get_messages()):
        if isinstance(m, dict) and m.get("role") == role and isinstance(m.get("content"), str):
            return str(m["content"])
    return ""


# ---------------------------------------------------------------- barge-in


def _reply_after_first_caller_turn(context: LLMContext) -> str:
    """What the context says the agent said between the caller's first and second messages."""
    text: list[str] = []
    callers = 0
    for m in context.get_messages():
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            callers += 1
            if callers == 2:
                break
        elif callers == 1 and m.get("role") == "assistant" and isinstance(m.get("content"), str):
            text.append(str(m["content"]))
    return " ".join(text)


@dataclass
class BargeInResult:
    config: str
    agent_was_speaking: bool  # the check only means something if the agent was mid-sentence
    vad_detect_ms: float | None  # caller starts talking over the agent -> VAD notices
    interruption_ms: float | None  # caller starts talking -> Pipecat interrupts the reply
    audio_stop_ms: float | None  # caller starts talking -> the agent's audio actually stops
    stop_after_interruption_ms: float | None  # interruption -> audio stops (playback flush)
    leaked_chunks: int  # agent audio started after the interruption (should be 0)
    generated_chars: int  # what the LLM wrote for the interrupted reply
    kept_chars: int  # what the context says the agent said
    kept_is_prefix: bool
    kept_text: str
    generated_text: str


LEAK_WINDOW_NS = 600_000_000  # a reply to the interruption can't start sooner than this


async def barge_in(
    bench: BenchCall, prompt: bytes, interrupt: bytes, after_secs: float
) -> BargeInResult:
    marks = bench.observer.arm()
    speaker = bench.transport.speaker
    _s, end = await bench.transport.caller.play(prompt)
    # wait for the agent to start answering, then let it talk for a bit
    deadline = time.monotonic() + TURN_TIMEOUT_SECS
    while not speaker.audio_since(end) and time.monotonic() < deadline:  # noqa: ASYNC110 - polls a fake speaker
        await asyncio.sleep(0.02)
    await asyncio.sleep(after_secs)
    vad_before, interrupts_before = len(marks.vad_starts), len(marks.interruptions)
    utterance = bench.transport.caller.start_playing(interrupt)
    await utterance.started.wait()
    talk = utterance.started_ns
    generated = "".join(marks.llm_text)  # the reply as written, before the caller cut in
    await utterance.done.wait()
    await bench.wait_quiet(utterance.ended_ns)

    vad = marks.vad_starts[vad_before] if len(marks.vad_starts) > vad_before else None
    new_hits = marks.interruptions[interrupts_before:]
    hit = new_hits[0] if new_hits else None
    writes = speaker.writes
    speaking = any(w <= talk + 100_000_000 and w + d >= talk for w, d in writes)
    stop: int | None = None
    leaked = 0
    if hit is not None:
        in_flight = [w + d for w, d in writes if w <= hit]
        leaks = [(w, d) for w, d in writes if hit < w <= hit + LEAK_WINDOW_NS]
        leaked = len(leaks)
        ends = in_flight[-1:] + [w + d for w, d in leaks]
        stop = max(ends) if ends else hit
    kept = _reply_after_first_caller_turn(bench.context) if bench.context is not None else ""
    squash = str.maketrans("", "", " \n")
    return BargeInResult(
        config=bench.label,
        agent_was_speaking=speaking,
        vad_detect_ms=_ms(vad, talk),
        interruption_ms=_ms(hit, talk),
        audio_stop_ms=_ms(stop, talk),
        stop_after_interruption_ms=_ms(stop, hit) if hit is not None and stop is not None else None,
        leaked_chunks=leaked,
        generated_chars=len(generated),
        kept_chars=len(kept),
        kept_is_prefix=generated.translate(squash).startswith(kept.translate(squash)),
        kept_text=kept[:300],
        generated_text=generated[:600],
    )


# ---------------------------------------------------------------- driver


async def run_config(
    name: str,
    overrides: dict[str, Any],
    *,
    gap_secs: float,
    out: Path,
    synth: Synth,
    max_turns: int | None = None,
    barge: bool = True,
) -> None:
    scripts = yaml.safe_load(SCRIPTS.read_text())
    settings = Settings(**overrides)
    with out.open("a") as f:
        for call in scripts["calls"]:
            async with BenchCall(settings, scripts["tenant"], label=name) as bench:
                for turn in call["turns"][:max_turns]:
                    pcm = caller_audio(turn["id"], turn["text"], call["language"], DATA, synth)
                    result = await bench.turn(turn["id"], turn["kind"], call["language"], pcm)
                    result.call = call["id"]
                    row = {"type": "turn", **asdict(result)}
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
                    logger.info(
                        "bench turn",
                        config=name,
                        id=turn["id"],
                        first_audio_ms=result.first_audio_ms,
                        llm=result.llm_targets,
                        reply=result.reply[:80],
                    )
                    if bench.ended:
                        break
                    await asyncio.sleep(gap_secs)
        if not barge:
            return
        bi = scripts["barge_in"]
        async with BenchCall(settings, scripts["tenant"], label=name) as bench:
            prompt, cut_in = bi["prompt"], bi["interrupt"]
            prompt_pcm = caller_audio(prompt["id"], prompt["text"], "en", DATA, synth)
            interrupt_pcm = caller_audio(cut_in["id"], cut_in["text"], "en", DATA, synth)
            result_bi = await barge_in(bench, prompt_pcm, interrupt_pcm, bi["after_agent_secs"])
            f.write(json.dumps({"type": "barge_in", **asdict(result_bi)}) + "\n")
            logger.info("bench barge-in", **asdict(result_bi))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--configs", nargs="*", default=list(CONFIGS))
    parser.add_argument("--gap-secs", type=float, default=6.0)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--turns", type=int, default=None, help="max turns per call (smoke runs)")
    parser.add_argument("--no-barge-in", action="store_true")
    args = parser.parse_args(argv)
    configure_logging("INFO", json_logs=False)
    from receptionist_agent.bench.report import write_report

    results_dir = DATA / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    if not args.report_only:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        synth = Synth()
        for name in args.configs:
            out = results_dir / f"{stamp}-{name}.jsonl"
            asyncio.run(
                run_config(
                    name,
                    CONFIGS[name],
                    gap_secs=args.gap_secs,
                    out=out,
                    synth=synth,
                    max_turns=args.turns,
                    barge=not args.no_barge_in,
                )
            )
    path = write_report(results_dir, Path("docs/benchmarks"), configs=CONFIGS)
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
