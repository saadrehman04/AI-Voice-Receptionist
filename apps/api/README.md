# apps/api (`receptionist_api`)

FastAPI backend. Owns all state; the voice agent's tools call it over HTTP.

Run: `make up && make migrate && make api`, then http://127.0.0.1:8000/docs.

| File | What it does |
| --- | --- |
| `main.py` | App factory; builds DB/Redis/calendar/KB resources in the lifespan. |
| `routes/agent_tools.py` | `/v1/tenants/{slug}/...` endpoints, `X-Agent-Token` auth, error codes. |
| `booking_service.py` | Availability, holds, create/reschedule/cancel, messages. The double-booking defences. |
| `holds.py` | Redis slot holds (atomic Lua acquire/release, 2-minute expiry). |
| `calendar.py` | `Calendar` interface, `FakeCalendar`, `GoogleCalendar` (REST + service account). |
| `knowledge_base.py` | YAML facts + markdown sections searched by local multilingual embeddings. |
| `db/models.py`, `migrations/` | SQLAlchemy models and Alembic migrations (exclusion constraint). |
| `testing.py` | Pytest fixtures with real Postgres/Redis, used by api and agent tests. |

Google Calendar setup: see docs/GOOGLE_CALENDAR.md.
