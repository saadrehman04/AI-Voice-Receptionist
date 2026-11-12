import io
import json

from loguru import logger

from receptionist_shared.logging import _make_json_sink, configure_logging


def _capture() -> tuple[io.StringIO, int]:
    buf = io.StringIO()
    handler_id = logger.add(_make_json_sink(buf), level="DEBUG")
    return buf, handler_id


def test_json_line_has_context_ids_at_top_level() -> None:
    buf, handler_id = _capture()
    try:
        with logger.contextualize(call_id="c-1", turn_id="t-2"):
            logger.bind(stage="stt").info("hello")
    finally:
        logger.remove(handler_id)
    line = json.loads(buf.getvalue().strip())
    assert line["msg"] == "hello"
    assert line["level"] == "INFO"
    assert line["call_id"] == "c-1"
    assert line["turn_id"] == "t-2"
    assert line["extra"] == {"stage": "stt"}


def test_exception_is_recorded_without_traceback_noise() -> None:
    buf, handler_id = _capture()
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")
    finally:
        logger.remove(handler_id)
    line = json.loads(buf.getvalue().strip())
    assert line["exc_type"] == "ValueError"
    assert line["exc"] == "boom"


def test_configure_logging_is_idempotent() -> None:
    configure_logging("WARNING", json_logs=True)
    configure_logging("INFO", json_logs=False)
    configure_logging("INFO", json_logs=True)


def test_stdlib_logging_is_routed_to_json() -> None:
    import logging

    configure_logging("INFO", json_logs=True)
    buf, handler_id = _capture()
    try:
        logging.getLogger("uvicorn.error").warning("from stdlib")
    finally:
        logger.remove(handler_id)
    line = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert line["msg"] == "from stdlib"
    assert line["extra"]["stdlib"] == "uvicorn.error"


def test_secrets_never_reach_a_log_line() -> None:
    """Phase 9: configured secret values and key-shaped strings are masked everywhere."""
    configure_logging("DEBUG", secrets=["my-agent-token-0123456789", "short"])
    buf, handler_id = _capture()
    groq_like = "gsk_" + "A1b2C3d4" * 4  # built here so this file holds no key-shaped text
    try:
        logger.bind(header=f"Bearer {'x' * 20}", nested={"k": [groq_like]}).info(
            "token my-agent-token-0123456789 and short words stay"
        )
        try:
            raise RuntimeError(f"401 for key {groq_like}")
        except RuntimeError:
            logger.exception("provider failed")
    finally:
        logger.remove(handler_id)
        configure_logging("INFO", secrets=[])
    out = buf.getvalue()
    assert "my-agent-token-0123456789" not in out
    assert groq_like not in out
    assert "x" * 20 not in out
    assert "[redacted]" in out
    assert "short words stay" in out  # values under 8 characters are not masked
    first, second = (json.loads(line) for line in out.strip().splitlines())
    assert first["extra"]["nested"] == {"k": ["[redacted]"]}
    assert second["exc_type"] == "RuntimeError" and second["exc"] == "401 for key [redacted]"
