# Decisions

Short entries, newest last: what we decided, why, and what else we considered.

## D1. Monorepo with one uv workspace for all Python code (Phase 0)
- **Decision:** `packages/shared`, `apps/agent`, `apps/api` and `evals` are members of one uv workspace,
  with a single `uv.lock` and one `.venv` at the root. Tool config (ruff, mypy, pytest) lives once in the
  root `pyproject.toml`.
- **Why:** the agent and api both import `receptionist-shared`. A workspace makes that a normal dependency
  with no publishing step, keeps versions consistent, and means one `make lint` / `make test` covers everything.
- **Alternatives:** separate uv projects per app (three venvs, duplicated config, version drift);
  one flat package (no clear boundary between agent and api).

## D2. Docker for infrastructure only; our services run on the host (Phase 0)
- **Decision:** `docker-compose.yml` runs Postgres 16 and Redis 7. The agent, api and web run directly with
  `make agent` / `make api` / `make web`.
- **Why:** fast reloads, easy debugging, and the agent needs direct access to local audio/CPU models.
  The ports are bound to 127.0.0.1, so the databases are never exposed on the LAN.
- **Alternatives:** everything in compose (slower iteration on a laptop without a GPU; can be added for deployment later).

## D3. Config through one typed `Settings` class (Phase 0)
- **Decision:** `receptionist_shared.config.Settings` (pydantic-settings) is the only place env vars are read.
  Provider choices are `Literal` types, secrets are `SecretStr`, and empty values count as "unset".
- **Why:** a typo like `LLM_PROVIDER=gorq` fails at startup rather than mid-call; keys can't leak into logs by accident;
  swapping a provider is a config change.
- **Alternatives:** `os.environ` lookups scattered through the code (no validation, easy to leak secrets).

## D4. Package names are prefixed `receptionist_*` (Phase 0)
- **Decision:** import names are `receptionist_agent`, `receptionist_api`, `receptionist_shared`, `receptionist_evals`.
- **Why:** bare names like `api` or `agent` clash easily with third-party modules and confuse mypy in a monorepo.
- **Alternatives:** short names (`agent`, `api`): nicer to type, but risky.

## D5. Next.js 16 defaults kept for apps/web (Phase 0)
- **Decision:** scaffolded with `create-next-app@16.4.6` (App Router, TypeScript, Tailwind 4, ESLint, `src/`),
  keeping its defaults (`cacheComponents`, Turbopack). pnpm 12 is pinned via `packageManager`.
  `make lint` runs ESLint plus `next typegen && tsc --noEmit`.
- **Why:** staying on the defaults means current Next docs apply directly. Typegen is needed because
  layouts use the generated `LayoutProps` types.
- **Alternatives:** Vite + React (would need extra work later for the Vercel deploy and server components).

## D6. Browser calls through Pipecat's development runner + prebuilt client (Phase 1)
- **Decision:** `python -m receptionist_agent` starts `pipecat.runner.run` restricted to WebRTC
  (`-t webrtc`). It serves the prebuilt voice client at `/client` and handles signaling
  (`/start`, `/api/offer`, ICE PATCH). Our code only provides `bot(runner_args)`.
- **Why:** WebRTC signaling (offer/answer, renegotiation, trickle ICE) is fiddly and changes
  between Pipecat versions; the runner is maintained alongside the transport. It is peer-to-peer
  (aiortc), so no media server and no cost. The same runner also has WhatsApp webhook support,
  which Phase 8 can build on.
- **Alternatives:** our own FastAPI signaling server (more code to keep in sync with Pipecat);
  a custom Next.js client now (the dashboard's test-call button comes in Phase 7).

## D7. Default models (Phase 1, checked against provider model lists on 2026-11-11)
- **Decision:** LLM `llama-3.3-70b-versatile` on Groq; STT `whisper-large-v3-turbo` on Groq;
  backup LLM `gemini-3.5-flash-lite`; TTS Kokoro `af_heart`; offline STT faster-whisper `small`
  (int8 on CPU); offline LLM Ollama `llama3.2:3b`. All overridable in `.env`.
- **Why:** the 70B Llama is not a reasoning model, so the first token arrives fast, and it handles
  Urdu and Urdu-English mixing far better than 8B models. Whisper turbo is Groq's fast multilingual
  STT. Flash-Lite is Gemini's lowest-latency stable model.
- **Alternatives:** `openai/gpt-oss-20b` (faster tokens, but reasoning adds time before the first
  word); `llama-3.1-8b-instant` (kept for Phase 5's "simple turn" routing).

## D8. Latency measured by an observer, from the moment the caller stops speaking (Phase 1)
- **Decision:** `TurnTimingObserver` watches frames from the side and logs, per turn:
  VAD stop (t=0) -> final transcript -> end-of-turn release -> first LLM token -> first TTS audio
  -> bot starts speaking. One JSON line per turn in `logs/turns.jsonl`; `make latency` prints P50/P95.
- **Why:** an observer adds nothing to the audio path. Measuring from VAD stop is what the caller
  experiences as "silence before the reply". If the caller pauses and continues, the clock restarts
  at the last stop.
- **Alternatives:** Pipecat's built-in TTFB metrics (per service, not end-to-end per turn);
  OpenTelemetry spans (planned for Phase 5, alongside this file).

## D9. Local models are built in a worker thread (Phase 1)
- **Decision:** `run_bot` calls `build_services` through `asyncio.to_thread`.
- **Why:** Kokoro, Piper and faster-whisper load their models inside their constructors, which
  takes seconds of CPU. On the event loop that would freeze every other live call.
- **Alternatives:** load once at process start and share (Phase 5 pre-warming will revisit).

## D10. Urdu speech: Piper's Urdu voices; Kokoro stays for English (found in Phase 1)
- **Status:** direction chosen; per-language TTS routing is built in Phase 2.
- **Problem:** Kokoro has no Urdu (its languages: English, Spanish, French, Hindi, Italian,
  Japanese, Portuguese, Chinese).
- **Finding:** Piper's catalogue has two Urdu voices, `ur_PK-fasih-medium` (male) and
  `ur_PK-aegis_female-medium` (female). Both are MIT-licensed, 22.05 kHz, and read Urdu script
  through eSpeak's `ur` phonemizer. (The `piper-tts` engine itself is GPL-3.0, which is fine for a
  service we run ourselves rather than distribute.)
- **Plan:** English replies -> Kokoro; Urdu replies -> Piper Urdu voice, picked per reply.
- **Alternatives:** Kokoro's Hindi voice reading Urdu transliterated into Devanagari (extra
  transliteration step, odd for Urdu loanwords); Meta MMS Urdu TTS (non-commercial licence).
