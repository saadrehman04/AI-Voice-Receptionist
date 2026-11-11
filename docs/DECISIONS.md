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

## D7. Default models (Phase 1) - LLM choice replaced by D23
> The Llama model below came from Groq's docs, checked without a key. The live model list
> (with a key) showed Groq had retired it; see D23.

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

## D18. Tenant isolation by construction: the tenant comes only from the verified token (Phase 4)
- **Decision:** owner JWTs (HS256, 12 h) carry the tenant id. Every dashboard route reads the tenant
  from the token and every query filters on `tenant_id`; no route accepts a tenant id or slug from
  the client. Records of another tenant are simply not found (404), not "forbidden" (no existence
  leak). Agent routes use a separate shared token and are scoped by slug; an owner token can't call
  them and vice versa. Secrets shorter than 32 characters are rejected at startup.
- **Why:** isolation bugs usually come from trusting a client-supplied id. Removing that input
  removes the bug class. The isolation tests create two businesses through real onboarding and try
  every read and write across the boundary.
- **Alternatives:** Postgres row-level security (strong, worth adding as defence in depth later;
  more moving parts with a pooled async engine); one schema per tenant (heavy for a few businesses).

## D19. Tenant config moves into Postgres; YAML becomes seed data (Phase 4)
- **Decision:** `tenants.config` holds the validated `TenantConfig` as JSONB (plus a `services`
  table mirror for reporting). Seed YAML is inserted once if missing and never overwrites owner
  edits. The agent fetches config from `GET /v1/tenants/{slug}/config` when it uses the api.
- **Why:** owners must be able to change prices and hours from the dashboard (Phase 7), and new
  businesses sign up through onboarding, so the files can't stay the source of truth.
- **Alternatives:** fully normalised tables for hours/services/FAQ (more joins and migrations for
  data that is always read whole); keeping YAML (no self-service).

## D20. Our own small Redis job queue instead of arq (Phase 4)
- **Decision:** `jobs.py`, the "reliable queue" pattern: LPUSH, BLMOVE to a processing list, ack by
  LREM, retries then a dead-letter list, and recovery of a crashed worker's jobs at start-up.
  `make worker` runs post-call analysis from it.
- **Why:** arq and saq both pin the Redis client to an older major version than ours. The pattern is
  ~100 lines, fully tested against real Redis, and easy to explain.
- **Alternatives:** downgrade redis-py for arq (blocks other upgrades); Celery (heavy for one job type).

## D21. Post-call analysis: facts from tools, words from the LLM (Phase 4)
- **Decision:** the call outcome (booked / rescheduled / cancelled / message / handoff / no action)
  is computed from successful tool calls. Only the summary, intent and sentiment come from the LLM
  (Groq, JSON mode, validated); a heuristic covers no key or LLM failure.
- **Why:** owners trust "booked" because it is literally what the system did. The LLM can't mislabel
  a failed booking as booked.
- **Alternatives:** asking the LLM for the outcome too (cheaper code, occasionally wrong facts).

## D22. Call records never block the call (Phase 4)
- **Decision:** the agent's `ApiCallRecorder` queues records in memory and a background task sends
  them in order with 3 retries; on hang-up it flushes with a 10 s cap. Writes are idempotent on the
  api side. Optional stereo recording (caller left, agent right) is written once at hang-up, with a
  spoken "this call may be recorded and you are speaking with an AI assistant" first.
- **Why:** a slow or down api must never add latency or end a live call; losing a log line is
  acceptable, freezing a caller is not.
- **Alternatives:** synchronous writes per turn (latency on the audio path); writing everything at
  hang-up only (a crash loses the whole call).

## D23. Main LLM gpt-oss-120b on Groq, with an HTTP-level fallback for free-tier limits (Phase 2)
- **Decision:** `openai/gpt-oss-120b` with `reasoning_effort=low` as the main LLM. Every request
  goes through `FallbackTransport` (llm_fallback.py), an httpx2 transport under the OpenAI client:
  on a 429, 5xx or connection error it re-sends the same request at once to the next target
  (`LLM_FALLBACKS`, default Groq gpt-oss-20b, then Gemini 3.5 Flash-Lite through Google's
  OpenAI-compatible endpoint). It skips a target while its `retry-after` or Groq's token-budget
  headers say it is exhausted.
- **Why (measured 2026-11-11):** Groq had retired Llama; the tool-capable models left are gpt-oss
  and Qwen. gpt-oss-120b: first token about 0.45 s, correct tool calls, natural Urdu. But Groq's
  free tier is 8,000 tokens/min *per model*, our request is about 1,500 tokens, and a tool turn
  makes 2-3 requests. One model ran dry after 1-2 turns and the client then slept on
  `retry-after` (10-25 s turns). Gemini Flash-Lite has room but is slower (1.2-6 s to first
  token). The fallback keeps the fast model for most turns and never leaves the caller in silence:
  4/4 English and Urdu booking runs passed back to back with turns of mostly 1.3-3.3 s.
