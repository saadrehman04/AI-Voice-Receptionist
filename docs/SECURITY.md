# Security

What can go wrong in a phone receptionist that books into strangers' calendars, and what
stops it. Each control names the test that proves it, so a regression fails CI.

## Who can do what

| Actor | Can | Cannot |
| --- | --- | --- |
| A caller | Talk to one business's receptionist; book, move or cancel a booking found by its phone number (plus the booking code when that number has several); leave a message | Change the rules, see the prompt, invent prices, touch another caller's booking, book outside opening hours |
| A business owner | Read and change their own business's settings, calls, bookings, messages | See or change another business's data; call the agent's tool endpoints |
| The voice agent | Call tool endpoints for the business it is serving (shared `X-Agent-Token`) | Read the owner dashboard |
| Meta (WhatsApp) | Deliver call events signed with the app secret | Have an unsigned or tampered event acted on |

## Controls

### Secrets
- Secrets come only from `.env` and are typed `SecretStr` in `Settings`, so printing the settings
  shows `**********` (`test_config.py::test_secrets_are_masked_when_printed`).
- **Log redaction (Phase 9):** every log line from every service, Pipecat's included, passes a
  patcher that masks each secret value in Settings plus anything shaped like a Groq/Google/Meta
  key, bearer token or JWT, in the message, the bound fields and exception text
  (`test_logging.py::test_secrets_never_reach_a_log_line`).
- **Repo scan (Phase 9):** CI fails if any tracked file contains something shaped like a real
  credential, or if `.env` is tracked (`test_repo_secrets.py`).
- The agent's rate-limit bookkeeping keys budgets by a hash of the API key, never the key
  (`llm_fallback.BudgetBook`); provider descriptions in logs never include keys
  (`test_providers.py::test_describe_providers_never_includes_secrets`).

### Webhooks and service-to-service calls
- WhatsApp webhooks must carry a valid `X-Hub-Signature-256` HMAC of the exact body; unsigned,
  tampered or unknown-number calls are refused and never answered
  (`test_whatsapp.py::test_signature_must_match_the_exact_body`,
  `test_unsigned_or_unknown_calls_are_refused`). With no app secret configured the endpoint
  answers 503: it fails closed.
- Tool endpoints need `X-Agent-Token`, compared in constant time; an API without a token
  configured refuses every tool call (`require_agent_token`).

### Tenant isolation
- Every table carries `tenant_id`; owner routes take the tenant from the signed JWT, never from
  the URL or body. Two real tenants are created through onboarding and every cross-tenant read
  and write is tried (`test_owners_and_isolation.py`: `test_lists_only_show_own_tenant`,
  `test_cannot_read_or_change_other_tenants_records`, `test_cannot_overwrite_other_tenants_settings`,
  `test_owner_token_cannot_call_agent_endpoints`). Usage counters are per tenant
  (`test_usage_limits.py`).
- Call recordings are served only if the file resolves inside `storage/<that tenant>/`
  (`test_setup_routes.py`).
- One WhatsApp number maps to at most one business (unique column, migration 0003).

### Owner accounts
- Passwords hashed with argon2; sessions are the API's JWT (12 h) in an `httpOnly`,
  `SameSite=Lax` cookie (`Secure` in production). Page scripts never see the token, and only the
  Next.js server talks to the API (no CORS).
- Expired or tampered tokens are rejected (`test_auth.py`).

### The website-draft fetcher (SSRF)
- Owners can paste a URL to pre-fill their setup. Every resolved address must be public
  (`ipaddress.is_global`), redirects are followed by hand and re-checked at each hop, the body is
  capped at 1 MB, and the endpoint is rate-limited (`test_website_draft.py`,
  `test_setup_routes.py::test_draft_refuses_private_addresses`).

### Bookings
- The LLM cannot book on its own say-so: `create_booking` reads the details back first and only
  commits after a **new** caller turn that confirms, enforced in code (`CallState`, D24).
- Slots are held in Redis during the read-back, writes are idempotent, and a Postgres exclusion
  constraint makes a double booking impossible even if Redis is bypassed
  (`test_double_booking.py`).
- Cancelling or moving a booking needs the phone number it was made with (and the booking code
  when that number has more than one), and the same read-back-then-confirm step.
- A booking on a weekday the caller never mentioned is refused (D35).
- **Per-business caps (Phase 9):** bookings and messages per hour, calls per hour, minutes per
  month (D38, `test_usage_limits.py`). A manipulated or looping agent can't flood a calendar.

### Prompt injection
- The prompt tells the model the rules can't be changed by the caller, and the facts it may
  state are only the business's config; tools enforce the rules that matter (confirmation,
  opening hours, prices come from config) rather than trusting the model.
- The eval suite has 7 injection scenarios (`evals/scenarios/*injection*`): reveal the prompt,
  fake "admin mode" discount, impersonate the manager to cancel others' bookings, a fake
  system message booking a closed day, a "the manager said it's cheaper" price override,
  instructions hidden inside a message for the owner, and an Urdu free-service attempt.
  Results: [evals/README.md](../evals/README.md#live-results).
- When no LLM is reachable, the scripted fallback (D37) states no business facts at all.

## Not covered (known gaps)
- **Caller identity.** A caller who knows someone's phone number can move or cancel that
  person's booking. Real phone lines (WhatsApp) tell us the caller's number, which could be
  required to match (or the booking code required always); browser test calls can't.
- **Abuse at the network edge.** No WAF or per-IP limits on the agent's WebRTC endpoint; a
  deployment should sit behind a proxy that rate-limits `/start` and `/whatsapp`.
- **Recordings at rest** are plain WAV files on disk; production would use encrypted object
  storage with retention rules.
- **Secrets rotation** is manual (edit `.env`, restart).
