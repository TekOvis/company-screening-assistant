"""The server files in deploy/ agree with each other and with the app's settings."""

import re
import tomllib
from pathlib import Path

from screener.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def _read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_server_settings_are_a_safe_mock_demo(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_TOKEN", "token-filled-in-by-install-sh")
    settings = Settings(_env_file=DEPLOY / "screener.env.example")
    assert settings.llm_backend == "mock"
    assert settings.public_demo and settings.cookie_secure and settings.demo_examples
    assert settings.anthropic_api_key is None
    assert settings.max_active_runs == 1 and settings.concurrency == 2
    assert settings.max_urls_per_request == 10 and settings.fetch_max_bytes == 1_000_000
    assert "ANTHROPIC" not in _read("screener.env.example").upper().replace(
        "NEVER PUT AN API KEY", ""
    )


def test_one_port_on_loopback_everywhere():
    unit, caddy, install = (
        _read("company-screener.service"),
        _read("screener.caddy"),
        _read("install.sh"),
    )
    assert "--host 127.0.0.1 --port 8001" in unit
    assert "--forwarded-allow-ips 127.0.0.1" in unit
    assert "reverse_proxy 127.0.0.1:8001" in caddy
    assert "PORT=8001" in install
    assert "screener.rustwatchtower.net {" in caddy


def test_unit_has_resource_limits_and_runs_unprivileged():
    unit = _read("company-screener.service")
    for line in (
        "User=screener",
        "MemoryMax=300M",
        "CPUQuota=50%",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "EnvironmentFile=/etc/company-screener/screener.env",
    ):
        assert line in unit
    # Without swap, MemoryHigh throttled a test process into a hang on the server.
    assert not re.search(r"^MemoryHigh=", unit, re.MULTILINE)


def test_install_script_activates_nothing():
    commands = [
        line.strip()
        for line in _read("install.sh").splitlines()
        if line.strip() and not line.strip().startswith(("#", "echo"))
    ]
    activating = re.compile(r"systemctl\s+(enable|start|restart|reload)|\bcaddy\b|/etc/caddy")
    assert [c for c in commands if activating.search(c)] == []


def test_requirements_match_the_lock_file_and_hold_no_dev_tools():
    lock = {
        p["name"].lower(): p["version"]
        for p in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    }
    pinned = dict(
        re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", _read("requirements.txt"), re.MULTILINE)
    )
    assert pinned, "no pinned packages found"
    assert {name.lower(): version for name, version in pinned.items()}.items() <= lock.items()
    assert not {"pytest", "ruff", "claude-agent-sdk"} & {name.lower() for name in pinned}
    assert "--hash=sha256:" in _read("requirements.txt")
