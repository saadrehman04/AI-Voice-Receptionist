# apps/agent (`receptionist_agent`)

Pipecat voice agent. `make agent`, then open http://localhost:7860/client and allow the microphone.

| File | What it does |
| --- | --- |
| `__main__.py` | Starts Pipecat's development runner (WebRTC only) and exposes `bot` to it. |
| `bot.py` | One call: builds services, wires the pipeline, runs it until hang-up. |
| `providers.py` | Config -> concrete STT / LLM / TTS service. The only vendor-specific code. |
| `prompts.py` | System prompt and greeting. |
| `timing.py` | `TurnTimingObserver`: per-turn stage timestamps -> `logs/turns.jsonl`. |
| `latency_report.py` | `make latency`: P50/P95 per stage. |

Tests: `make test` (fast, no network) and `make test-models` (real Kokoro + faster-whisper on CPU).
