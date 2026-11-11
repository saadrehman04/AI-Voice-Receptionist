"""Phase 5 latency features, in real Pipecat pipelines (no models, no network)."""

from collections.abc import AsyncGenerator
from typing import Any

from pipecat.frames.frames import (
    Frame,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.services.tts_service import TTSService
from pipecat.tests.utils import SleepFrame, run_test

from receptionist_agent.latency import (
    FILLERS,
    FirstClauseAggregator,
    FullReplyBuffer,
    LatencyFlags,
    NumberPauseTuner,
    ToolFiller,
    use_clause_chunking,
)
from receptionist_shared import Settings


async def chunks(aggregator: FirstClauseAggregator, *parts: str) -> list[str]:
    out: list[str] = []
    for part in parts:
        async for aggregation in aggregator.aggregate(part):
            out.append(aggregation.text)
    rest = await aggregator.flush()
    return [*out, rest.text] if rest is not None and rest.text else out


async def test_first_chunk_may_end_at_a_comma_then_sentences() -> None:
    agg = FirstClauseAggregator()
    got = await chunks(agg, "Sure, we have Thursday at eleven, ", "or twelve. ", "Which one? ")
    assert got == ["Sure, we have Thursday at eleven,", "or twelve.", "Which one?"]


async def test_short_opening_and_numbers_are_not_split() -> None:
    agg = FirstClauseAggregator()
    got = await chunks(agg, "A haircut is 2,500 rupees, about forty five minutes. ")
    # "Sure," alone is too short; "2,500" has no space after the comma.
    assert got[0] == "A haircut is 2,500 rupees,"


async def test_each_reply_gets_its_own_first_clause() -> None:
    agg = FirstClauseAggregator()
    await chunks(agg, "We open at ten in the morning, and close at nine. ")
    again = await chunks(agg, "Parking is free in front, and on the side street. ")
    assert again[0] == "Parking is free in front,"


async def test_urdu_comma_counts() -> None:
    agg = FirstClauseAggregator()
    got = await chunks(agg, "جی ہاں، ہم اتوار کو دس بجے کھلتے ہیں، ", "اور نو بجے بند۔ ")
    assert got[0] == "جی ہاں، ہم اتوار کو دس بجے کھلتے ہیں،"


class RecordingTTS(TTSService):
    """A real Pipecat TTSService that records what it is asked to say."""

    def __init__(self) -> None:
        super().__init__(sample_rate=16000)
        self.said: list[str] = []

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        self.said.append(text)
        yield TTSAudioRawFrame(b"\x00\x00" * 160, 16000, 1, context_id=context_id)


async def test_tts_uses_the_swapped_aggregator() -> None:
    """Guards the private-attribute swap in `use_clause_chunking` across Pipecat upgrades."""
    tts = RecordingTTS()
    use_clause_chunking(tts)
    await run_test(
        tts,
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame("Yes, there is free parking right in front, "),
            LLMTextFrame("and on the side street. "),
            LLMFullResponseEndFrame(),
            SleepFrame(0.3),
        ],
    )
    assert tts.said[0].strip() == "Yes, there is free parking right in front,"


async def test_full_reply_buffer_sends_one_text_after_the_reply_ends() -> None:
    down, _ = await run_test(
        FullReplyBuffer(),
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame("We open "),
            LLMTextFrame("at ten."),
            LLMFullResponseEndFrame(),
        ],
        expected_down_frames=[LLMFullResponseStartFrame, LLMTextFrame, LLMFullResponseEndFrame],
    )
    text = next(f for f in down if isinstance(f, LLMTextFrame))
    assert text.text == "We open at ten."


def filler(language: str | None = "en") -> ToolFiller:
    return ToolFiller(after_ms=100, language=lambda: language)


def spoken(frames: Any) -> list[TTSSpeakFrame]:
    return [f for f in frames if isinstance(f, TTSSpeakFrame)]


async def test_slow_tool_gets_a_filler_that_stays_out_of_the_context() -> None:
    down, _ = await run_test(
        filler(),
        frames_to_send=[
            UserStoppedSpeakingFrame(),
            FunctionCallsStartedFrame(function_calls=[]),
            SleepFrame(0.3),
        ],
    )
    (said,) = spoken(down)
    assert said.text == FILLERS["en"]
    assert said.append_to_context is False  # never between a tool call and its result


async def test_filler_speaks_the_callers_language() -> None:
    down, _ = await run_test(
        filler("ur"),
        frames_to_send=[FunctionCallsStartedFrame(function_calls=[]), SleepFrame(0.3)],
    )
    assert spoken(down)[0].text == FILLERS["ur"]


async def test_no_filler_when_the_agent_already_spoke_or_the_tool_was_fast() -> None:
    already_spoke, _ = await run_test(
        filler(),
        frames_to_send=[
            UserStoppedSpeakingFrame(),
            LLMTextFrame("Let me check that."),
            FunctionCallsStartedFrame(function_calls=[]),
            SleepFrame(0.3),
        ],
    )
    fast_tool, _ = await run_test(
        filler(),
        frames_to_send=[
            UserStoppedSpeakingFrame(),
            FunctionCallsStartedFrame(function_calls=[]),
            LLMTextFrame("Yes, there is parking."),  # the answer came within 100 ms
            SleepFrame(0.3),
        ],
    )
    assert spoken(already_spoke) == []
    assert spoken(fast_tool) == []


def test_flags_come_from_settings() -> None:
    flags = LatencyFlags.from_settings(
        Settings(_env_file=None, tts_text_chunking="full", filler_after_ms=0, vad_stop_secs=0.3)
    )
    assert flags == LatencyFlags(text_chunking="full", filler_after_ms=0, vad_stop_secs=0.3)
    assert flags.vad_number_stop_secs == 0.8


async def test_number_question_lengthens_the_next_pause_then_restores_it() -> None:
    from pipecat.frames.frames import VADParamsUpdateFrame

    tuner = NumberPauseTuner(normal_secs=0.2, number_secs=0.8)
    _, up = await run_test(
        tuner,
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame("May I have your name and phone number, please?"),
            LLMFullResponseEndFrame(),
            # System frames jump the queue; in a call this comes seconds later, after the caller.
            SleepFrame(0.1),
            UserStoppedSpeakingFrame(),  # the caller has read out the number
            LLMFullResponseStartFrame(),
            LLMTextFrame("Thank you. Is there parking? Yes, free parking."),  # no number asked
            LLMFullResponseEndFrame(),
        ],
    )
    updates = [f.params.stop_secs for f in up if isinstance(f, VADParamsUpdateFrame)]
    assert updates == [0.8, 0.2]


async def test_urdu_number_question_counts() -> None:
    from pipecat.frames.frames import VADParamsUpdateFrame

    _, up = await run_test(
        NumberPauseTuner(normal_secs=0.2, number_secs=0.8),
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame("براہ کرم اپنا نام اور فون نمبر بتائیں؟"),
            LLMFullResponseEndFrame(),
        ],
    )
    assert [f.params.stop_secs for f in up if isinstance(f, VADParamsUpdateFrame)] == [0.8]
