# One entry point for every common task. Run `make help` for the list.
# Python runs through uv (one workspace, one .venv); the web app through pnpm.

UV_RUN := uv run --all-packages
WEB    := pnpm --dir apps/web
PY_SRC := packages apps/agent apps/api evals

.DEFAULT_GOAL := help
.PHONY: help install up down logs agent api web test lint fmt evals latency test-models chat migrate worker

help: ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-8s\033[0m %s\n", $$1, $$2}'

install: ## Install Python + web deps, create .env from .env.example if missing
	uv sync --all-packages
	$(WEB) install --frozen-lockfile
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example - fill in your keys")

up: ## Start Postgres + Redis and wait until healthy
	docker compose up -d --wait

down: ## Stop Postgres + Redis (data kept in volumes)
	docker compose down

logs: ## Follow Postgres + Redis logs
	docker compose logs -f

agent: ## Run the voice agent; talk to it at http://localhost:7860/client
	$(UV_RUN) python -m receptionist_agent

chat: ## Text chat with the receptionist (real LLM + tools, stub bookings; ARGS="--tenant demo-workshop")
	$(UV_RUN) python -m receptionist_agent.chat $(ARGS)

latency: ## P50/P95 per stage from logs/turns.jsonl (ARGS="--last 50")
	$(UV_RUN) python -m receptionist_agent.latency_report $(ARGS)

migrate: ## Apply database migrations (alembic upgrade head)
	$(UV_RUN) alembic -c apps/api/alembic.ini upgrade head

worker: ## Run the background worker (post-call summaries)
	$(UV_RUN) python -m receptionist_api.worker

api: ## Run the FastAPI backend (auto-reload in dev)
	$(UV_RUN) receptionist-api

web: ## Run the Next.js dev server on :3000
	$(WEB) dev

test: ## Run all tests (fast; no model downloads, no network)
	$(UV_RUN) pytest

test-models: ## Smoke-test local Kokoro + faster-whisper on CPU (downloads models once)
	$(UV_RUN) pytest -m models -s

lint: ## Lint + type-check Python and web
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .
	$(UV_RUN) mypy $(PY_SRC)
	$(WEB) lint
	$(WEB) typecheck

fmt: ## Auto-fix lint issues and format Python code
	$(UV_RUN) ruff check --fix .
	$(UV_RUN) ruff format .

evals: ## Run the simulated-caller eval suite (Phase 6)
	$(UV_RUN) receptionist-evals
