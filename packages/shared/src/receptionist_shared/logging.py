"""Structured logging for every service.

Pipecat logs through loguru, so we configure loguru once: one JSON object per line on
stderr, with `call_id` / `turn_id` lifted to the top level when they are bound, e.g.

    with logger.contextualize(call_id=call_id):
        ...  # every log line inside (including Pipecat's) carries call_id

`log_json=False` switches to loguru's coloured human format for local debugging.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any, TextIO

from loguru import logger

if TYPE_CHECKING:
    from loguru import Message, Record

# Keys we always surface at the top level of a JSON log line when present.
CONTEXT_KEYS = ("call_id", "turn_id", "tenant_id")


def format_record(record: Record) -> str:
    """Render one loguru record as a compact JSON line (no trailing newline)."""
    extra: dict[str, Any] = dict(record["extra"])
    line: dict[str, Any] = {
        "ts": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "msg": record["message"],
    }
    for key in CONTEXT_KEYS:
        if key in extra:
            line[key] = extra.pop(key)
    if extra:
        line["extra"] = extra
    if record["exception"] is not None:
        exc = record["exception"]
        line["exc_type"] = exc.type.__name__ if exc.type else None
        line["exc"] = str(exc.value)
    return json.dumps(line, default=str, ensure_ascii=False)


def _make_json_sink(stream: TextIO) -> Any:
    def sink(message: Message) -> None:
        stream.write(format_record(message.record) + "\n")
        stream.flush()

    return sink


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    """Replace loguru's default handler with ours. Safe to call more than once."""
    logger.remove()
    if json_logs:
        logger.add(_make_json_sink(sys.stderr), level=level, backtrace=False, diagnose=False)
    else:
        logger.add(sys.stderr, level=level, backtrace=False, diagnose=False)
