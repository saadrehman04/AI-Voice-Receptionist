# Progress

Tick a phase only when its "done when" check in BUILD_GUIDE.md passes.

- [ ] Phase 0: Scaffold the monorepo
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

### Phase 0 (scaffold): built, not ticked yet
- Built: a uv workspace (shared, agent, api, evals) with strict mypy, ruff and pytest (13 tests); Next.js 16 app
  in apps/web (pnpm 12); docker-compose with Postgres 16 + Redis 7 and healthchecks; Makefile; GitHub Actions CI;
  typed `Settings` in packages/shared; ARCHITECTURE.md, DECISIONS.md and CLAUDE.md commands.
- Verified locally: `make up` (Postgres 16 + Redis 7 healthy, localhost-only), `make test`, `make lint`,
  `make agent`, `make evals`, `make api` + `/health`, `pnpm build`.
- Open: CI not run yet (no commits/GitHub repo yet). Tick once CI is green.
- Deferred on purpose: JSON logging with call_id/turn_id (Phase 1); DB/Redis readiness check in /health (Phase 4).
