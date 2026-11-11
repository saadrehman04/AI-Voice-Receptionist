import json
from pathlib import Path
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from receptionist_agent.timing import JsonlTurnWriter, TurnTiming, TurnTimingObserver

MS = 1_000_000
_P = FrameProcessor()


def push(frame: Frame, at_ms: float, *, first: bool = True) -> FramePushed:
    return FramePushed(
        source=_P,
        destination=_P,
        frame=frame,
        direction=FrameDirection.DOWNSTREAM,
        timestamp=int(at_ms * MS),
        first_push=first,
    )


def transcript(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text=text, user_id="caller", timestamp="")


def audio() -> TTSAudioRawFrame:
    return TTSAudioRawFrame(audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1)


def make_observer() -> tuple[TurnTimingObserver, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []

    async def on_turn(record: dict[str, Any]) -> None:
        records.append(record)

    return TurnTimingObserver(call_id="call-1", on_turn=on_turn), records


async def feed(obs: TurnTimingObserver, events: list[FramePushed]) -> None:
    for e in events:
        await obs.on_push_frame(e)


async def test_full_turn_records_every_stage() -> None:
    obs, records = make_observer()
    await feed(
        obs,
        [
            push(VADUserStoppedSpeakingFrame(), 1000),
            push(transcript("I want to book"), 1300),
            push(UserStoppedSpeakingFrame(), 1350),
            push(LLMTextFrame("Sure"), 1650),
            push(LLMTextFrame(", when?"), 1700),
            push(audio(), 1900),
            push(audio(), 1950),
            push(BotStartedSpeakingFrame(), 1960),
        ],
    )
    (r,) = records
    assert r["call_id"] == "call-1"
    assert r["turn_index"] == 1
    assert r["stt_final"] == 300.0
    assert r["llm_first_token"] == 650.0
    assert r["tts_first_audio"] == 900.0
    assert r["stages"] == {
        "stt": 300.0,
        "end_of_turn": 350.0,
        "llm": 300.0,  # from turn release (later than transcript) to first token
        "tts": 250.0,
        "playout": 60.0,
        "total": 960.0,
    }
    assert obs.current_turn is None


async def test_pause_mid_sentence_restarts_the_clock() -> None:
    obs, records = make_observer()
    await feed(
        obs,
        [
            push(VADUserStoppedSpeakingFrame(), 0),
            push(transcript("I want"), 200),
            push(VADUserStoppedSpeakingFrame(), 2000),  # caller carried on, stopped again
            push(transcript("to book a haircut"), 2250),
            push(UserStoppedSpeakingFrame(), 2300),
            push(LLMTextFrame("Sure"), 2500),
            push(audio(), 2700),
            push(BotStartedSpeakingFrame(), 2710),
        ],
    )
    (r,) = records
    assert r["stages"]["stt"] == 250.0
    assert r["stages"]["total"] == 710.0
    assert r["transcript_chars"] == len("I want to book a haircut")


async def test_greeting_without_caller_turn_is_ignored() -> None:
    obs, records = make_observer()
    await feed(obs, [push(audio(), 10), push(BotStartedSpeakingFrame(), 20)])
    assert records == []


async def test_repeat_pushes_are_ignored() -> None:
    obs, records = make_observer()
    await feed(
        obs,
        [
            push(VADUserStoppedSpeakingFrame(), 0),
            push(transcript("hi"), 100),
            push(UserStoppedSpeakingFrame(), 110),
            push(LLMTextFrame("Hello"), 400),
            push(LLMTextFrame("again"), 50, first=False),  # same frame passed along later
            push(audio(), 600),
            push(BotStartedSpeakingFrame(), 610),
        ],
    )
    assert records[0]["llm_first_token"] == 400.0


async def test_abandoned_turn_is_dropped_and_next_turn_counts() -> None:
    obs, records = make_observer()
    await feed(
        obs,
        [
            push(VADUserStoppedSpeakingFrame(), 0),
            push(transcript("hello"), 100),
            push(UserStoppedSpeakingFrame(), 120),
            push(LLMTextFrame("Hi"), 300),
            # caller talks over us before any audio played -> new turn
            push(VADUserStoppedSpeakingFrame(), 1000),
            push(transcript("sorry, one more thing"), 1200),
            push(UserStoppedSpeakingFrame(), 1220),
            push(LLMTextFrame("Of course"), 1500),
            push(audio(), 1700),
            push(BotStartedSpeakingFrame(), 1710),
        ],
    )
    (r,) = records
    assert r["turn_index"] == 2
    assert r["stages"]["total"] == 710.0


def test_record_without_bot_start_uses_tts_audio_for_total() -> None:
    t = TurnTiming(call_id="c", turn_index=1, user_stopped_ns=0)
    t.stt_final_ns = 100 * MS
    t.llm_first_token_ns = 300 * MS
    t.tts_first_audio_ns = 500 * MS
    stages = t.to_record()["stages"]
    assert stages["total"] == 500.0
    assert stages["playout"] is None
    assert stages["end_of_turn"] is None


async def test_writer_appends_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "turns.jsonl"
    writer = JsonlTurnWriter(path)
    await writer.write({"turn_id": "a", "stages": {"total": 1.0}})
    await writer.write({"turn_id": "b", "text": "اردو"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["turn_id"] for line in lines] == ["a", "b"]
    assert "اردو" in lines[1]
