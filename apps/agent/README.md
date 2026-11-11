# apps/agent (`receptionist_agent`)

Pipecat voice agent. `make agent`, then open http://localhost:7860/client and allow the microphone.

| File | What it does |
| --- | --- |
| `__main__.py` | Starts Pipecat's development runner (WebRTC only) and exposes `bot` to it. |
| `bot.py` | One call: builds services, wires the pipeline, runs it until hang-up. |
| `providers.py` | Config -> concrete STT / LLM / TTS service. The only vendor-specific code. |
| `brain/prompt.py` | System prompt built from the tenant YAML; greeting. |
| `brain/state.py` | `CallState`: caller details + the read-back-then-confirm rules. |
| `brain/tools.py` | The 8 tools (schemas + handlers) and their Pipecat registration. |
| `brain/backend.py` | `BookingBackend` interface + in-memory stub. |
| `brain/knowledge.py` | Answers questions strictly from tenant config. |
| `brain/text_session.py` | The same brain without audio (`make chat`, evals). |
| `language.py` | Urdu/English detection and per-reply TTS voice routing. |
| `chat.py` | `make chat`. |
| `testing.py` | `ScriptedLLM` for offline, deterministic conversation tests. |
| `timing.py` | `TurnTimingObserver`: per-turn stage timestamps -> `logs/turns.jsonl`. |
| `latency_report.py` | `make latency`: P50/P95 per stage. |

Tests: `make test` (fast, no network) and `make test-models` (real Kokoro + faster-whisper on CPU).
