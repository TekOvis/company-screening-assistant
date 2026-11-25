"""The server files in deploy/ agree with each other and with the app's settings."""

import re
import subprocess
import sys
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


def test_the_venv_is_never_moved_and_the_service_does_not_rely_on_its_scripts():
    """A first install failed with 203/EXEC: the venv was built in a temporary directory
    and moved, which left venv/bin/uvicorn pointing at a missing interpreter."""
    install, unit = _read("install.sh"), _read("company-screener.service")
    assert ".partial" not in install and not re.search(r"^\s*mv .*RELEASE", install, re.M)
    assert 'python3 -m venv "$RELEASE/venv"' in install
    assert "ExecStart=/opt/company-screener/current/venv/bin/python -m uvicorn " in unit


def test_restart_loops_are_bounded():
    unit = _read("company-screener.service")
    assert "StartLimitIntervalSec=600" in unit and "StartLimitBurst=5" in unit


def test_health_check_follows_the_agreed_rules():
    """Every minute, 5 s timeout, restart only after 3 failures in a row, at most 3
    restarts per hour, and never start a demo that was stopped on purpose."""
    script = _read("healthcheck.sh")
    timer = _read("company-screener-health.timer")
    unit = _read("company-screener-health.service")

    assert "OnCalendar=minutely" in timer
    assert "URL=${HEALTH_URL:-http://127.0.0.1:8001/healthz}" in script
    for setting in (
        "TIMEOUT_S=5",
        "FAILURES_BEFORE_RESTART=3",
        "MAX_RESTARTS=3",
        "RESTART_WINDOW_S=3600",
    ):
        assert re.search(rf"^{setting}$", script, re.MULTILINE), setting
    assert '--max-time "$TIMEOUT_S"' in script

    # Looks at the service before checking it, and only ever uses try-restart.
    assert script.index("systemctl is-active") < script.index("curl ")
    assert "systemctl try-restart --no-block" in script
    assert not re.search(r"systemctl\s+(?:--\S+\s+)*(start|restart|enable|reload)\b", script)
    # The check unit must not pull the demo in.
    assert not re.search(r"^(Wants|Requires|BindsTo|PartOf)=", unit, re.MULTILINE)
    assert "Type=oneshot" in unit
    assert "ExecStart=/opt/company-screener/bin/healthcheck.sh" in unit
    assert "StateDirectory=company-screener-health" in unit


def test_install_script_installs_the_health_check_outside_the_releases():
    install = _read("install.sh")
    assert "HEALTH_SCRIPT=$BASE/bin/healthcheck.sh" in install
    assert 'deploy/healthcheck.sh" "$HEALTH_SCRIPT"' in install
    for name in ("company-screener-health.service", "company-screener-health.timer"):
        assert f'deploy/{name}"' in install
    assert 'systemd-analyze verify "$UNIT" "$HEALTH_UNIT" "$HEALTH_TIMER"' in install


def test_caddy_detach_removes_only_the_screener_block():
    detach = _read("caddy-detach.sh")
    assert "SITE=screener.rustwatchtower.net" in detach
    assert "--dry-run" in detach
    assert "runuser -u caddy -- caddy validate" in detach
    assert detach.index("caddy validate") < detach.index("systemctl reload caddy")


def _detach(tmp_path: Path, caddyfile: str) -> subprocess.CompletedProcess[str]:
    """Run the block-removal step of caddy-detach.sh on a sample Caddyfile."""
    code = _read("caddy-detach.sh").split("<<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
    (tmp_path / "Caddyfile").write_text(caddyfile, encoding="utf-8", newline="\n")
    return subprocess.run(  # noqa: S603 - our own interpreter and code
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path / "Caddyfile"),
            str(tmp_path / "new"),
            "screener.rustwatchtower.net",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


OTHER_SITES = (
    "app.example.org {\n\treverse_proxy 127.0.0.1:9000\n\theader {\n\t\t-Server\n\t}\n}\n\n"
    "static.example.org {\n\troot * /var/www/static\n\tfile_server\n}\n"
)


def test_caddy_detach_keeps_every_other_site(tmp_path):
    later_site = "\nlater.example {\n\trespond hi\n}\n"
    for before, after in ((OTHER_SITES, ""), (OTHER_SITES, later_site)):
        result = _detach(tmp_path, before + "\n" + _read("screener.caddy") + after)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "new").read_text(encoding="utf-8") == before + after


def test_caddy_detach_changes_nothing_without_exactly_one_block(tmp_path):
    result = _detach(tmp_path, OTHER_SITES)
    assert result.returncode != 0 and "found 0" in result.stderr
    assert not (tmp_path / "new").exists()


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
