"""Structured logging for every service.

Pipecat logs through loguru, so we configure loguru once: one JSON object per line on
stderr, with `call_id` / `turn_id` lifted to the top level when they are bound, e.g.

    with logger.contextualize(call_id=call_id):
        ...  # every log line inside (including Pipecat's) carries call_id

`log_json=False` switches to loguru's coloured human format for local debugging.

Secrets never reach a log line (Phase 9): a patcher masks the values of every secret in
Settings, plus anything shaped like a provider key or bearer token, in the message, the
bound extras and exception text, before any sink sees the record.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, TextIO

from loguru import logger

if TYPE_CHECKING:
    from loguru import Message, Record

MASK = "[redacted]"
# Shapes of real credentials, masked even if they aren't in Settings (a pasted key, a header).
KEY_PATTERNS = re.compile(
    r"gsk_[A-Za-z0-9]{20,}"  # Groq
    r"|AIza[0-9A-Za-z_\-]{30,}"  # Google / Gemini
    r"|EAA[A-Za-z0-9]{30,}"  # Meta access tokens
    r"|sk-[A-Za-z0-9_\-]{20,}"  # OpenAI-style keys
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]{16,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"  # JWTs
)
MIN_SECRET_LEN = 8  # shorter "secrets" would mask ordinary words


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values = sorted({s for s in secrets if len(s) >= MIN_SECRET_LEN}, key=len, reverse=True)
        self._values = re.compile("|".join(map(re.escape, values))) if values else None

    def text(self, value: str) -> str:
        if self._values is not None:
            value = self._values.sub(MASK, value)
        return KEY_PATTERNS.sub(MASK, value)

    def any(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.any(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return type(value)(self.any(v) for v in value)
        return value

    def __call__(self, record: Record) -> None:
        record["message"] = self.text(record["message"])
        record["extra"].update({k: self.any(v) for k, v in record["extra"].items()})
        exc = record["exception"]
        if exc is not None and exc.value is not None:
            masked = self.text(str(exc.value))
            if masked != str(exc.value):
                # Keep the type if it can be rebuilt from text; lose the secret either way.
                try:
                    value: BaseException = (exc.type or RuntimeError)(masked)
                except Exception:
                    value = RuntimeError(masked)
                record["exception"] = exc._replace(value=value)


def settings_secrets() -> list[str]:
    """Every SecretStr value in Settings (empty if settings can't be loaded)."""
    try:
        from pydantic import SecretStr

        from receptionist_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return []
    out = []
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr):
            out.append(value.get_secret_value())
    return out


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


class InterceptHandler(logging.Handler):
    """Send stdlib `logging` records (uvicorn, sqlalchemy, httpx...) through loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(depth=6, exception=record.exc_info).bind(stdlib=record.name).log(
            level, record.getMessage()
        )


def configure_logging(
    level: str = "INFO", *, json_logs: bool = True, secrets: Iterable[str] | None = None
) -> None:
    """Replace loguru's default handler with ours and route stdlib logging into it.

    `secrets`: values to mask; default: every secret in Settings. Safe to call more than once.
    """
    logger.remove()
    logger.configure(patcher=Redactor(settings_secrets() if secrets is None else secrets))
    if json_logs:
        logger.add(_make_json_sink(sys.stderr), level=level, backtrace=False, diagnose=False)
    else:
        logger.add(sys.stderr, level=level, backtrace=False, diagnose=False)
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        std = logging.getLogger(name)
        std.handlers = [InterceptHandler()]
        std.propagate = False
