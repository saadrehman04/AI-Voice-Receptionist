import logging

import pytest

from receptionist_agent.__main__ import describe_providers, main
from receptionist_shared import Settings


def test_describe_providers_lists_choices() -> None:
    s = Settings(stt_provider="faster_whisper", llm_provider="ollama", tts_provider="piper")
    assert describe_providers(s) == "stt=faster_whisper llm=ollama tts=piper"


def test_describe_providers_never_includes_secrets() -> None:
    s = Settings(groq_api_key="gsk_super_secret")  # type: ignore[arg-type]
    assert "gsk_super_secret" not in describe_providers(s)


def test_main_runs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="receptionist_agent"):
        main()
    assert "agent placeholder started" in caplog.text
