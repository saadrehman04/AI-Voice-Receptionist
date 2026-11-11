# Architecture

Status: Phase 0 (scaffold). Boxes marked *(planned)* do not exist in code yet.

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
- **A call (from Phase 1):** the browser sends audio over WebRTC to the agent. The agent streams it
  through STT -> LLM -> TTS and streams audio back. Each turn logs stage timings.
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
