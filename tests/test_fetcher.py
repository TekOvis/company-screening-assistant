"""HttpFetcher tests. httpx.MockTransport stands in for the network: nothing leaves the
machine, and each test controls DNS answers through a fake resolver."""

import socket

import httpx
import pytest

from screener.fetcher import FetchError, HttpFetcher
from screener.netguard import BlockedAddressError, UnsafeURLError

from .conftest import ACME_HOME, make_settings

PUBLIC_IP = "93.184.216.34"


def _fetcher(handler, dns, **settings_overrides) -> HttpFetcher:
    async def resolver(host, port):
        if host not in dns:
            raise socket.gaierror("unknown host")
        return dns[host]

    return HttpFetcher(
        make_settings(**settings_overrides),
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )


def _html(body: str = ACME_HOME, status: int = 200, **headers) -> httpx.Response:
    return httpx.Response(
        status,
        content=body.encode(),
        headers={"content-type": "text/html; charset=utf-8", **headers},
    )


@pytest.mark.anyio
async def test_connects_to_checked_ip_with_original_host_and_sni():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _html()

    async with _fetcher(handler, {"acme.com": [PUBLIC_IP]}) as fetcher:
        page = await fetcher.fetch("acme.com")

    request = seen[0]
    assert request.url.host == PUBLIC_IP  # pinned to the address we checked
    assert request.headers["host"] == "acme.com"
    assert request.extensions["sni_hostname"] == "acme.com"  # TLS verifies the real name
    assert request.headers["accept-encoding"] == "identity"
    assert page.url == "https://acme.com/"
    assert "headquartered in Stuttgart" in page.text
    assert [link.path for link in page.links] == ["/about", "/contact"]


@pytest.mark.anyio
@pytest.mark.parametrize("answers", [["10.0.0.5"], [PUBLIC_IP, "127.0.0.1"], ["169.254.169.254"]])
async def test_blocks_hosts_resolving_to_non_public_addresses(answers):
    def handler(request):  # must never be reached
        raise AssertionError("request was sent")

    async with _fetcher(handler, {"evil.com": answers}) as fetcher:
        with pytest.raises(BlockedAddressError):
            await fetcher.fetch("https://evil.com/")


@pytest.mark.anyio
async def test_redirect_to_internal_address_is_blocked():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://internal-api.com/admin"})

    dns = {"acme.com": [PUBLIC_IP], "internal-api.com": ["10.1.2.3"]}
    async with _fetcher(handler, dns) as fetcher:
        with pytest.raises(BlockedAddressError):
            await fetcher.fetch("https://acme.com/")


@pytest.mark.anyio
async def test_redirect_to_localhost_name_is_rejected():
    def handler(request):
        return httpx.Response(301, headers={"location": "http://localhost:8000/"})

    async with _fetcher(handler, {"acme.com": [PUBLIC_IP]}) as fetcher:
        with pytest.raises(UnsafeURLError):
            await fetcher.fetch("https://acme.com/")


@pytest.mark.anyio
async def test_follows_safe_redirects_and_reports_final_url():
    def handler(request):
        if request.headers["host"] == "acme.com":
            return httpx.Response(301, headers={"location": "https://www.acme.com/en/"})
        return _html()

    dns = {"acme.com": [PUBLIC_IP], "www.acme.com": [PUBLIC_IP]}
    async with _fetcher(handler, dns) as fetcher:
        page = await fetcher.fetch("http://acme.com/")
    assert page.requested_url == "http://acme.com/"
    assert page.url == "https://www.acme.com/en/"


@pytest.mark.anyio
async def test_too_many_redirects():
    def handler(request):
        return httpx.Response(302, headers={"location": "/loop"})

    async with _fetcher(handler, {"acme.com": [PUBLIC_IP]}, fetch_max_redirects=2) as fetcher:
        with pytest.raises(FetchError) as info:
            await fetcher.fetch("https://acme.com/")
    assert info.value.kind == "too_many_redirects"


@pytest.mark.anyio
@pytest.mark.parametrize("declare_length", [True, False])
async def test_rejects_pages_over_the_size_limit(declare_length):
    body = "<p>" + "x" * 20_000 + "</p>"

    def handler(request):
        response = _html(body)
        if not declare_length:
            response.headers.pop("content-length", None)
        return response

    async with _fetcher(handler, {"acme.com": [PUBLIC_IP]}, fetch_max_bytes=10_000) as fetcher:
        with pytest.raises(FetchError) as info:
            await fetcher.fetch("https://acme.com/")
    assert info.value.kind == "too_large"


@pytest.mark.anyio
async def test_rejects_non_html_content():
    def handler(request):
        return httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})

    async with _fetcher(handler, {"acme.com": [PUBLIC_IP]}) as fetcher:
        with pytest.raises(FetchError) as info:
            await fetcher.fetch("https://acme.com/")
    assert info.value.kind == "unsupported_content"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "retryable"), [(404, False), (403, False), (503, True), (429, True)]
)
async def test_http_errors_are_classified(status, retryable):
    def handler(request):
        return _html("error", status=status)

    async with _fetcher(handler, {"acme.com": [PUBLIC_IP]}) as fetcher:
        with pytest.raises(FetchError) as info:
            await fetcher.fetch("https://acme.com/")
    assert info.value.kind == "http_status"
    assert info.value.retryable is retryable


@pytest.mark.anyio
async def test_dns_failure_and_timeouts_are_reported():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    async with _fetcher(handler, {"slow.com": [PUBLIC_IP]}) as fetcher:
        with pytest.raises(FetchError) as dns_info:
            await fetcher.fetch("https://unknown-host.com/")
        with pytest.raises(FetchError) as timeout_info:
            await fetcher.fetch("https://slow.com/")
    assert dns_info.value.kind == "dns"
    assert timeout_info.value.kind == "timeout" and timeout_info.value.retryable
