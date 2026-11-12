# Case study: a bilingual AI receptionist on a free stack

## The problem

Small businesses in Pakistan (salons, clinics, car workshops) run on phone calls, and most of them
miss calls: the owner is with a customer, the shop is closed, or the line is busy. Every missed
call is a missed booking. A human receptionist costs a salary; existing AI receptionists are
English-first, priced in dollars, and don't speak Urdu the way callers actually do (Urdu and
English mixed in one sentence).

The goal: an assistant that answers every call in Urdu or English, books, moves and cancels
appointments in the business's calendar, answers questions from the business's own facts, and
hands off to a person when it can't help. It had to run on free tiers until a paying client
covers a phone line, on a laptop with no GPU.

**Who it's for:** the business owner, who signs up in a few minutes, sees every call (transcript,
recording, summary, what was booked) and never has to trust a black box.

## What was built

- A real-time voice agent (Pipecat): VAD → speech-to-text → LLM with tools → text-to-speech,
  streamed end to end, in the browser (WebRTC) and over WhatsApp calls.
- A multi-tenant backend (FastAPI, Postgres, Redis) that owns bookings, messages, call records
  and per-business limits, and serves the agent's tools over HTTP.
- An owner dashboard (Next.js): sign-up that drafts the setup from the business's website, calls
  with transcripts and recordings, bookings, messages, analytics, settings, a test-call button.
- A simulated-caller eval suite (76 scenarios, English and Urdu) that runs in CI and blocks a
  merge if booking accuracy drops.

## Decisions and trade-offs

### Pipeline (STT → LLM → TTS) instead of speech-to-speech
Speech-to-speech models (Gemini Live, OpenAI Realtime) answer faster and sound more natural, and
were the obvious alternative. I chose the cascaded pipeline because:
- **Control per stage.** Booking safety lives in tool code between the stages: the agent reads
  details back, and a booking only commits after a *new* caller turn confirms (D11, D24). With
  text in the middle every turn can be logged, scored and replayed.
