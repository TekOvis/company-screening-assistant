"""Public-demo protection: access token, per-IP limits, daily budget, safe settings."""

import pytest
from fastapi.testclient import TestClient

from screener.demo import SESSION_COOKIE, DailyBudget, RateLimiter, session_value
from screener.fake_llm import FakeAnthropicClient
from screener.fixtures import FixtureFetcher
from screener.web import create_app

from .conftest import ACME_HOME, make_settings

TOKEN = "demo-token-for-tests-1234"
FORM = {
    "urls": "https://acme-valves.example/",
    "industry": "industrial valves",
    "geography": "Germany",
    "target_business": "Manufacturer of industrial valves",
    "exclusions": "",
}
API_PAYLOAD = {
    "urls": ["https://acme-valves.example/"],
    "criteria": {
        "industry": "industrial valves",
        "geography": "Germany",
        "target_business": "Manufacturer of industrial valves",
    },
}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _app(**overrides):
    settings = make_settings(**overrides)
    pages = {"https://acme-valves.example/": ACME_HOME}
    return create_app(
        settings,
        client=FakeAnthropicClient(),
        fetcher_factory=lambda: FixtureFetcher(pages, settings),
    )


def test_rate_limiter_uses_a_sliding_window():
    clock = _Clock()
    limiter = RateLimiter(limit=2, window_s=60, clock=clock)
    assert limiter.allow("ip") and limiter.allow("ip")
    assert not limiter.allow("ip")
    assert limiter.retry_after_s("ip") == 61
    assert limiter.allow("other-ip")  # limits are per key
    clock.now = 60
    assert limiter.allow("ip")


def test_daily_budget_caps_runs_by_what_is_left():
    day = {"value": "2026-11-25"}
    budget = DailyBudget(max_companies=3, max_tokens=100_000, today=lambda: day["value"])
    cap, refusal = budget.reserve(2, run_token_cap=80_000)
    assert (cap, refusal) == (80_000, None)
    cap2, refusal2 = budget.reserve(1, run_token_cap=80_000)
    assert refusal2 is None and cap2 == 20_000  # only what is left for the day
    assert budget.reserve(1, 80_000)[1] is not None  # no companies left
    budget.settle(80_000, 5_000)
    assert budget.remaining_tokens() == 100_000 - 5_000 - 20_000
    day["value"] = "2026-11-26"
    assert budget.reserve(3, 80_000)[1] is None  # a new day


@pytest.mark.parametrize(
    "overrides",
    [
        {"public_demo": True},  # no token
        {"public_demo": True, "demo_access_token": "short"},
        {"public_demo": True, "demo_access_token": TOKEN, "llm_backend": "claude_code"},
    ],
)
def test_public_demo_settings_must_be_safe(overrides):
    with pytest.raises(ValueError):
        make_settings(**overrides)


def test_without_a_token_everything_is_open():
    with TestClient(_app()) as client:
        assert client.get("/").status_code == 200
        assert client.get("/login", follow_redirects=False).status_code == 303


def test_pages_and_api_require_the_token():
    with TestClient(_app(demo_access_token=TOKEN)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/static/style.css").status_code == 200
        page = client.get("/", follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"] == "/login"
        assert client.post("/api/screen", json=API_PAYLOAD).status_code == 401
        assert client.get("/runs/x/export.json").status_code == 401
        assert client.get("/openapi.json").status_code == 401

        bearer = {"Authorization": f"Bearer {TOKEN}"}
        assert client.post("/api/screen", json=API_PAYLOAD, headers=bearer).status_code == 200
        wrong = {"Authorization": "Bearer wrong-token"}
        assert client.post("/api/screen", json=API_PAYLOAD, headers=wrong).status_code == 401


def test_login_sets_a_session_cookie_and_logout_clears_it():
    with TestClient(_app(demo_access_token=TOKEN)) as client:
        assert client.post("/login", data={"token": "nope"}).status_code == 401
        response = client.post("/login", data={"token": TOKEN}, follow_redirects=False)
        assert response.status_code == 303
        cookie = response.cookies.get(SESSION_COOKIE)
        assert cookie == session_value(TOKEN) and TOKEN not in cookie
        assert "httponly" in response.headers["set-cookie"].lower()
        assert client.get("/").status_code == 200
        client.post("/logout")
        assert client.get("/", follow_redirects=False).status_code == 303


def test_login_attempts_are_limited():
    with TestClient(_app(demo_access_token=TOKEN, login_attempts_per_ip_per_15_min=2)) as client:
        assert client.post("/login", data={"token": "a"}).status_code == 401
        assert client.post("/login", data={"token": "b"}).status_code == 401
        blocked = client.post("/login", data={"token": TOKEN})
        assert blocked.status_code == 429 and "Retry-After" in blocked.headers


def test_screenings_per_ip_are_limited():
    with TestClient(_app(runs_per_ip_per_hour=1)) as client:
        assert client.post("/screen", data=FORM, follow_redirects=False).status_code == 303
        second = client.post("/screen", data=FORM)
        assert second.status_code == 429 and "screenings per hour" in second.text
        assert client.post("/api/screen", json=API_PAYLOAD).status_code == 429


def test_daily_company_limit():
    with TestClient(_app(daily_max_companies=1)) as client:
        two = {**FORM, "urls": "https://acme-valves.example/\nhttps://other.example/"}
        response = client.post("/screen", data=two)
        assert response.status_code == 429 and "daily limit" in response.text


def test_daily_token_budget_caps_each_run():
    # 10,000 tokens for the whole day cannot cover even one company's reservation.
    with TestClient(_app(daily_max_tokens=10_000)) as client:
        body = client.post("/api/screen", json=API_PAYLOAD).json()
        assert body["results"][0]["status"] == "budget_exceeded"
        assert "token limit per run" in body["results"][0]["error"]
