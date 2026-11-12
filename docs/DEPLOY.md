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

## 1. API, worker and agent on a server

Any Linux VM (Ubuntu 24.04 used below) with about 4 CPUs and 8 GB RAM: the agent keeps its
speech models in memory. Ready-made files are in `deploy/`: a Caddyfile (HTTPS) and systemd
units that start the API, worker and agent at boot and restart them if they stop.

You need two web names pointing at the server's public IP, one for the API and one for the
agent (free from DuckDNS, e.g. `myapp-api.duckdns.org` and `myapp-agent.duckdns.org`).

**Install** (as a sudo user on the server):

```bash
sudo apt update && sudo apt install -y git make docker.io docker-compose-v2 caddy
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
sudo useradd --system --create-home --shell /usr/sbin/nologin receptionist
sudo git clone https://github.com/<you>/voice-receptionist.git /opt/voice-receptionist
sudo chown -R receptionist: /opt/voice-receptionist
cd /opt/voice-receptionist
sudo -u receptionist -H make install-server   # Python deps + .env with fresh secrets
```

**Configure** `sudo -u receptionist nano /opt/voice-receptionist/.env`:

- `ENVIRONMENT=prod` (no auto-reload, secure cookies)
- a long random `POSTGRES_PASSWORD`, and the same password inside `DATABASE_URL`
- `GROQ_API_KEY` (and optionally `GEMINI_API_KEY`, `AZURE_SPEECH_KEY`)
- `JWT_SECRET`, `AGENT_API_TOKEN` and `BOOKING_BACKEND=api` were set by `make install-server`

```bash
sudo docker compose up -d --wait              # Postgres + Redis, local to the server only
sudo -u receptionist -H make migrate
```

**Run the services**:

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now receptionist-api receptionist-worker receptionist-agent
journalctl -u receptionist-agent -f           # watch it start (models load in ~30 s)
```

**HTTPS with Caddy.** The agent's `/start` takes the business from the request and checks no
login, so Caddy lets call set-up through only with a key that the dashboard sends, lets Meta's
signed `/whatsapp` webhook through, and refuses everything else (Pipecat's `/client` playground
included). Make the key and fill in your names:

```bash
KEY=$(openssl rand -hex 32)
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i -e "s/api.example.com/myapp-api.duckdns.org/" \
  -e "s/agent.example.com/myapp-agent.duckdns.org/" \
  -e "s/REPLACE_WITH_AGENT_KEY/$KEY/" /etc/caddy/Caddyfile
sudo systemctl reload caddy
echo "$KEY"   # once, to paste into Vercel as AGENT_KEY (step 2); keep it out of chats and repos
```

**Firewall**: open TCP 80 and 443 (Caddy). Call audio is WebRTC over UDP and goes straight to
the agent, not through Caddy: allow inbound UDP to the server (aiortc picks high ports per call),
or use a TURN server (see "WebRTC audio" under the laptop demo below). On a cloud VM behind NAT (Azure, Oracle) the agent may
also need a STUN server to learn its public address. This part hasn't been tested on a cloud
server yet: check the first browser test call carefully.

**Update** to a new version:

```bash
cd /opt/voice-receptionist && sudo -u receptionist git pull
sudo -u receptionist -H make install-server && sudo -u receptionist -H make migrate
sudo systemctl restart receptionist-api receptionist-worker receptionist-agent
```

### Demo from your laptop instead of a server

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
   | `AGENT_KEY` | the key from the Caddy step (test calls are refused without it) |

3. Deploy. Sign-up is at `/signup`.

All three are read only on the server (they have no `NEXT_PUBLIC_` prefix), so the
browser never learns where the API lives, and the session cookie is `Secure` in production.

## 3. Check it

- `https://<your-app>.vercel.app/signup`: create a business; the website step needs the API to
  reach the internet and an LLM key.
- `/welcome` → connect the calendar → **Start a test call**: you should hear the greeting.
- Hang up → `/calls` shows the call; the summary appears once the worker has run.

## Local development

`make web` serves the dashboard on http://localhost:3000 with `API_URL` and `AGENT_URL`
defaulting to the local ports, so no web `.env` is needed.
