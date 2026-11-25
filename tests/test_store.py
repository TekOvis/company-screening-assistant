"""The in-memory job store is bounded: count, time to live, and runs in progress."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from screener.fake_llm import FakeAnthropicClient
from screener.fixtures import FixtureFetcher
from screener.schemas import ScreeningCriteria, ScreeningRun
from screener.web import BUSY_MESSAGE, RunStore, StoreFullError, create_app

from .conftest import make_settings

CRITERIA = ScreeningCriteria(industry="valves", geography="Germany", target_business="valves")


def _run(run_id: str, status: str = "done") -> ScreeningRun:
    return ScreeningRun(
        run_id=run_id, status=status, backend="mock", model="mock", criteria=CRITERIA, urls=[]
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_finished_runs_expire_after_the_ttl():
    clock = _Clock()
    store = RunStore(max_runs=10, ttl_s=60, clock=clock)
    store.add(_run("old"))
    store.add(_run("busy", status="running"))
    clock.now += 61
    assert store.get("old") is None  # expired
    assert store.get("busy") is not None  # runs in progress never expire


def test_oldest_finished_runs_are_dropped_first():
    store = RunStore(max_runs=3, ttl_s=3600, clock=_Clock())
    store.add(_run("running-1", status="running"))
    store.add(_run("done-1"))
    store.add(_run("done-2"))
    store.add(_run("new"))
    assert store.get("done-1") is None
    assert {"running-1", "done-2", "new"} == {
        r for r in ("running-1", "done-2", "new") if store.get(r)
    }
    assert len(store) == 3


def test_store_refuses_when_every_run_is_in_progress():
    store = RunStore(max_runs=2, ttl_s=3600, clock=_Clock())
    store.add(_run("a", status="running"))
    store.add(_run("b", status="running"))
    with pytest.raises(StoreFullError):
        store.add(_run("c"))
    assert store.active_count() == 2


class _HangingFetcher(FixtureFetcher):
    """Keeps a run 'in progress' until the app shuts down and cancels it."""

    async def fetch(self, url, *, same_site_as=None):
        await asyncio.sleep(3600)


def test_too_many_active_runs_get_429():
    settings = make_settings(max_active_runs=1, max_stored_runs=5)
    app = create_app(
        settings,
        client=FakeAnthropicClient([]),
        fetcher_factory=lambda: _HangingFetcher({}, settings),
    )
    form = {
        "urls": "https://slow.example/",
        "industry": "valves",
        "geography": "Germany",
        "target_business": "valves",
        "exclusions": "",
    }
    with TestClient(app) as client:
        first = client.post("/screen", data=form, follow_redirects=False)
        assert first.status_code == 303
        second = client.post("/screen", data=form)
        assert second.status_code == 429
        assert BUSY_MESSAGE in second.text
        api = client.post(
            "/api/screen",
            json={"urls": ["https://slow.example/"], "criteria": CRITERIA.model_dump()},
        )
        assert api.status_code == 429


def test_settings_reject_inconsistent_store_limits():
    with pytest.raises(ValueError):
        make_settings(max_active_runs=5, max_stored_runs=5)
