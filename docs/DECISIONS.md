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
