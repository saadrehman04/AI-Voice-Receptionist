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
- **Status:** built in Phase 2 (`language.py`): a router before a Pipecat `ServiceSwitcher`
  picks the voice per reply from its script.
- **Problem:** Kokoro has no Urdu (its languages: English, Spanish, French, Hindi, Italian,
  Japanese, Portuguese, Chinese).
- **Finding:** Piper's catalogue has two Urdu voices, `ur_PK-fasih-medium` (male) and
  `ur_PK-aegis_female-medium` (female). Both are MIT-licensed, 22.05 kHz, and read Urdu script
  through eSpeak's `ur` phonemizer. (The `piper-tts` engine itself is GPL-3.0, which is fine for a
  service we run ourselves rather than distribute.)
- **Plan:** English replies -> Kokoro; Urdu replies -> Piper Urdu voice, picked per reply.
- **Alternatives:** Kokoro's Hindi voice reading Urdu transliterated into Devanagari (extra
  transliteration step, odd for Urdu loanwords); Meta MMS Urdu TTS (non-commercial licence).

## D11. Booking safety is enforced in code, not only in the prompt (Phase 2)
- **Decision:** every write tool (create, reschedule, cancel) is two calls. The first
  (`caller_confirmed=false`) validates everything and returns a read-back; `CallState` remembers
  exactly what was read back. The second (`caller_confirmed=true`) only goes through if the details
  are identical. A slot can only be booked if `check_availability` offered it in this call, and the
  backend write carries an idempotency key.
- **Why:** an LLM can skip a prompt instruction; it cannot skip a check in the tool. This turns
  "always read back and confirm" and "never offer a time we didn't check" into guarantees, which is
  what makes it safe to let an LLM write to a real calendar.
- **Alternatives:** prompt-only rules (cheaper, but untestable and occasionally ignored); a single
  `confirm_booking` tool (the model could call it without a read-back).

## D12. Tenant facts: core facts in the prompt, everything else through a tool (Phase 2)
- **Decision:** each business is a YAML file validated by `TenantConfig` (strict: unknown keys
  rejected, slug must match file name, overlapping hours rejected). Services, prices, hours and
  policies go into the system prompt; FAQs (and the Phase 3 knowledge base) are answered by
  `lookup_business_info`, which returns `found=false` rather than guessing.
- **Why:** the facts needed on most turns cost no tool round trip; the long tail can grow without
  bloating every request. A missing price is `null` in YAML and the prompt says "never estimate".
- **Alternatives:** everything in the prompt (simple, but grows with every FAQ); everything via tools
  (an extra LLM round trip on the commonest questions).

## D13. A scripted LLM for tests and evals (Phase 2)
- **Decision:** `receptionist_agent.testing.ScriptedLLM` subclasses Pipecat's OpenAI LLM service and
  only replaces the network call with a Python function. Conversation tests run through Pipecat's
  real context aggregators, tool registration and function-call frames.
- **Why:** deterministic, offline, fast tests of the whole brain (a full booking conversation runs
  in under a second), while still catching wiring mistakes a pure unit test would miss.
- **Alternatives:** mocking the LLM client (brittle, tied to the OpenAI wire format); only live-LLM
  evals (slow, rate-limited, non-deterministic; they come in Phase 6 on top of this).

## D14. Tools live in the api; the agent calls them over HTTP (Phase 3)
- **Decision:** availability, holds, bookings, messages and knowledge lookups are api endpoints
  (`/v1/tenants/{slug}/...`), authenticated with a shared `X-Agent-Token`. The agent's
  `ApiBackend` implements the same `BookingBackend` interface as the in-memory stub, so tools,
  tests and `make chat` didn't change. Request/response models live in `receptionist_shared`.
- **Why:** one place owns state (DB, Redis, calendar), so several agent processes (and later the
  WhatsApp bridge) share one source of truth. The agent stays stateless and restartable.
- **Alternatives:** the agent talking to Postgres and Google directly (every agent process holds DB
  connections and credentials; logic duplicated when the dashboard needs the same rules).

## D15. Double booking is impossible, not just unlikely (Phase 3)
- **Decision:** four layers. (1) A Redis hold on every 15-minute bucket the slot plus buffer touches,
  taken at read-back, 2-minute expiry, acquired atomically with a Lua script. (2) Idempotency keys
  (unique per tenant in Postgres; also used as the Google event id). (3) A fresh calendar + DB check
  right before writing. (4) A Postgres exclusion constraint:
  `EXCLUDE USING gist (tenant_id WITH =, guard WITH &&) WHERE status = 'confirmed'`, where
  `guard` is the booking widened by half the buffer on each side.
- **Why:** layers 1-3 make conflicts rare and give the caller a friendly "someone is booking that
  right now"; layer 4 is the guarantee, enforced by the database under any concurrency. The race
  tests run five simultaneous bookings for one slot, and a variant that skips Redis and the re-check
  entirely, against real Postgres: exactly one wins every time.
- **Alternatives:** a Redis lock alone (fails if Redis restarts or a lock expires mid-write);
  `SELECT ... FOR UPDATE` on a slots table (needs pre-generated slot rows; awkward with variable
  service lengths); serializable transactions (retries everywhere).

## D16. Calendar write inside the DB transaction, with compensation (Phase 3)
- **Decision:** insert the booking row (the constraint fires), then create the Google event, then
  commit. If Google fails, the row rolls back. If the commit fails after the event exists, we delete
  the event. If the calendar is unreachable, nothing is offered or written.
- **Why:** the two systems can't share a transaction, so we order the steps so every failure leaves
  them consistent, and we fail toward "no booking + take a message", never toward a phantom booking.
- **Alternatives:** an outbox table + worker (more robust under crashes; worth it when calendar
  writes become slow or batched. Noted for Phase 9).

## D17. Knowledge base: YAML facts first, then local multilingual embeddings (Phase 3)
- **Decision:** `lookup_business_info` answers from the tenant YAML first (exact), then from
  `tenants/kb/<slug>/*.md` split by heading and searched with
  `paraphrase-multilingual-MiniLM-L12-v2` via fastembed (ONNX on CPU, 0.22 GB, Apache-2.0).
  Below a similarity threshold it returns `found=false`. If the model is off or missing, sections
  are ranked by word overlap.
- **Why:** free, offline, small enough for a laptop, and multilingual, so an Urdu question can
  match an English note. In-memory vectors are plenty for a few dozen sections per tenant.
- **Alternatives:** pgvector (worth it at thousands of chunks); a hosted embedding API (cost, and
  an extra network hop on the call path); keyword-only search (misses paraphrases).
