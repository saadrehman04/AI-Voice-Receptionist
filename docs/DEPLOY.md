# Deploying

The free setup: the web dashboard on **Vercel's free tier**, and the API, worker, agent,
Postgres and Redis on a machine you control (your laptop for a demo, or any small VM later).
Nothing here needs a paid service.

```
Browser ──https──> Vercel (apps/web) ──https──> API (FastAPI)  ── Postgres, Redis
   │                      │                        ▲
   │                      └──https──> Agent /start, offer (WebRTC signalling)
   └──────── WebRTC audio (UDP) ────────> Agent ───┘ (tools, call records)
```

## 1. API, worker and agent

On the machine that runs them (from the repo root):

```bash
make install            # once
make up && make migrate # Postgres + Redis, then the schema
make api                # :8000
make worker             # post-call summaries
make agent              # :7860
```

`.env` must have `JWT_SECRET`, `AGENT_API_TOKEN`, `GROQ_API_KEY` (and optionally
`GEMINI_API_KEY`, `AZURE_SPEECH_KEY`), plus `BOOKING_BACKEND=api` so the agent reads tenants
created in the dashboard. For real calendars see [GOOGLE_CALENDAR.md](GOOGLE_CALENDAR.md).

In production run them without reload, e.g. `uv run --all-packages uvicorn
receptionist_api.main:app --host 127.0.0.1 --port 8000`, under systemd or `docker run`, and put
the API and the agent behind HTTPS (Caddy does certificates for free).

### Reaching them from Vercel without a server

For a demo from your laptop, a free Cloudflare quick tunnel gives each port a public HTTPS URL:

```bash
cloudflared tunnel --url http://127.0.0.1:8000   # prints https://<random>.trycloudflare.com
cloudflared tunnel --url http://127.0.0.1:7860
```

Quick-tunnel URLs change every run; a named tunnel (free, needs a Cloudflare account and a
domain) keeps them fixed.

**WebRTC audio** doesn't go through the tunnel: the browser and the agent connect directly
(UDP). That works on the same network. Across the internet behind home NAT it may not, unless
the agent has a TURN server (`--ice-servers` / `PIPECAT_ICE_SERVERS`). Phase 8 (WhatsApp)
replaces browser calls for real callers anyway; the browser test call is for owners trying it.

## 2. Web dashboard on Vercel

1. Import the GitHub repo in Vercel and set **Root Directory** to `apps/web`. It detects
   Next.js and pnpm from `package.json`.
2. Environment variables (Production):

   | Name | Value |
   | --- | --- |
   | `API_URL` | the API's public HTTPS URL, no trailing slash |
   | `AGENT_URL` | the agent's public HTTPS URL |

3. Deploy. Sign-up is at `/signup`.

Both variables are read only on the server (they have no `NEXT_PUBLIC_` prefix), so the
browser never learns where the API lives, and the session cookie is `Secure` in production.

## 3. Check it

- `https://<your-app>.vercel.app/signup`: create a business; the website step needs the API to
  reach the internet and an LLM key.
- `/welcome` → connect the calendar → **Start a test call**: you should hear the greeting.
- Hang up → `/calls` shows the call; the summary appears once the worker has run.

## Local development

`make web` serves the dashboard on http://localhost:3000 with `API_URL` and `AGENT_URL`
defaulting to the local ports, so no web `.env` is needed.