- **Urdu.** Each stage could be picked for Urdu separately (Whisper large-v3 for mixed
  Urdu-English, Azure's Pakistani voices). Speech-to-speech Urdu quality wasn't something I
  could verify or tune.
- **Free and swappable.** Every provider sits behind an interface and is chosen in `.env`; when
  one free tier ran out, another took over (D23). One speech-to-speech vendor is one point of
  failure and one bill.

**Cost:** latency. The best path measured ~1.8-2.0 s from the caller going quiet to the agent's
first audio, against a < 1 s goal (see Results). Speech-to-speech is the first thing I'd try with
a budget, behind the same tool layer.

### Pipecat instead of LiveKit Agents
Both are open-source Python frameworks for voice agents. Pipecat won on:
- **No media server.** Its peer-to-peer WebRTC transport (aiortc) runs in the agent process;
  LiveKit Agents expects a LiveKit server (self-hosted, or LiveKit Cloud), one more service to
  run for free.
- **Frames all the way down.** Everything (audio, text, tool calls, interruptions) is a frame
  through processors, so custom stages (a language router that picks the Urdu voice per reply,
  latency observers, a first-clause text splitter) are small classes.
- **Built-in pieces this project needed:** Smart Turn v3 (a CPU end-of-turn model), Groq, Gemini,
  Kokoro, Piper and Azure services, and WhatsApp transport support to build on.

**Cost:** Pipecat changes fast (1.x releases every few weeks). Twice its behaviour was the bug:
its Azure service waited a network round trip before stopping on an interruption (fixed in a
subclass, D30), and its Groq STT couldn't auto-detect language (D25). Each workaround has a test
that fails if upstream fixes it, so it can be deleted.

### Living within free tiers
- **Groq** (LLM + Whisper) is the fastest free option but allows 8k tokens a minute and ~200k a
  day per model. One call uses ~1.5k tokens per request, two or three requests per turn. A
  fallback transport *below* the OpenAI client moves to the next model on a 429 before Pipecat
  sees anything, learns each model's remaining budget from response headers, and shares that
  across calls (D23, D31).
- **Gemini Flash-Lite** is the backup: reliable, but ~4 s to first text, so callers notice.
- **Local models on a CPU:** faster-whisper and Piper run fine; Kokoro's natural English voice
  took ~1x real time, too slow for a live call, so Piper speaks by default (D25).
- **Evals cost tokens too:** the full 76-scenario run needs ~1.2M tokens, several free days.
  CI runs a 15-scenario subset.

### Safety in code, not only in the prompt
Every rule the model broke in testing moved from the prompt into code: the read-back-then-
confirm gate (D24), refusing a date whose weekday the caller never said (D35), phone numbers
spelled digit by digit by the tool so the read-back can't garble them (D35), a database
constraint that makes double booking impossible (D15). The prompt guides the conversation; the
code decides what gets written.

### Failure is a normal path (Phase 9)
Free tiers fail daily. Each stage has a next step: Groq STT → retry → local Whisper; Groq LLM →
other models → Gemini → a scripted receptionist that takes a message; Azure voice → local Piper.
A guard apologises if any error still reaches the caller. Pulling any single key leaves callers
a way through (D37).

## Results

| What | Result | How measured |
| --- | --- | --- |
| Caller stops → agent audio (P50, Groq path, Piper) | ~1.8-2.0 s (goal < 1 s, not met) | `make bench`, 20 real-time utterances through the real pipeline ([findings](benchmarks/2026-11-11-findings.md)) |
| Barge-in: agent audio stops after interruption | 12-47 ms | same benchmark; `test_barge_in.py` in CI |
| Booking accuracy (evals, CI subset) | 100% of 9 booking cases | `make evals`, LLM caller + rule checks + Gemini judge ([baseline](../evals/baseline.json)) |
| Scenario pass rate (CI subset) | 92.3% (12 of 13 decided) | same |
| Prompt-injection scenarios (7) | first run 6 of 7 (the failure exposed a real hand-off bug, fixed); re-run no failures (4 decided, 3 inconclusive on free-tier limits) | `make evals ARGS="--tags injection"` |
| WhatsApp call answered (simulated Meta) | 0.35-0.41 s | `make whatsapp-sim` against the real agent |
| Tests | 477 Python + 16 web, Postgres/Redis included | `make test`, CI on every push |

**Pilot numbers:** [TODO: calls answered per week at a pilot business]
[TODO: missed calls before vs after] [TODO: share of calls booked without a human]
[TODO: owner time saved per week] [TODO: caller satisfaction / hang-up rate]

## What went wrong (and what it taught me)

- **The model "confirmed" its own read-back.** Asked to read details back and wait for a yes,
  the LLM sometimes read back and booked in the same turn. Fix: the commit needs a caller turn
  newer than the read-back, checked in code (D24). Lesson: *a rule the model must follow every
  time belongs in code.*
- **A retired model.** The planned LLM (Llama 3.3 on Groq) had been retired; I'd checked docs, not
  the live model list. Lesson: check the provider's live API, not its docs page.
- **The free tier's second limit.** The latency benchmark looked worse with every config until I
  found Groq's daily cap had run out mid-run and Gemini was answering. The report now splits
  turns by which model answered. Lesson: *measure which path served the request, not only how
  long it took.*
- **Wrong weekday, garbled numbers.** The first live eval run caught the agent booking Monday for a
  caller who said Thursday, and reading 0300 555 0128 back in Urdu as "three hundred, fifty,
  fifty". Neither showed up in hand testing. Lesson: *simulated callers at volume find what
  demos don't.*
- **Hanging up in silence.** A small model, routed a "great, thank you", ended the call without a
  word. The agent now always says goodbye before hanging up.
- **Failure drills found what unit tests didn't.** Running the real agent with each key revoked
  showed that the local Whisper fallback loaded on first use (20 s of silence for that caller),
  that Whisper "small" takes 7-11 s per utterance on this CPU (now "base": 2-4.6 s), that the
  backup's free tier ran out too (Gemini: 500 requests a day), and that the scripted fallback
  turned a caller's "yes" to a read-back into a message instead of booking it. Lesson:
  *a fallback you haven't run live is a guess.*
- **An eval that was wrong.** An injection check's regex matched the agent's *refusal* ("I can't
  give you a discount code"). Lesson: test the tests; read failures before trusting the score.

## What I'd do next

1. A live pilot with one business on a real WhatsApp number (the code path is built and tested
   against a simulated Meta; switching is configuration).
2. Streaming STT with partial transcripts, or speech-to-speech behind the same tools, to get
   under 1 s.
3. Groq's paid tier for the main path (cents per call), keeping Gemini and the scripted
   message-taker as fallbacks.
4. Require the caller's own number (known on WhatsApp) to move or cancel a booking.
5. Run the full 76-scenario suite nightly instead of the 15-scenario subset per PR.

## Lessons

- **Latency is a budget, not a number.** Writing the per-stage table (VAD 225 ms, STT 380 ms, LLM
  700 ms, TTS 290 ms) made it obvious which changes could matter and which never would.
- **Free tiers shape the architecture.** Fallbacks, budgets and a scripted last resort weren't
  polish; they were what made the free stack usable at all.
- **Write the decision down when you make it.** 42 short decision records made this case study
  a summary rather than an excavation.
