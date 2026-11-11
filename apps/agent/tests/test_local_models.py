"""Opt-in smoke test for the offline speech path: Kokoro TTS -> faster-whisper STT on CPU.

Downloads ~0.6 GB of models on first run, so it is excluded from `make test` and CI.
Run it with `make test-models`. It proves the local fallbacks load and work on this
machine, and prints how long each step took.
"""

import time

import numpy as np
import pytest
import soxr
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.worker import PipelineParams
from pipecat.tests.utils import run_test

from receptionist_agent.providers import build_stt, build_tts
from receptionist_shared import Settings

pytestmark = pytest.mark.models

SENTENCE = "I would like to book a haircut for tomorrow at ten in the morning."
STT_RATE = 16000


async def test_kokoro_speech_is_transcribed_by_faster_whisper() -> None:
    settings = Settings(
        _env_file=None,
        stt_provider="faster_whisper",
        faster_whisper_model="small",
        stt_language="en",
        tts_provider="kokoro",
    )

    t0 = time.perf_counter()
    tts = build_tts(settings)
    t_tts_load = time.perf_counter() - t0

    t0 = time.perf_counter()
    down, _ = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(SENTENCE)],
        pipeline_params=PipelineParams(audio_out_sample_rate=24000),
    )
    t_tts = time.perf_counter() - t0
    chunks = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert chunks, "Kokoro produced no audio"
    rate = chunks[0].sample_rate
    pcm = np.frombuffer(b"".join(c.audio for c in chunks), dtype=np.int16)
    speech_secs = len(pcm) / rate
    pcm16k = soxr.resample(pcm.astype(np.float32), rate, STT_RATE).astype(np.int16)

    t0 = time.perf_counter()
    stt = build_stt(settings)
    t_stt_load = time.perf_counter() - t0

    chunk = STT_RATE // 50  # 20 ms frames, like a real call
    audio_frames = [
        InputAudioRawFrame(pcm16k[i : i + chunk].tobytes(), STT_RATE, 1)
        for i in range(0, len(pcm16k), chunk)
    ]
    t0 = time.perf_counter()
    down, _ = await run_test(
        stt,
        frames_to_send=[
            VADUserStartedSpeakingFrame(),
            *audio_frames,
            VADUserStoppedSpeakingFrame(),
        ],
        pipeline_params=PipelineParams(audio_in_sample_rate=STT_RATE),
    )
    t_stt = time.perf_counter() - t0
    text = " ".join(f.text for f in down if isinstance(f, TranscriptionFrame)).lower()

    print(
        f"\nkokoro load {t_tts_load:.1f}s, synth {t_tts:.2f}s for {speech_secs:.1f}s of speech"
        f"\nwhisper-small load {t_stt_load:.1f}s, transcribe {t_stt:.2f}s"
        f"\ntranscript: {text!r}"
    )
    for word in ("book", "haircut", "tomorrow"):
        assert word in text


async def test_piper_speaks_urdu_greeting() -> None:
    from receptionist_agent.providers import build_urdu_tts

    t0 = time.perf_counter()
    tts = build_urdu_tts(Settings(_env_file=None))
    t_load = time.perf_counter() - t0
    text = "السلام علیکم، نور بیوٹی سیلون میں فون کرنے کا شکریہ۔"
    t0 = time.perf_counter()
    down, _ = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame(text)],
        pipeline_params=PipelineParams(audio_out_sample_rate=24000),
    )
    t_synth = time.perf_counter() - t0
    chunks = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert chunks, "Piper produced no Urdu audio"
    secs = sum(len(c.audio) for c in chunks) / 2 / chunks[0].sample_rate
    print(f"\npiper ur_PK load {t_load:.1f}s, synth {t_synth:.2f}s for {secs:.1f}s of speech")
    assert secs > 1.0
