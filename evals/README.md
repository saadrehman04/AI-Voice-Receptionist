# evals/

The simulated-caller eval suite (Phase 6): an LLM plays a caller, the real agent brain answers,
and scorers check what actually happened.

```bash
make evals                                   # all scenarios, text mode
make evals ARGS="--tags ci"                  # the 15-scenario CI subset
make evals ARGS="--ids salon-reschedule-en"  # one scenario
make evals ARGS="--audio"                    # the 10 `audio` scenarios as speech
make evals ARGS="--tags ci --baseline evals/baseline.json"   # exit 1 if booking accuracy
                                                             # drops > 2 points
```

Each run writes `evals/results/<timestamp>/scorecard.md` and `scorecard.json` (git-ignored):
headline metrics, a pass/fail table by tag, every failure with its reason, and what changed
since the previous run. `.github/workflows/evals.yml` runs the `ci` subset on PRs that touch
the agent and posts the scorecard in the job summary.

## How a scenario runs

1. `scenarios/<id>.yaml` sets the tenant, a frozen clock (`now`), the caller's persona, goal and
   facts, existing bookings (`seed_bookings`), and what must be true at the end (`expect`).
2. The agent is the real thing: the same prompt, tools, `CallState` and LLM fallback as a phone
   call, over `TextSession` (text mode) or the full voice pipeline (`--audio`: caller text ->
   Kokoro/Piper -> VAD, Groq STT, LLM, TTS), with an in-memory backend seeded per scenario.
3. The caller is an LLM (Gemini first, then Groq gpt-oss-20b; not the agent's model, so it
   doesn't use up the agent's Groq budget). It knows only the scenario and what the
   receptionist said, and ends with `<END>`.
4. Scorers check state, not words (`scorers.py`): the outcome from tool calls, the booking in
   the backend (service, time, name, exact phone), slots that must stay free, no overlapping
   bookings, required and forbidden tools and phrases, price numbers that must come from the
   tenant's facts, and a Gemini judge with a strict rubric for invented facts.

## Scenario format

```yaml
id: salon-book-haircut-en          # = file name
title: Simple haircut booking
tenant: demo-salon
language: en                       # en | ur | mixed (Roman Urdu + English)
tags: [booking, simple, ci]
now: '2026-11-16T10:00:00+05:00'   # Monday 10:00 in Lahore
persona: Polite and clear.
goal: Book a haircut on Wednesday at 2 pm.
facts: {name: Sara Ahmed, phone: 0321 5550101}
seed_bookings: []                  # bookings that exist before the call
expect:
  outcome: booked                  # or a list of acceptable outcomes
  booking: {service: Haircut, start: '2026-11-18T14:00:00+05:00', phone: '+923215550101'}
  no_booking_at: []                # slots that must stay free
  tools_called: [check_availability, create_booking]
  must_mention: []                 # regexes, case-insensitive
  must_not_mention: []
```

## Free-tier limits

A full run is about 73 scenarios x 6-10 turns: a few hundred agent requests plus as many caller
requests, and one judge request per scenario. Scenarios run one at a time by default
(`--parallel`), and every LLM goes through the fallback transport, which moves to the next
model on a 429 instead of waiting. CI uses the 15-scenario `ci` subset with `--retries 1`,
because one flaky scenario is about 7 points there.

## Live results

The first live run (2026-11-12, ci subset) and what it found are in `docs/PROGRESS.md`
(Phase 6) and DECISIONS D35. `evals/baseline.json` holds the metrics CI compares against;
refresh it with `--update-baseline evals/baseline.json` after an intended improvement.

Prompt injection (Phase 9, `--tags injection`, 7 scenarios): the first live run passed 6 of 7.
The failure was a hidden instruction in a message for the owner: the agent safely refused to
act on it, but `transfer_to_human` dropped the phone number the caller had just given and asked
for it again (fixed: the tool now takes name and phone). The re-run had no failures (4 decided,
3 inconclusive because every free-tier model was rate-limited at some point).

