import pytest

from screener.fetcher import FetchError
from screener.fixtures import FixtureFetcher, FixturePage

from .conftest import ACME_HOME


@pytest.mark.anyio
async def test_fixture_redirects_follow_the_same_site_rule(settings):
    fetcher = FixtureFetcher(
        {
            "https://acme.example/": ACME_HOME,
            "https://acme.example/about": FixturePage(status_code=301, location="/company/"),
            "https://acme.example/company/": "<p>About us</p>",
            "https://acme.example/contact": FixturePage(
                status_code=302, location="https://elsewhere.example/"
            ),
            "https://elsewhere.example/": "<p>Another site</p>",
        },
        settings,
    )
    page = await fetcher.fetch("https://acme.example/about", same_site_as="acme.example")
    assert page.url == "https://acme.example/company/"
    assert "About us" in page.text

    with pytest.raises(FetchError) as info:
        await fetcher.fetch("https://acme.example/contact", same_site_as="acme.example")
    assert info.value.kind == "offsite_redirect"


@pytest.mark.anyio
async def test_missing_fixture_is_a_404(settings):
    fetcher = FixtureFetcher({"https://acme.example/": ACME_HOME}, settings)
    with pytest.raises(FetchError) as info:
        await fetcher.fetch("https://acme.example/missing")
    assert info.value.message == "HTTP 404"
