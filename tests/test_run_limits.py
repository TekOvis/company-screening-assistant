"""A screening cannot hold its slot forever: time limit, errors, and the slot afterwards."""

import asyncio

from fastapi.testclient import TestClient

from screener import web
from screener.fake_llm import FakeAnthropicClient
from screener.fixtures import FixtureFetcher
from screener.web import create_app

from .conftest import ACME_HOME, make_settings
from .test_web import _wait_until_done

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


class HangingFetcher(FixtureFetcher):
    """A site that never answers: only the run's time limit can end the wait."""

    async def fetch(self, url, *, same_site_as=None):
        await asyncio.sleep(3600)


def _app(fetcher_cls=FixtureFetcher, **overrides):
    settings = make_settings(**{"max_active_runs": 1, "run_timeout_s": 0.3, **overrides})
    pages = {"https://acme-valves.example/": ACME_HOME}
    return create_app(
        settings,
        client=FakeAnthropicClient(),
        fetcher_factory=lambda: fetcher_cls(pages, settings),
    )


def test_a_hanging_screening_stops_at_the_time_limit_and_frees_its_slot():
    with TestClient(_app(HangingFetcher)) as client:
        first = client.post("/screen", data=FORM, follow_redirects=False)
        assert first.status_code == 303
        # While it runs, the only slot is taken.
        assert client.post("/screen", data=FORM).status_code == 429
        html = _wait_until_done(client, first.headers["location"])
        assert "Stopped early: the time limit of 0.3 s per screening was reached" in html
        run = client.get(first.headers["location"] + "/export.json").json()
        assert run["status"] == "done" and run["results"] == []
        # The slot is free again.
        assert client.post("/screen", data=FORM, follow_redirects=False).status_code == 303


def test_the_api_returns_at_the_time_limit_and_frees_its_slot():
    with TestClient(_app(HangingFetcher)) as client:
        body = client.post("/api/screen", json=API_PAYLOAD).json()
        assert body["status"] == "done"
        assert "time limit" in body["stop_reason"] and body["results"] == []
        assert client.post("/api/screen", json=API_PAYLOAD).status_code == 200


def test_an_internal_error_fails_the_run_and_frees_its_slot(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web, "screen_companies", broken)
    with TestClient(_app()) as client:
        first = client.post("/screen", data=FORM, follow_redirects=False)
        html = _wait_until_done(client, first.headers["location"])
        assert "internal error while processing the run" in html
        assert "boom" not in html  # internal details are not shown
        assert client.post("/screen", data=FORM, follow_redirects=False).status_code == 303


def test_a_normal_run_is_not_affected_by_the_limit():
    with TestClient(_app(run_timeout_s=30)) as client:
        first = client.post("/screen", data=FORM, follow_redirects=False)
        _wait_until_done(client, first.headers["location"])
        run = client.get(first.headers["location"] + "/export.json").json()
        assert run["stop_reason"] is None and run["results"][0]["status"] == "ok"
