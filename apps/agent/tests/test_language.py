import pytest
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    ManuallySwitchServiceFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.tests.utils import run_test

from receptionist_agent.language import (
    CallerLanguageTracker,
    ReplyLanguageRouter,
    caller_language,
    detect_script,
)


@pytest.mark.parametrize(
    ("text", "script"),
    [
        ("Hello there", "en"),
        ("  123, ", None),
        ("جی ہاں", "ur"),
        ("हाँ जी", "hi"),
        ("— السلام علیکم", "ur"),
    ],
)
def test_detect_script(text: str, script: str | None) -> None:
    assert detect_script(text) == script


def test_hindi_script_counts_as_urdu_caller() -> None:
    assert caller_language("मुझे अपॉइंटमेंट चाहिए") == "ur"
    assert caller_language("I need an appointment") == "en"


async def test_tracker_reports_caller_language() -> None:
    seen: list[str] = []
    await run_test(
        CallerLanguageTracker(seen.append),
        frames_to_send=[
            TranscriptionFrame("مجھے بکنگ کرنی ہے", "caller", ""),
            TranscriptionFrame("ok thanks", "caller", ""),
        ],
        expected_down_frames=[TranscriptionFrame, TranscriptionFrame],
    )
    assert seen == ["ur", "en"]


def make_router() -> tuple[ReplyLanguageRouter, FrameProcessor, FrameProcessor]:
    en, ur = FrameProcessor(name="kokoro"), FrameProcessor(name="piper-ur")
    return ReplyLanguageRouter(english_tts=en, urdu_tts=ur), en, ur


def switches(frames: list[Frame]) -> list[str]:
    return [f.service.name for f in frames if isinstance(f, ManuallySwitchServiceFrame)]


async def test_urdu_reply_switches_before_any_text() -> None:
    router, _, _ = make_router()
    down, _ = await run_test(
        router,
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame(" "),
            LLMTextFrame("جی"),
            LLMTextFrame(" ہاں"),
            LLMFullResponseEndFrame(),
        ],
    )
    types = [type(f).__name__ for f in down]
    assert types[0] == "ManuallySwitchServiceFrame"
    assert types[1:] == [
        "LLMFullResponseStartFrame",
        "LLMTextFrame",
        "LLMTextFrame",
        "LLMTextFrame",
        "LLMFullResponseEndFrame",
    ]
    assert switches(list(down)) == ["piper-ur"]
    assert router.active_language == "ur"


async def test_english_reply_needs_no_switch_and_keeps_order() -> None:
    router, _, _ = make_router()
    down, _ = await run_test(
        router,
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame("Sure"),
            LLMTextFrame(", when?"),
            LLMFullResponseEndFrame(),
        ],
    )
    assert switches(list(down)) == []
    assert [getattr(f, "text", None) for f in down if isinstance(f, LLMTextFrame)] == [
        "Sure",
        ", when?",
    ]


async def test_switches_back_to_english_next_reply() -> None:
    router, _, _ = make_router()
    down, _ = await run_test(
        router,
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame("ٹھیک ہے"),
            LLMFullResponseEndFrame(),
            LLMFullResponseStartFrame(),
            LLMTextFrame("Okay"),
            LLMFullResponseEndFrame(),
        ],
    )
    assert switches(list(down)) == ["piper-ur", "kokoro"]


async def test_greeting_speak_frame_picks_voice() -> None:
    router, _, _ = make_router()
    down, _ = await run_test(router, frames_to_send=[TTSSpeakFrame("السلام علیکم")])
    assert switches(list(down)) == ["piper-ur"]


async def test_reply_without_letters_is_released_at_end() -> None:
    router, _, _ = make_router()
    down, _ = await run_test(
        router,
        frames_to_send=[LLMFullResponseStartFrame(), LLMFullResponseEndFrame()],
    )
    assert [type(f).__name__ for f in down] == [
        "LLMFullResponseStartFrame",
        "LLMFullResponseEndFrame",
    ]


async def test_interruption_drops_held_frames() -> None:
    router, _, _ = make_router()
    down, _ = await run_test(
        router,
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame("  "),
            InterruptionFrame(),
            LLMFullResponseStartFrame(),
            LLMTextFrame("Yes?"),
            LLMFullResponseEndFrame(),
        ],
    )
    texts = [f.text for f in down if isinstance(f, LLMTextFrame)]
    assert texts == ["Yes?"]
