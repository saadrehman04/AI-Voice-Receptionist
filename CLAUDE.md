# Voice AI Receptionist

## What this is
A multi-tenant AI phone receptionist for small businesses (clinics, salons, auto workshops).
It answers calls in Urdu and English, books/reschedules/cancels appointments on Google Calendar,
answers questions from each business's knowledge base, and hands off to a human when unsure.
Calls come in through the browser (WebRTC) during development, and through the WhatsApp Business
Calling API (incoming calls only) later.

This is also my AI-engineering portfolio project, so code quality, tests, measurements and
written decisions matter as much as features.

## Roadmap
The full build plan is in docs/BUILD_GUIDE.md (phases 0-9).
Progress is tracked in docs/PROGRESS.md.
- Only work on the phase I give you. Do not start later phases.
- At the end of a phase, tick it off in docs/PROGRESS.md and add 2-4 lines on what was built and anything left open.

## Hard constraints
- FREE tools only: Pipecat, Groq (LLM + hosted Whisper STT) as the main provider, Gemini free tier as
  backup LLM and eval judge, Ollama / faster-whisper as offline fallbacks, Kokoro/Piper TTS,
  FastAPI, Postgres, Redis, Next.js. Do not add paid services without asking me.
- My laptop: Linux Mint, Intel graphics, NO NVIDIA GPU. Don't rely on local GPU models for the live path.
- Every provider (STT, LLM, TTS) sits behind an interface so it can be swapped by config.
- Latency matters: stream everything, never block the audio loop, measure each stage.
- Never invent business facts (prices, hours). If it's not in the tenant config/KB, the agent says it will check.
- Bookings must be safe: confirm details with the caller, lock the slot, idempotent writes, no double-booking.
- Secrets only from .env; never hard-code keys, never print them in logs.

## Stack and conventions
- Python 3.12, uv, ruff, mypy (strict where practical), pytest. Async everywhere in the agent.
- Next.js (App Router) + TypeScript + Tailwind for apps/web. pnpm.
- Postgres via SQLAlchemy 2 + Alembic migrations. All tables carry tenant_id.
- Structured JSON logging. Each call has a call_id; each turn has a turn_id.

## How to work
- Before coding a phase, propose a plan (files to create/change, approach, risks) and wait for my OK.
- Check the CURRENT docs/version of Pipecat and other libraries before using an API; they change often.
- Write tests with every feature. Run tests and linters before saying a task is done.
- Keep docs/ARCHITECTURE.md and docs/DECISIONS.md updated (short entries: decision, why, alternatives).
- Small commits with clear messages after each working step.
- I am learning: after each phase, give me a short explanation of the key files so I can explain them in interviews.

## Commands
Run from the repo root. `make help` lists everything.
- `make install`: `uv sync --all-packages` + `pnpm install` in apps/web; creates .env from .env.example if missing
- `make up` / `make down` / `make logs`: Postgres 16 + Redis 7 via docker compose (`up` waits for healthchecks)
- `make agent`: voice agent on :7860 (`uv run --all-packages python -m receptionist_agent`); talk at http://localhost:7860/client. Must run as `python -m` (Pipecat's runner finds `bot` on `__main__`)
- `make chat`: text chat with the receptionist (real LLM + tools, stub bookings; `ARGS="--tenant demo-workshop"`); needs GROQ_API_KEY
- `make latency`: P50/P95 per stage from logs/turns.jsonl (`ARGS="--last 50"`)
- `make bench`: latency benchmark, 20 utterances through the real pipeline per flag config -> docs/benchmarks/<date>.md (`ARGS="--configs piper clause"`, `--turns 2` for a smoke run, `--report-only`); needs GROQ_API_KEY, takes ~6 min per config
- `make traces`: start Jaeger (UI http://localhost:16686); set `OTEL_ENABLED=true` for the agent to export spans
- `make migrate`: apply Alembic migrations to DATABASE_URL (run after `make up`)
- `make worker`: background worker for post-call summaries (Redis queue)
- `make api`: FastAPI on 127.0.0.1:8000 with reload in dev (`receptionist-api`); docs at /docs
- `make web`: Next.js dev server on :3000 (`pnpm --dir apps/web dev`)
- `make test`: pytest, no network or model downloads (tests marked `models` are skipped). Needs `make up` for the Postgres/Redis tests; they skip without it (CI sets REQUIRE_SERVICES=1 so they never skip there)
- `make test-models`: opt-in smoke test of local Kokoro + faster-whisper on CPU (downloads ~0.6 GB once)
- Single test: `uv run --all-packages pytest apps/api/tests/test_health.py -k ok`
- `make lint`: ruff check, ruff format --check, mypy (strict), eslint, `next typegen && tsc --noEmit`
- `make fmt`: ruff --fix + ruff format
- `make evals`: simulated-caller eval suite (placeholder until Phase 6)
- Add a Python dependency: `uv add --package receptionist-api <pkg>` (or receptionist-agent / receptionist-shared)
- Add a web dependency: `pnpm --dir apps/web add <pkg>`
- Python packages import as `receptionist_shared`, `receptionist_agent`, `receptionist_api`, `receptionist_evals` (src layout).
