import pytest

from receptionist_agent import __main__ as agent_main
from receptionist_shared import Settings


def test_runner_argv_forces_webrtc_and_our_host_port() -> None:
    argv = agent_main.runner_argv("127.0.0.1", 7861, ["-v"])
    assert argv[1:] == ["-t", "webrtc", "--host", "127.0.0.1", "--port", "7861", "-v"]


def test_main_exits_early_without_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_main, "get_settings", lambda: Settings(_env_file=None))
    with pytest.raises(SystemExit) as exc:
        agent_main.main()
    assert exc.value.code == 2


def test_bot_is_exported_for_the_runner() -> None:
    # Pipecat's runner looks up `bot` on the __main__ module.
    assert callable(agent_main.bot)