- **Why below the client:** the pipeline sees one normal streamed reply, so no turn is lost.
  Pipecat's `ServiceSwitcher` failover only switches after a service is marked unusable and does
  not re-send the request that failed.
- **Gemini repairs (found live):** Gemini 3 rejects earlier tool calls without a
  `thought_signature`, so we add Google's documented placeholder. Its streamed tool calls have no
  `index`, which made Pipecat invent an empty-named call and then silenced the call, so the
  transport rewrites the stream. An end-to-end test through the real OpenAI SDK and Pipecat
  guards this.
- **Alternatives:** Gemini as main (better instruction-following in our runs, but 3-4x slower to
  first token); rotating between Groq models round-robin (wastes the best model); waiting on
  `retry-after` (dead air); a paid Groq tier (not allowed by the free-tools rule).

## D24. A booking commit needs a new caller turn since the read-back (Phase 2)
- **Decision:** `CallState.require_prepared` refuses `caller_confirmed=true` unless the caller has
  spoken since the read-back. The tool glue counts caller messages in the LLM context
  (`count_caller_turns`), so the count is the conversation itself, not a guess.
- **Why:** in the first live run gpt-oss-120b read the details back and confirmed them in the same
  turn, inventing the caller's "yes". The prompt already forbade it; only code can guarantee it.
  This applies to book, reschedule and cancel.
- **Alternatives:** prompt wording only (the model ignored it); one tool call per LLM response
  (hurts normal multi-tool turns).

## D25. Fixes the first end-to-end voice runs found (Phase 5)
- **Decision:** `AutoLanguageGroqSTTService` leaves `language` out of the Whisper request when
  `STT_LANGUAGE` is unset; Kokoro gets a 12 s "no audio yet" timeout instead of Pipecat's 3 s;
  `lookup_business_info` results carry `ok: true`.
- **Why:** Pipecat 1.15's Groq STT asserts a language and defaults to English, so with
  auto-detect every transcription failed, and forcing English would garble Urdu callers. Kokoro
  synthesises a whole sentence before sending audio, at about real time on this CPU, so any
  sentence over ~3 s was dropped as "no audio" (the greeting among them). Lookups without `ok`
  were logged as failed tool calls in call records. None of this showed in unit tests with
  fakes; the benchmark runs the real services, which is why it exists.
