import pytest

from screener.fetcher import FetchedPage
from screener.tools import ToolInputError, build_tool_url, site_url_for, validate_tool_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/about", "/about"),
        (" /about-us ", "/about-us"),
        ("/en/company?tab=locations", "/en/company?tab=locations"),
        ("/team#management", "/team"),
    ],
)
def test_valid_paths(raw, expected):
    assert validate_tool_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "about",
        "//evil.com/",
        "https://evil.com/",
        "/a b",
        "/a\\b",
        "/\x00",
        "/" + "x" * 201,
        "/r?u=http://evil.com",
        42,
        None,
        ["/about"],
    ],
)
def test_invalid_paths(raw):
    with pytest.raises(ToolInputError):
        validate_tool_path(raw)


def test_paths_are_joined_to_the_company_origin_only():
    page = FetchedPage(
        requested_url="http://acme.com/",
        url="https://www.acme.com/en/home",
        status_code=200,
        mime_type="text/html",
        title=None,
        text="",
    )
    site = site_url_for(page)
    assert site == "https://www.acme.com/"
    assert build_tool_url(site, "/about?x=1") == "https://www.acme.com/about?x=1"
    assert build_tool_url(site, "/%2F%2Fevil.com") == "https://www.acme.com/%2F%2Fevil.com"
