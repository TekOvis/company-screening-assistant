"""End-to-end tests of the web app in mock mode with saved pages (no API, no internet)."""

import csv
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from screener.export import safe_cell
from screener.fake_llm import FakeAnthropicClient
from screener.fixtures import FixtureFetcher
from screener.web import create_app, parse_url_list, safe_href

from .conftest import ACME_ABOUT, ACME_HOME, make_settings

MARKET_ONLY_HTML = """<html><head><title>Danube Valves | Industrial valves</title></head><body>
<p>We manufacture industrial valves for process industries.</p>
<p>We serve customers in Germany and Austria.</p>
</body></html>"""

FORM = {
    "urls": "https://acme-valves.example/\nhttps://market-only.example/\nhttps://missing.example/",
    "industry": "industrial valves",
    "geography": "Germany",
    "target_business": "Manufacturer of industrial valves",
    "exclusions": "",
}


@pytest.fixture
def client():
    settings = make_settings()
    pages = {
        "https://acme-valves.example/": ACME_HOME,
        "https://acme-valves.example/about": ACME_ABOUT,
        "https://market-only.example/": MARKET_ONLY_HTML,
    }
    app = create_app(
        settings,
        client=FakeAnthropicClient(),
        fetcher_factory=lambda: FixtureFetcher(pages, settings),
    )
    with TestClient(app) as test_client:
        yield test_client


def _wait_until_done(client, run_path: str, timeout_s: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        html = client.get(run_path).text
        if "Processing" not in html:
            return html
        time.sleep(0.05)
    raise AssertionError("run did not finish in time")


def test_index_shows_form_and_mock_banner(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'name="urls"' in response.text
    assert "Mock mode" in response.text


def test_full_flow_form_to_table_and_exports(client):
    response = client.post("/screen", data=FORM, follow_redirects=False)
    assert response.status_code == 303
    run_path = response.headers["location"]

    html = _wait_until_done(client, run_path)
    assert "Acme Valves" in html
    assert "badge-fit" in html
    assert "fetch_error" in html or "fetch error" in html  # the missing site failed cleanly

    exported = json.loads(client.get(f"{run_path}/export.json").text)
    assert exported["status"] == "done"
    acme, market_only, missing = exported["results"]
    assert [r["status"] for r in exported["results"]] == ["ok", "ok", "fetch_error"]
    assert acme["fit"] == "fit"
    assert acme["company_locations"] == ["Stuttgart", "Germany"]
    assert {e["check"] for e in acme["evidence"]} == {"supported"}
    # Serving customers in Germany is a market, not a location: geography stays unknown,
    # and missing information gives insufficient_information, not an automatic non-fit.
    assert market_only["company_locations"] == []
    assert market_only["markets_served"] == ["Germany", "Austria"]
    assert market_only["geography_match"] == "unknown"
    assert market_only["fit"] == "insufficient_information"
    assert missing["fit"] is None
    assert exported["total_usage"]["llm_calls"] == 4  # agent + evidence check, 2 companies
    assert "Not financial" in exported["disclaimer"]

    csv_text = client.get(f"{run_path}/export.csv").text
    rows = list(csv.DictReader(io.StringIO(csv_text.lstrip("﻿"))))
    assert rows[0]["company_name"] == "Acme Valves"
    assert rows[1]["markets_served"] == "Germany; Austria"
    assert rows[2]["status"] == "fetch_error"


def test_form_validation_errors(client):
    too_many = "\n".join(f"https://site{i}.example/" for i in range(11))
    response = client.post("/screen", data={**FORM, "urls": too_many})
    assert response.status_code == 422
    assert "At most 10 URLs" in response.text

    response = client.post(
        "/screen", data={**FORM, "urls": "ftp://acme-valves.example/", "industry": ""}
    )
    assert response.status_code == 422
    assert "only http and https" in response.text
    assert "Industry" in response.text


def test_json_api(client):
    payload = {
        "urls": ["acme-valves.example"],
        "criteria": {
            "industry": "industrial valves",
            "geography": "Germany",
            "target_business": "Manufacturer of industrial valves",
        },
    }
    response = client.post("/api/screen", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["urls"] == ["https://acme-valves.example/"]
    assert body["results"][0]["company_name"] == "Acme Valves"


def test_unknown_run_is_404(client):
    assert client.get("/runs/does-not-exist").status_code == 404


def test_parse_url_list():
    assert parse_url_list(" a.com\nb.com, a.com\n\n c.com ") == ["a.com", "b.com", "c.com"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.com/a", "https://x.com/a"),
        ("javascript:alert(1)", None),
        ("data:text/html,x", None),
        (None, None),
    ],
)
def test_safe_href(url, expected):
    assert safe_href(url) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('=HYPERLINK("x")', '\'=HYPERLINK("x")'),
        ("+1", "'+1"),
        ("-2", "'-2"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("normal", "normal"),
        (3, 3),
    ],
)
def test_csv_cells_are_protected_against_formula_injection(value, expected):
    assert safe_cell(value) == expected
