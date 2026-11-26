"""A page whose parsing never ends must not stop the screenings that come after it.

The first screening's page never finishes parsing, and the screening ends at a time limit
(the screening's or the page's). Then a second screening of a normal page arrives. It must
actually be processed, not just get a free slot, and the stuck parser must be gone, not
left running next to a new one. With the earlier single parser thread, this test failed:
the second screening hit its time limit too, because its page waited behind the stuck one.
"""

import multiprocessing

import httpx
import pytest
from fastapi.testclient import TestClient

from screener.fake_llm import FakeAnthropicClient
from screener.fetcher import HttpFetcher
from screener.page_parser import PageParser
from screener.web import create_app

from . import parse_targets
from .conftest import ACME_HOME, make_settings

PUBLIC_IP = "93.184.216.34"
STUCK_HOME = (
    f"<html><body>{parse_targets.HANG.decode()}<p>We make industrial valves.</p></body></html>"
)
CRITERIA = {
    "industry": "industrial valves",
    "geography": "Germany",
    "target_business": "Manufacturer of industrial valves",
}


@pytest.fixture
def parser():
    parser = PageParser(target=parse_targets.extract_for_tests)
    yield parser
    parser.stop()


def _handler(request: httpx.Request) -> httpx.Response:
    body = STUCK_HOME if request.headers["host"] == "stuck-site.com" else ACME_HOME
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/html"})


async def _resolver(host, port):
    return [PUBLIC_IP]


def _children() -> int:
    return len(multiprocessing.active_children())


@pytest.mark.parametrize(
    ("limit", "overrides"),
    [
        ("screening", {"run_timeout_s": 3, "fetch_total_timeout_s": 30}),
        ("page", {"run_timeout_s": 30, "fetch_total_timeout_s": 1}),
    ],
)
def test_a_stuck_parse_does_not_stop_later_screenings(parser, limit, overrides):
    settings = make_settings(max_active_runs=1, **overrides)
    app = create_app(
        settings,
        client=FakeAnthropicClient(),
        fetcher_factory=lambda: HttpFetcher(
            settings, resolver=_resolver, transport=httpx.MockTransport(_handler), parser=parser
        ),
    )
    children_before = _children()
    with TestClient(app) as client:
        stuck = client.post(
            "/api/screen", json={"urls": ["https://stuck-site.com/"], "criteria": CRITERIA}
        ).json()
        if limit == "screening":
            assert "time limit" in stuck["stop_reason"] and stuck["results"] == []
        else:
            assert stuck["results"][0]["status"] == "fetch_error"
            assert "timeout" in stuck["results"][0]["error"]
        # The stuck parser was stopped, not left running.
        assert parser.pid is None and _children() == children_before

        normal = client.post(
            "/api/screen", json={"urls": ["https://acme.com/"], "criteria": CRITERIA}
        ).json()
        assert normal["stop_reason"] is None
        [result] = normal["results"]
        assert result["status"] == "ok", result["error"]
        assert result["pages_fetched"] == ["https://acme.com/"]
        assert result["fit"] is not None
        # One fresh parser, no leftovers.
        assert parser.started == 2 and _children() == children_before + 1
