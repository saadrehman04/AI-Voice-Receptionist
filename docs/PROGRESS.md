# Progress

Tick a phase only when its "done when" check in BUILD_GUIDE.md passes.

- [x] Phase 0: Scaffold the monorepo
- [ ] Phase 1: Voice loop in the browser
- [ ] Phase 2: The receptionist brain
- [ ] Phase 3: Real tools and safe booking
- [ ] Phase 4: Backend, multi-tenancy and call records
- [ ] Phase 5: Latency and barge-in
- [ ] Phase 6: Simulated-caller eval suite
- [ ] Phase 7: Owner dashboard and onboarding
- [ ] Phase 8: WhatsApp calling
- [ ] Phase 9: Hardening and portfolio write-up

## Notes per phase
(Claude adds 2-4 lines here at the end of each phase: what was built, anything left open.)

### Phase 0 (scaffold): done 2026-11-11
- Built: a uv workspace (shared, agent, api, evals) with strict mypy, ruff and pytest (13 tests); Next.js 16 app
  in apps/web (pnpm 12); docker-compose with Postgres 16 + Redis 7 and healthchecks; Makefile; GitHub Actions CI;
  typed `Settings` in packages/shared; ARCHITECTURE.md, DECISIONS.md and CLAUDE.md commands.
- Verified locally: `make up` (Postgres 16 + Redis 7 healthy, localhost-only), `make test`, `make lint`,
  `make agent`, `make evals`, `make api` + `/health`, `pnpm build`.
- CI green on GitHub (lint + test, ~26s). setup-uv has no floating major tag, so it is pinned by commit SHA.
- Deferred on purpose: JSON logging with call_id/turn_id (Phase 1); DB/Redis readiness check in /health (Phase 4).

### Phase 1 (voice loop): built, waiting on a live test
- Built: Pipecat 1.15 runner with the peer-to-peer WebRTC transport and prebuilt client at :7860/client;
  Silero VAD + Smart Turn v3 (CPU); STT/LLM/TTS chosen by config (Groq | faster-whisper, Groq | Gemini |
  Ollama, Kokoro | Piper); per-turn stage timings to logs/turns.jsonl; `make latency` for P50/P95.
- Verified: unit + in-pipeline tests; server boots and serves the client; offline STT/TTS on this CPU
  (`make test-models`: Kokoro 3.2 s for 3.1 s of speech; Piper Urdu 0.3 s for 2.9 s; whisper-small 4.6 s).
- Open: the 2-minute spoken conversation needs GROQ_API_KEY and a person at the microphone.

### Phase 2 (receptionist brain): built, waiting on a live-LLM check
- Built: tenant YAML schema + 2 example tenants; prompt from tenant facts; CallState with
  read-back-then-confirm enforced in code; 8 tools; Urdu/English TTS routing (Piper ur_PK for Urdu);
  text-mode session + `make chat`; ScriptedLLM for deterministic conversation tests.
- Open: greet / FAQ / refuse-unknown-price / tool-timing checks with a real LLM (needs GROQ_API_KEY;
  Phase 6 automates them).

### Phase 3 (real tools, safe booking): built, waiting on Google Calendar + a live call
- Built: tools served by the api; Redis holds, idempotency keys, re-check, Postgres exclusion
  constraint (race tests pass against real Postgres, also with Redis bypassed); Google Calendar over
  REST + fake calendar; markdown KB with local multilingual embeddings (Urdu question -> English note).
- Open: "a spoken booking appears in Google Calendar" needs the service account (docs/GOOGLE_CALENDAR.md)
  and GROQ_API_KEY.

### Phase 4 (backend, tenancy, call records): built, isolation tests pass
- Built: migration 0002 (users, services, calls, turns, tool_calls, tenant config JSONB); onboarding +
  argon2/JWT login; dashboard API scoped by token; agent call recorder (non-blocking) + optional
  stereo recording with spoken disclosure; Redis job queue + `make worker` for summaries.
- Verified: isolation tests (two tenants via real onboarding, every cross-tenant read/write refused);
  system test agent -> api -> Postgres -> queue -> worker -> dashboard; real api + worker processes.
- Open: a real voice test call end to end (GROQ_API_KEY); summaries use the heuristic until then.
