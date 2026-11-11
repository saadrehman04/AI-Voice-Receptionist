"""Chat with the receptionist in the terminal (text only, real LLM and tools, stub bookings).

    make chat                      # default tenant
    make chat ARGS="--tenant demo-workshop"

Type a message and press Enter; tool calls are printed so you can see what the agent did.
Ctrl-D or /quit to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

from receptionist_agent.brain.backend import InMemoryBackend
from receptionist_agent.brain.prompt import build_system_prompt
from receptionist_agent.brain.state import CallState
from receptionist_agent.brain.text_session import TextSession
from receptionist_agent.brain.tools import ReceptionistTools
from receptionist_agent.providers import ProviderConfigError, build_llm
from receptionist_shared import get_settings
from receptionist_shared.logging import configure_logging
from receptionist_shared.tenant import load_tenant_by_slug


async def chat(tenant_slug: str | None) -> int:
    settings = get_settings()
    configure_logging("WARNING", json_logs=settings.log_json)
    tenant = load_tenant_by_slug(tenant_slug or settings.default_tenant, Path(settings.tenants_dir))
    try:
        llm = build_llm(settings, build_system_prompt(tenant, tenant.now()))
    except ProviderConfigError as exc:
        print(exc)
        return 2
    state = CallState()
    tools = ReceptionistTools(tenant, state, InMemoryBackend(), call_id=uuid.uuid4().hex)
    async with TextSession(tenant=tenant, llm=llm, tools=tools, state=state) as session:
        print(f"[{tenant.business_name}] {session.greeting}")
        while True:
            try:
                line = await asyncio.to_thread(input, "you> ")
            except EOFError:
                break
            if line.strip() in ("/quit", "/exit"):
                break
            if not line.strip():
                continue
            reply = await session.send(line)
            for call in reply.tool_calls:
                args = json.dumps(call.arguments, ensure_ascii=False)
                result = json.dumps(call.result, ensure_ascii=False, default=str)[:300]
                print(f"  [tool] {call.name}({args}) -> {result}")
            if reply.text:
                print(f"agent> {reply.text}")
            if reply.ended:
                print("[call ended]")
                break
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat with the receptionist (text mode)")
    parser.add_argument("--tenant", default=None)
    args = parser.parse_args(argv)
    return asyncio.run(chat(args.tenant))


if __name__ == "__main__":
    raise SystemExit(main())
