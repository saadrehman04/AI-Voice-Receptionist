# Architecture

Status: Phase 2 (receptionist brain with stub tools). The api, real calendar and dashboard are still planned.

## Big picture

```mermaid
flowchart LR
    caller([Caller<br/>browser WebRTC<br/>later: WhatsApp call])

    subgraph agent["apps/agent - Pipecat voice agent (Python)"]
        direction LR
        vad[VAD<br/>Silero] --> stt[STT<br/>Groq Whisper<br/>| faster-whisper]
        stt --> llm[LLM<br/>Groq<br/>| Gemini | Ollama]
        llm --> tts[TTS<br/>Kokoro<br/>| Piper]
    end

    subgraph api["apps/api - FastAPI (Python)"]
        tools[tools: availability,<br/>booking, messages]
        records[call records,<br/>tenants, auth]
    end

    pg[(Postgres 16)]
    redis[(Redis 7<br/>slot locks, job queue)]
    gcal[[Google Calendar]]
    web["apps/web - Next.js<br/>owner dashboard"]

    caller <-->|audio| vad
    tts -->|audio| caller
    llm -->|tool calls over HTTP| tools
    agent -->|turns, timings| records
    tools --> pg
    tools --> redis
    tools --> gcal
    records --> pg
    web -->|REST| api
```

## Components

| Path | What it is | Phase |
| --- | --- | --- |
| `apps/agent` | Pipecat pipeline: audio in -> VAD -> STT -> LLM -> TTS -> audio out. Each provider sits behind an interface picked by config. | 1-2, 5 |
| `apps/api` | FastAPI. Owns all state: tenants, bookings, calls, messages. The agent calls it over HTTP for tools. | 3-4 |
| `apps/web` | Next.js dashboard for business owners, and the browser test-call page. | 1, 7 |
| `packages/shared` | Python code both agent and api import: `Settings` (env config) now; tenant config schemas and API models later. | 0+ |
| `evals` | Simulated callers that run scenarios against the agent in text mode and score the result. | 6 |
| `tenants` | Example business configs (YAML), validated by `packages/shared`. | 2 |
| `docker-compose.yml` | Local Postgres and Redis only. Our own services run on the host. | 0 |

## Key flows

- **Config:** every process reads `.env` through `receptionist_shared.get_settings()`. Secrets are
  `SecretStr`, so they show as `**********` if a Settings object is ever logged.
- **A call:** `make agent` starts Pipecat's runner on :7860. The browser client at `/client` POSTs
  `/start`, then exchanges an SDP offer/answer, and audio flows peer-to-peer (aiortc). The runner calls
  `receptionist_agent.bot.bot()` once per connection, which builds the services (in a thread, since
  local models load slowly), wires the pipeline and runs it until the caller hangs up.

  ```text
  transport.input -> STT -> caller language -> user aggregator -> LLM
      -> reply language router -> TTS switcher (Kokoro | Piper Urdu) -> transport.output
      -> assistant aggregator
  ```

  The user aggregator decides when the caller has finished (VAD plus the Smart Turn v3 ONNX model on
  CPU) and adds the transcript to the shared `LLMContext`; the assistant aggregator records what the
  bot actually said. `TurnTimingObserver` watches frames from the side and writes one JSON line per
  turn to `logs/turns.jsonl`.
- **The brain (apps/agent/.../brain/):** per call we load the tenant YAML, build the system prompt
  from its facts (`prompt.py`), and create a `CallState` and `ReceptionistTools`. The LLM calls tools;
  tools validate input, go through `CallState` (which enforces read-back-then-confirm, see D11) and
  read/write through a `BookingBackend` (an in-memory stub now; the api in Phase 3).
  `knowledge.py` answers only from config. `text_session.py` runs the same brain without audio
  (`make chat`, and the Phase 6 evals).
- **Languages:** `CallerLanguageTracker` notes the caller's language from each transcript (Urdu or
  Hindi script -> Urdu); `ReplyLanguageRouter` holds each reply until its first letter shows its
  script, then switches the TTS (Urdu -> Piper `ur_PK`, else Kokoro) before any text is spoken.
- **Logging:** loguru everywhere (Pipecat uses it too), one JSON object per line, with `call_id` and
  `turn_id` at the top level (see `receptionist_shared.logging`).
- **A booking (from Phase 3):** the LLM calls a tool, the agent calls the api, and the api locks the slot
  in Redis, re-checks Google Calendar, writes the event with an idempotency key, and stores it in Postgres.

## Ports (local)

| Service | Port |
| --- | --- |
| api | 8000 |
| agent | 7860 |
| web | 3000 |
| Postgres | 5432 (localhost only) |
| Redis | 6379 (localhost only) |
