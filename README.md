# Voice AI Receptionist

A bilingual (Urdu/English) AI receptionist for small businesses: answers calls, books appointments
on Google Calendar, answers questions, and hands off to a human when unsure. Built on Pipecat with a
fully free stack.

Status: in development. See docs/PROGRESS.md.

- Build plan: docs/BUILD_GUIDE.md
- Rules for Claude Code: CLAUDE.md

## Run locally
Needs uv, Node 20+ with pnpm (`corepack enable pnpm`), Docker and make.

```bash
make install   # deps + .env
make up        # Postgres + Redis
make test && make lint
```

Then `make api`, `make agent` or `make web`. `make help` lists all commands.

(This README gets rewritten for recruiters in phase 9.)
