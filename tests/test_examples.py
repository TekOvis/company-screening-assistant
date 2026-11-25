"""Ready-made synthetic examples and the mock-mode banner."""

from fastapi.testclient import TestClient

from screener.examples import EXAMPLE_TITLES, load_examples
from screener.fake_llm import FakeAnthropicClient
from screener.web import create_app

from .conftest import make_settings
from .test_web import _wait_until_done

FIT_CASE = "01-valves-fit-homepage"
MOCK_MISS_CASE = "06-valves-nonfit-distributor"


def _no_network():
    raise AssertionError("examples must never use the HTTP fetcher")


def _app(**overrides):
    settings = make_settings(**{"demo_examples": True, **overrides})
    return create_app(settings, client=FakeAnthropicClient(), fetcher_factory=_no_network)


def test_all_configured_examples_exist_in_the_case_set():
    assert list(load_examples()) == list(EXAMPLE_TITLES)


def test_examples_are_off_by_default():
    app = create_app(make_settings(), client=FakeAnthropicClient(), fetcher_factory=_no_network)
    with TestClient(app) as client:
        assert client.get("/examples").status_code == 404
        assert client.post(f"/examples/{FIT_CASE}").status_code == 404
        assert "/examples" not in client.get("/").text


def test_mock_mode_is_clearly_labelled_on_every_page():
    with TestClient(_app()) as client:
        for path in ("/", "/examples"):
            html = client.get(path).text
            assert "Mock demo." in html and "does <strong>not</strong> show the quality" in html


def test_examples_page_lists_examples_with_expected_labels():
    with TestClient(_app()) as client:
        html = client.get("/examples").text
        for title in EXAMPLE_TITLES.values():
            assert title.replace("'", "&#39;") in html
        assert "invented company" in html
        assert "Try a ready-made synthetic example" in client.get("/").text


def test_an_example_runs_on_saved_pages_and_shows_the_expected_label():
    with TestClient(_app()) as client:
        response = client.post(f"/examples/{FIT_CASE}", follow_redirects=False)
        assert response.status_code == 303
        html = _wait_until_done(client, response.headers["location"])
        assert f"Synthetic example <code>{FIT_CASE}</code>" in html
        assert "Expected label" in html and "badge-fit" in html
        run = client.get(response.headers["location"] + "/export.json").json()
        assert run["example_id"] == FIT_CASE and run["example_expected"] == "fit"
        assert run["results"][0]["status"] == "ok"


def test_the_example_the_mock_gets_wrong_is_shown_as_wrong():
    """Keyword rules miss 'distributor'; the page shows the expected label next to it."""
    with TestClient(_app()) as client:
        response = client.post(f"/examples/{MOCK_MISS_CASE}", follow_redirects=False)
        html = _wait_until_done(client, response.headers["location"])
        run = client.get(response.headers["location"] + "/export.json").json()
        assert run["example_expected"] == "non_fit"
        assert run["results"][0]["fit"] != "non_fit"  # mock mode is not a model
        assert "badge-non_fit" in html


def test_unknown_example_is_404():
    with TestClient(_app()) as client:
        assert client.post("/examples/99-missing").status_code == 404


def test_examples_count_against_the_per_ip_limit():
    with TestClient(_app(runs_per_ip_per_hour=1)) as client:
        assert client.post(f"/examples/{FIT_CASE}", follow_redirects=False).status_code == 303
        second = client.post(f"/examples/{FIT_CASE}")
        assert second.status_code == 429 and "screenings per hour" in second.text
