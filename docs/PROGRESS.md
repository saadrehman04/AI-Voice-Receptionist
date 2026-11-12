# Progress

Tick a phase only when its "done when" check in BUILD_GUIDE.md passes.

- [x] Phase 0: Scaffold the monorepo
- [ ] Phase 1: Voice loop in the browser
- [x] Phase 2: The receptionist brain
- [ ] Phase 3: Real tools and safe booking
- [ ] Phase 4: Backend, multi-tenancy and call records
- [x] Phase 5: Latency and barge-in
- [x] Phase 6: Simulated-caller eval suite
- [x] Phase 7: Owner dashboard and onboarding
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

### Phase 2 (receptionist brain): done 2026-11-11
- Built: tenant YAML schema + 2 example tenants; prompt from tenant facts; CallState with
  read-back-then-confirm enforced in code; 8 tools; Urdu/English TTS routing (Piper ur_PK for Urdu);
  text-mode session + `make chat`; ScriptedLLM for deterministic conversation tests.
- Verified live (docs/evidence/phase2-chat.md): greets with the business name, hours and FAQ from
  the YAML, refuses the unlisted bridal price, "will check" + message for an unknown question,
  check -> read back -> book only after "yes". English and Urdu booking runs pass 4/4.
- Found live and fixed: Groq had retired Llama 3.3 (now gpt-oss-120b, D23); the model confirmed
  its own read-back, so a commit now needs a new caller turn (D24); 8k tokens/min per Groq model
  stalled calls 10-25 s, so an HTTP-level fallback goes gpt-oss-120b -> gpt-oss-20b -> Gemini (D23).
- Open: model answers still vary run to run; Phase 6's eval suite measures that properly.

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

### Phase 5 (latency, barge-in): done 2026-11-11
- Built: `make bench` (20 utterances through the real pipeline over a fake transport, P50/P95 per
  stage per flag config); flags for first-clause TTS chunking, tool fillers, pre-warm,
  simple-turn routing, VAD stop and a longer pause after asking for a number; 30 s availability
  cache; OpenTelemetry spans + Jaeger (`make traces`); optional Azure voices (D30).
- Measured ([findings](benchmarks/2026-11-11-findings.md)): Kokoro -> Piper saves ~1.5 s; best
  path ~1.8-2.0 s from caller stop to agent audio (goal < 1 s not met on free tiers: STT ~0.6 s,
  LLM ~0.7 s, TTS ~0.3 s). Barge-in: audio stops 12-37 ms after the interruption, context keeps
  only what was played (`test_barge_in.py` in CI).
- Found and fixed: Groq STT failed without a language; Kokoro dropped long sentences; phone
  numbers were cut at the first pause; Groq's 200k tokens/day cap (D31).
- Open: pre-warm and routing need a re-run on a fresh Groq day; Azure voices need the owner's key.

### Phase 6 (simulated-caller evals): done 2026-11-12
- 73 scenarios (15 tagged `ci`, 10 `audio`), LLM caller + rule scorers + Gemini judge; one command
  (`make evals`) writes a markdown + JSON scorecard with a diff; the PR workflow fails if booking
  accuracy drops more than 2 points below `evals/baseline.json`.
- First live run of the ci subset: 11/15. Real bugs fixed: wrong-weekday dates (code guard),
  reschedule done as a new booking and asked twice (prompt), no goodbye after "bye" (prompt),
  Urdu phone read-back grouped into wrong numbers (the tool now spells digits in Urdu). Two
  checks were wrong and were fixed (an injection regex matched the refusal; a cancel 3 days
  ahead was required to quote the 4-hour policy). Baseline run: 12/13 decided, booking 100%;
  the failure (Urdu read-back) then passed after its fix.
- Open: a full 73-scenario run needs ~1.2M agent tokens, several free-tier days (or Groq's paid
  tier); `workshop-ur-book-oil-change` stayed inconclusive (rate limits).

### Phase 8 (WhatsApp calling): built and verified with a simulated Meta; live number open
- Webhook in the agent (signed, fail-closed), call answering over WebRTC (pre_accept/accept),
  the normal pipeline per call (recorded, summarised, WhatsApp badge on the dashboard),
  confirmation text after booking/move/cancel, number -> business mapping (unique column,
  Settings field). Fake Meta (`make whatsapp-sim`) + a real-WebRTC CI test. D36, docs/WHATSAPP.md.
- Live simulated calls against the real agent: answered in ~0.4 s, booked, confirmation sent,
  agent hung up after goodbye. Found and fixed: English phone read-back dropped digits (now
  spelled by the tool, like Urdu; D35).
- Not ticked: the "done when" needs a real WhatsApp call on a Meta number, which the owner
  chose to skip for now (steps in docs/WHATSAPP.md, section 2).

### Phase 7 (owner dashboard and onboarding): done 2026-11-12
- apps/web: sign-up wizard (website draft -> services/FAQ -> hours -> languages -> account),
  /welcome (calendar connect + test call), Calls (transcript, recording, summary, actions),
  Bookings, Messages, Analytics (calls/day, booking rate, calls while closed, P50/P95 per stage),
  Settings. Mobile-friendly. API: website draft with SSRF checks and grounding (D32), calendar
  connect, recordings, richer stats. Session and test-call design in D33-D34; docs/DEPLOY.md.
- Verified in the browser: signed up a new business, connected a (practice) calendar, test call
  connected and greeted with the new business's name, the call showed up with its summary,
  a settings change reached the agent's config.
- Open: the Vercel deploy itself needs the owner's Vercel account (steps in DEPLOY.md); the
  browser pane blocks the microphone, so a spoken test-call conversation is still the owner's
  to try; Google Calendar connect is tested with the fake calendar only.

