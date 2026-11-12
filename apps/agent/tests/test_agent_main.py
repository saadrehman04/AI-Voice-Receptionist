# Imported here, not inside the tests: importing Pipecat's runner loads .env into os.environ,
# which would undo the fixture below.
import pipecat.runner.run as runner
import pytest
from pydantic import SecretStr

import receptionist_agent.local_stt as local_stt
from receptionist_agent import __main__ as agent_main
from receptionist_shared import Settings


def test_runner_argv_forces_webrtc_and_our_host_port() -> None:
    argv = agent_main.runner_argv("127.0.0.1", 7861, ["-v"])
    assert argv[1:] == ["-t", "webrtc", "--host", "127.0.0.1", "--port", "7861", "-v"]


@pytest.fixture(autouse=True)
def no_env_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_main_exits_early_on_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicitly chosen provider without its key is a mistake worth stopping for.
    settings = Settings(_env_file=None, llm_provider="gemini")
    monkeypatch.setattr(agent_main, "get_settings", lambda: settings)
    with pytest.raises(SystemExit) as exc:
        agent_main.main()
    assert exc.value.code == 2


def test_main_starts_without_any_key_and_preloads_local_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 9 (D37): no keys at all still answers calls (local STT, scripted message-taker)."""
    started: list[str] = []
    monkeypatch.setattr(agent_main, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(runner, "main", lambda: started.append("runner"))
    monkeypatch.setattr(local_stt, "preload", lambda model: started.append(f"preload {model}"))
    monkeypatch.setattr("receptionist_agent.whatsapp.routes.install", lambda app, settings: None)
    monkeypatch.setattr("sys.argv", ["receptionist-agent"])
    agent_main.main()
    # No Groq key: STT is local already, so nothing to preload; the runner starts.
    assert started == ["runner"]


def test_main_preloads_the_fallback_whisper_when_groq_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []
    settings = Settings(_env_file=None, groq_api_key=SecretStr("gsk_test"))
    monkeypatch.setattr(agent_main, "get_settings", lambda: settings)
    monkeypatch.setattr(runner, "main", lambda: started.append("runner"))
    monkeypatch.setattr(local_stt, "preload", lambda model: started.append(f"preload {model}"))
    monkeypatch.setattr("receptionist_agent.whatsapp.routes.install", lambda app, settings: None)
    monkeypatch.setattr("sys.argv", ["receptionist-agent"])
    agent_main.main()
    assert started == ["preload base", "runner"]


def test_bot_is_exported_for_the_runner() -> None:
    # Pipecat's runner looks up `bot` on the __main__ module.
    assert callable(agent_main.bot)
