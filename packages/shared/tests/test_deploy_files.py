"""deploy/ (Caddyfile, systemd units) stays in step with the code it starts and protects.

These files only run on a server, so nothing else would notice a renamed module, a moved port
or a real key pasted into the repo's copy.
"""

import importlib.util
import re
from pathlib import Path

from receptionist_shared import Settings

REPO = Path(__file__).resolve().parents[3]
CADDYFILE = (REPO / "deploy" / "Caddyfile").read_text()
UNITS = {p.stem: p.read_text() for p in (REPO / "deploy" / "systemd").glob("*.service")}
TEST_CALL_PROXY = (REPO / "apps/web/src/app/api/test-call/[...path]/route.ts").read_text()
WHATSAPP_ROUTES = (REPO / "apps/agent/src/receptionist_agent/whatsapp/routes.py").read_text()


def _site(name: str) -> str:
    """The block for one site in the Caddyfile (up to the next top-level closing brace)."""
    match = re.search(rf"^{re.escape(name)} \{{\n(.*?)^\}}", CADDYFILE, re.M | re.S)
    assert match, f"no {name} site in deploy/Caddyfile"
    return match.group(1)


def test_caddy_proxies_to_the_ports_the_services_listen_on() -> None:
    defaults = Settings(_env_file=None)
    assert f"reverse_proxy 127.0.0.1:{defaults.api_port}" in _site("api.example.com")
    assert f"reverse_proxy 127.0.0.1:{defaults.agent_port}" in _site("agent.example.com")
    assert f"--port {defaults.api_port}" in UNITS["receptionist-api"]


def test_only_the_dashboard_and_meta_reach_the_agent() -> None:
    agent = _site("agent.example.com")
    assert "@calls path /start /sessions/*" in agent
    assert "@nokey not header X-Agent-Key REPLACE_WITH_AGENT_KEY" in agent
    assert "handle /whatsapp {" in agent
    assert re.search(r"handle \{\s*respond 404\s*\}", agent)  # /client and the rest
    assert '@app.post("/whatsapp"' in WHATSAPP_ROUTES  # the path Caddy lets through
    assert '"X-Agent-Key": AGENT_KEY' in TEST_CALL_PROXY  # the header Caddy checks


def test_the_repo_copy_keeps_placeholders_not_real_values() -> None:
    for placeholder in ("api.example.com", "agent.example.com", "REPLACE_WITH_AGENT_KEY"):
        assert placeholder in CADDYFILE


def test_each_unit_starts_a_module_that_exists() -> None:
    assert set(UNITS) == {"receptionist-api", "receptionist-worker", "receptionist-agent"}
    for name, unit in UNITS.items():
        exec_start = re.search(r"^ExecStart=(.+)$", unit, re.M)
        assert exec_start, name
        command = exec_start.group(1)
        assert command.startswith("/usr/local/bin/uv run --all-packages --no-sync "), name
        module = re.search(r"(?:python -m |uvicorn )([\w.]+)", command)
        assert module, name
        assert importlib.util.find_spec(module.group(1)) is not None, f"{name}: {module.group(1)}"
        assert "WorkingDirectory=/opt/voice-receptionist" in unit, name
        assert "Restart=always" in unit, name
