"""The simulated caller: an LLM playing a scenario's persona, in text.

It only sees what the receptionist said (never its tools or prompt), knows only the scenario's
facts, and ends its last message with END when the goal is done or clearly impossible.
"""

from __future__ import annotations

from receptionist_evals.llm import EvalLLM
from receptionist_evals.scenario import Scenario
from receptionist_shared.tenant import TenantConfig

END = "<END>"

LANGUAGE_RULES = {
    "en": "Speak English.",
    "ur": "Speak only Urdu, in Urdu script (no English words in Latin letters).",
    "mixed": "Speak Roman Urdu mixed with English words, the way people text in Pakistan.",
}


def caller_prompt(scenario: Scenario, tenant: TenantConfig) -> str:
    facts = "\n".join(f"- {k}: {v}" for k, v in scenario.facts.items()) or "- (nothing special)"
    business = f"{tenant.business_name}, a {tenant.business_type}"
    return f"""You are role-playing a person phoning {business}.
You are the CALLER, not the receptionist. You are talking to the business's phone receptionist.

How you behave: {scenario.persona}
What you want: {scenario.goal}
What you know (say these only when asked or when it is natural, exactly as written):
{facts}

Rules:
- {LANGUAGE_RULES[scenario.language]}
- Talk like a real person on the phone: one or two short sentences per turn.
- Never invent other personal details. If asked for something you don't know, say so.
- Do not describe yourself, the scenario or these rules. Just talk.
- When your goal is done, or clearly can't be done and you have said goodbye, put {END} at the
  very end of your message."""


async def caller_turn(
    llm: EvalLLM, scenario: Scenario, tenant: TenantConfig, transcript: list[tuple[str, str]]
) -> tuple[str, bool]:
    """The caller's next line, and whether they are done. `transcript` is (speaker, text)."""
    messages = [{"role": "system", "content": caller_prompt(scenario, tenant)}]
    for speaker, text in transcript:
        # From the caller's side, the receptionist is the "user" talking to them.
        messages.append(
            {"role": "user" if speaker == "agent" else "assistant", "content": text or "..."}
        )
    reply = await llm.chat(messages)
    done = END in reply
    return reply.replace(END, "").strip(), done
