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