- **Alternatives:** fixing the language per tenant (breaks mixed Urdu/English callers);
  shorter sentences for Kokoro (the prompt can't guarantee length).

## D26. Short yes/no turns try a smaller model first (Phase 5)
- **Decision:** when the caller's latest message is at most five words, all from a small
  yes/no/thanks vocabulary (English and Urdu), the fallback transport tries
  `LLM_SIMPLE_TURN_MODEL` (gpt-oss-20b) first for that turn, tool follow-ups included.
- **Why:** these turns need no reasoning ("Yes, that's right" -> confirm the read-back), and
  the smaller model answers faster. Using it also saves the main model's per-minute token
  budget for the turns that need it. It's safe because the booking rules are enforced in code
  (D11, D24), not by the model.
- **Alternatives:** an LLM classifier to decide (costs a request to save one); routing by
  intent from CallState (the state lags the turn by one).

## D27. Availability is cached for 30 s, without weakening booking safety (Phase 5)
- **Decision:** the api caches the calendar + DB busy times per tenant and day for
  `AVAILABILITY_CACHE_SECS`. Holds are always read live, every write clears the tenant's
  cache, and the re-check right before a write never uses the cache.
- **Why:** a caller asking "what about Thursday? Friday?" shouldn't wait for Google Calendar
  each time. A stale entry can at worst offer a slot that the write then refuses with
  alternatives (tested: the owner blocks a slot in the calendar, the cache still offers it,
  the booking is refused).
- **Alternatives:** no cache (slower, simpler); invalidation by Google push notifications
  (needs a public webhook; later, with Phase 8's public endpoint).

## D28. Traces: Pipecat's OpenTelemetry spans + ours, viewed in Jaeger (Phase 5)
- **Decision:** `OTEL_ENABLED=true` turns on Pipecat's spans (conversation > turn > STT / LLM /
  TTS) and ours (`tool <name>`, and `turn latency` with an event per stage), exported over
  OTLP/HTTP. `make traces` starts Jaeger 2.22 (docker-compose profile, in-memory) at :16686.
- **Why:** a trace shows one slow turn end to end, which P50/P95 tables can't. Jaeger is one
  container. The guide's optional Langfuse needs Postgres + ClickHouse + Redis, too heavy for
  this laptop just to look at spans. With tracing off, the OpenTelemetry API is a no-op.
- **Alternatives:** Langfuse (LLM-centric views, heavy); logs only (no span tree).

## D29. Evals: an LLM caller against the real brain, scored on state (Phase 6)
- **Decision:** each scenario (YAML: tenant, frozen clock, persona, goal, facts, seeded
  bookings, expectations) runs the real agent (prompt, tools, `CallState`, LLM fallback) against
  a simulated caller LLM, in text (`TextSession`) or as speech (`--audio`, the benchmark's
  replay transport). Scorers check the backend's final state, the tool calls and the
  transcript. A Gemini judge with a strict rubric looks for invented facts, alongside a rule
  check that every price-like number comes from the tenant's facts.
- **Why:** an agent can say "you're booked" without booking, so we score what happened, not
  what was said. The caller and judge run Gemini first, so they don't use up the agent's Groq
  budget. A frozen clock and seeded bookings make "next Saturday" and "slot full" the same on
  every run.
- **CI:** `.github/workflows/evals.yml` runs the 15-scenario `ci` subset on agent PRs with one
  retry for failed scenarios, and fails if booking accuracy drops more than 2 points below
  `evals/baseline.json`. One scenario is about 7 points of the subset, so in practice any new
  booking failure that survives a retry blocks the PR. It needs the API keys as repository
  secrets; without them the job skips with a notice.
- **Alternatives:** scripted callers (no variety, can't handle a clarifying question);
  recorded LLM replies (deterministic and free, but any prompt change invalidates them, which
  is exactly when the evals must run live); judging the outcome with the LLM (it can be fooled
  the same way the agent can).

## D30. Azure AI Speech as an optional voice for English and Urdu (Phase 5)
- **Decision:** `TTS_PROVIDER=azure` speaks English with `AZURE_VOICE_EN` (default
  en-US-JennyNeural). `URDU_TTS_PROVIDER=auto` then also uses Azure for Urdu
  (`AZURE_VOICE_UR`, default ur-PK-UzmaNeural; ur-PK-AsadNeural is the male voice). Any mix
  works, e.g. Piper English with Azure Urdu. Keys come only from `.env`.
- **Why:** Piper's Urdu voice sounds robotic and Kokoro is too slow on this CPU (D25). Azure is
  the one provider we found with natural Pakistani Urdu and English voices on a monthly free
  tier (F0, about 0.5M characters). Its streaming TTS reports word timestamps, so barge-in can
  keep exactly the words spoken. Chosen by the owner over Gemini Live (speech-to-speech),
  which would have meant giving up per-stage control.
- **Update (same day):** the owner chose Azure for both languages (`TTS_PROVIDER=azure` in their
  `.env`; the code default stays local so the project runs without an Azure account). Pipecat's
  Azure service waited for Azure's "stop" round trip before passing an interruption on, so the
  agent kept talking ~0.4 s after being interrupted. `QuickStopAzureTTSService` stops in the
  background: 47 ms, measured.
- **Alternatives:** ElevenLabs (best English, Urdu not confirmed for its fast models); Cartesia
  (low latency, Urdu added recently, paid); Google Cloud TTS ($300 trial credit, Urdu coverage
  unclear); Uplift AI (Urdu specialist, not in our Pipecat version yet).

## D31. Groq's free tier has a daily limit too; rate-limit state is shared per process (Phase 5)
- **Decision:** the fallback keeps one `BudgetBook` per process, keyed by model and API key
  (never the key itself), so every call learns from the others' 429s and token headers. It
  models Groq's per-minute bucket as refilling continuously (rate = what's missing / time to
  full), and counts tokens on unescaped text.
- **Why (measured 2026-11-11):** besides 8k tokens/minute, Groq allows **200k tokens per day per
  model** (`TPD`, reported in the 429). That's about 100 agent requests a day per model. During
  the benchmark, gpt-oss-20b ran out, so three of the eight configs were answered almost
  entirely by Gemini: at P50 that's 4.4 s to first text versus 0.7 s for gpt-oss-120b. Before
  this fix each new call rediscovered an exhausted model with a wasted 429. It also skipped a
  model until its bucket was completely full, and escaped Urdu counted six times its length.
- **Consequence:** the free tier fits development and demos, not a busy line or a full eval run
  (about 1.2M agent tokens). Groq's pay-as-you-go tier would cost cents for that; adopting it
  is the owner's call (paid service).
- **Alternatives:** per-call state (simple, but repeats every 429); waiting out `retry-after`
  (dead air).
