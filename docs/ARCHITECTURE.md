# Architecture

Status: Phase 4 (multi-tenant backend with owner auth, call records and post-call analysis). The dashboard UI comes in Phase 7.

## Big picture

```mermaid
flowchart LR
    caller([Caller<br/>browser WebRTC<br/>later: WhatsApp call])

    subgraph agent["apps/agent - Pipecat voice agent (Python)"]
        direction LR
        vad[VAD<br/>Silero] --> stt[STT<br/>Groq Whisper<br/>| faster-whisper]
        stt --> llm[LLM<br/>Groq gpt-oss + fallback<br/>| Gemini | Ollama]
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
| `apps/api` | FastAPI. Owns all state: tenants, bookings, messages (calls in Phase 4). Serves the agent's tools over HTTP. | 3-4 |
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
      -> [tool filler] [full-reply buffer] -> reply language router
      -> TTS switcher (English voice | Urdu voice) -> transport.output -> assistant aggregator
  ```

  The bracketed stages come from the latency flags (`speech_stage`, see below).

  The user aggregator decides when the caller has finished (VAD plus the Smart Turn v3 ONNX model on
  CPU) and adds the transcript to the shared `LLMContext`; the assistant aggregator records what the
  bot actually said. `TurnTimingObserver` watches frames from the side and writes one JSON line per
  turn to `logs/turns.jsonl`.
- **The brain (apps/agent/.../brain/):** per call we load the tenant YAML, build the system prompt
  from its facts (`prompt.py`), and create a `CallState` and `ReceptionistTools`. The LLM calls tools;
  tools validate input, go through `CallState` (which enforces read-back-then-confirm, see D11) and
  read/write through a `BookingBackend` (an in-memory stub now; the api in Phase 3).
  `knowledge.py` answers only from config. `text_session.py` runs the same brain without audio
  (`make chat`, and the Phase 6 evals). A commit (`caller_confirmed=true`) also needs a caller turn
  after the read-back, counted from the LLM context (D24).
- **LLM fallback (D23):** the Groq LLM's OpenAI client sends through `FallbackTransport`
  (`llm_fallback.py`). Each request goes to the first target with free-tier budget left
  (gpt-oss-120b -> gpt-oss-20b -> Gemini Flash-Lite via its OpenAI-compatible endpoint), and moves
  on at once on a 429/5xx, so the pipeline always sees one ordinary streamed reply. For Gemini it
  adds placeholder thought signatures and repairs the tool-call stream.
- **Languages:** `CallerLanguageTracker` notes the caller's language from each transcript (Urdu or
  Hindi script -> Urdu); `ReplyLanguageRouter` holds each reply until its first letter shows its
  script, then switches the TTS (Urdu -> Piper `ur_PK`, else Kokoro) before any text is spoken.
- **Latency (Phase 5, `latency.py`):** each feature is a flag so `make bench` can compare them
  (D25-D28). `TTS_TEXT_CHUNKING=clause` lets a reply's first chunk end at a comma, so speech starts
  before the first sentence is finished. `ToolFiller` says "One moment." if a tool call runs past
  `FILLER_AFTER_MS` with nothing said yet, and keeps it out of the LLM context. Connections are
  pre-warmed at call start. Short yes/no turns go to a smaller model first (`llm_fallback`). The
  api caches availability for 30 s; holds and the pre-write re-check never use the cache.
- **Voices:** `TTS_PROVIDER` picks the English voice (Kokoro, Piper or Azure) and
  `URDU_TTS_PROVIDER` the Urdu one (Piper or Azure; `auto` follows Azure). Pipecat's Azure service
  streams with word timestamps (D30).
- **Barge-in:** Pipecat's VAD turn-start strategy interrupts the reply when the caller talks; the
  output transport drops queued audio, and the assistant aggregator keeps only sentences whose
  audio was played (`test_barge_in.py` checks this without models; the benchmark with them).
- **Benchmark (`bench/`, `make bench`):** 20 caller utterances (synthetic voices, or recordings
  in `data/bench/recorded/`) are played in real time through the real pipeline over a fake
  transport (`ReplayTransport`). Time zero is the true end of the caller's speech. Writes
  `docs/benchmarks/<date>.md`.
- **Tracing:** with `OTEL_ENABLED=true`, Pipecat's conversation/turn/service spans plus ours
  (`tool <name>`, `turn latency` with an event per stage) go over OTLP/HTTP to Jaeger
  (`make traces`, UI :16686).
- **Logging:** loguru everywhere (Pipecat uses it too), one JSON object per line, with `call_id` and
  `turn_id` at the top level (see `receptionist_shared.logging`).
- **A booking:** the LLM calls `check_availability`; the agent's `ApiBackend` asks the api, which
  merges calendar busy times, confirmed bookings and other callers' holds, and returns at most three
  options. At read-back (`create_booking`, `caller_confirmed=false`) the api takes a 2-minute Redis
  hold on the slot. After the caller's yes, `POST /bookings` checks the hold, re-checks calendar + DB,
  inserts the row (Postgres exclusion constraint = final guard), creates the Google event inside the
  same transaction, commits, and releases the hold. See DECISIONS D14-D16.

  ```text
  agent tool --HTTP + X-Agent-Token--> api /v1/tenants/{slug}/...
      availability : Calendar.busy + bookings table + Redis holds -> free_slots -> 3 options
      holds        : Lua all-or-nothing SET NX on 15-min buckets (PX 120 s)
      bookings     : idempotency key -> hold -> re-check -> INSERT (EXCLUDE gist) -> calendar event
      messages     : messages table (kind = message | handoff)
      knowledge    : tenant YAML facts, then markdown sections by embedding similarity
  ```
- **Call records:** the agent's recorder sends call start, each caller/agent turn (agent turns
  carry that reply's latency breakdown), each tool call and call end to the api from a background
  task (never on the audio path). Ending a call queues a `summarize_call` job; `make worker` takes it
  from Redis, computes the outcome from tool calls and asks the LLM for summary/intent/sentiment.
- **Owners:** `POST /v1/onboarding` creates a business (validated config) + owner account and returns
  a JWT; `POST /v1/auth/login`. Dashboard routes under `/v1/dashboard/...` take the tenant only from
  the token (calls + transcript + tool calls, bookings, messages, stats with P50/P95 latency,
  settings). See D18.
- **Database (Alembic, `make migrate`):** `tenants` (config JSONB), `users`, `services`, `calls`,
  `turns`, `tool_calls`, `bookings` (no-overlap exclusion constraint), `messages`. Every row has
  `tenant_id`.

  ```mermaid
  erDiagram
      tenants ||--o{ users : owns
      tenants ||--o{ services : offers
      tenants ||--o{ calls : receives
      calls ||--o{ turns : has
      calls ||--o{ tool_calls : made
      tenants ||--o{ bookings : has
      tenants ||--o{ messages : has
  ```

## Ports (local)

| Service | Port |
| --- | --- |
| api | 8000 |
| agent | 7860 |
| web | 3000 |
| Postgres | 5432 (localhost only) |
| Redis | 6379 (localhost only) |
| Jaeger UI / OTLP (optional, `make traces`) | 16686 / 4318 (localhost only) |
