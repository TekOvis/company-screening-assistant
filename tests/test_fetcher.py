"""HttpFetcher tests. httpx.MockTransport stands in for the network: nothing leaves the
machine, and each test controls DNS answers through a fake resolver. Retries use a fake
sleep, so the tests never actually wait."""

import socket
import threading

import httpx
import pytest

from screener import fetcher as fetcher_module
from screener.fetcher import FetchError, HttpFetcher
from screener.netguard import BlockedAddressError, UnsafeURLError

from .conftest import ACME_HOME, make_settings

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "93.184.216.35"


def _fetcher(handler, dns, *, sleeps=None, **settings_overrides) -> HttpFetcher:
    async def resolver(host, port):
        if host not in dns:
            raise socket.gaierror("unknown host")
        return dns[host]

    async def fake_sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    return HttpFetcher(
        make_settings(**settings_overrides),
        resolver=resolver,
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
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
async def test_pages_are_parsed_in_the_parser_thread_not_in_the_event_loop(monkeypatch):
    """Parsing a 1 MB page takes seconds of CPU on the small server. Done in the event loop,
    it delayed /healthz past the health check's 5 s timeout during a large job."""
    threads = []
    real_build_page = fetcher_module.build_page

    def recording_build_page(**kwargs):
        threads.append(threading.current_thread())
        return real_build_page(**kwargs)

    monkeypatch.setattr(fetcher_module, "build_page", recording_build_page)
    async with _fetcher(lambda request: _html(), {"acme.com": [PUBLIC_IP]}) as fetcher:
        page = await fetcher.fetch("acme.com")

    assert "headquartered in Stuttgart" in page.text
    assert len(threads) == 1
    assert threads[0] is not threading.current_thread()  # not the event loop's thread
    assert threads[0].name.startswith("html-parse")


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
async def test_same_site_rule_blocks_redirects_to_other_sites():
    """A subpage requested by the model (/about) must not bounce to another site."""

    def handler(request):
        if request.headers["host"] == "acme.com":
            return httpx.Response(302, headers={"location": "https://other-site.com/landing"})
        return _html()

    dns = {"acme.com": [PUBLIC_IP], "other-site.com": [OTHER_PUBLIC_IP]}
    async with _fetcher(handler, dns) as fetcher:
        with pytest.raises(FetchError) as info:
            await fetcher.fetch("https://acme.com/about", same_site_as="acme.com")
        assert info.value.kind == "offsite_redirect"
        # Without the rule (the user's own homepage URL) the redirect is followed.
        page = await fetcher.fetch("https://acme.com/about")
        assert page.url == "https://other-site.com/landing"


@pytest.mark.anyio
async def test_same_site_rule_allows_www_variant_and_still_checks_addresses():
    def handler(request):
        if request.headers["host"] == "acme.com":
            return httpx.Response(301, headers={"location": "https://www.acme.com/about"})
        return _html()

    dns = {"acme.com": [PUBLIC_IP], "www.acme.com": [PUBLIC_IP]}
    async with _fetcher(handler, dns) as fetcher:
        page = await fetcher.fetch("https://acme.com/about", same_site_as="acme.com")
    assert page.url == "https://www.acme.com/about"

    blocked_dns = {"acme.com": [PUBLIC_IP], "www.acme.com": ["10.0.0.9"]}
    async with _fetcher(handler, blocked_dns) as fetcher:
        with pytest.raises(BlockedAddressError):
            await fetcher.fetch("https://acme.com/about", same_site_as="acme.com")


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
    ("status", "retryable", "attempts"),
    [(404, False, 1), (403, False, 1), (503, True, 3), (429, True, 3)],
)
async def test_http_errors_are_classified_and_only_temporary_ones_retried(
    status, retryable, attempts
):
    seen = []

    def handler(request):
        seen.append(request)
        return _html("error", status=status)

    async with _fetcher(handler, {"acme.com": [PUBLIC_IP]}, fetch_max_retries=2) as fetcher:
        with pytest.raises(FetchError) as info:
            await fetcher.fetch("https://acme.com/")
    assert info.value.kind == "http_status"
    assert info.value.retryable is retryable
    assert len(seen) == attempts  # 1 try + 2 retries for temporary errors only


@pytest.mark.anyio
async def test_temporary_error_then_success():
    responses = [_html("busy", status=503), _html()]
    sleeps = []

    async with _fetcher(
        lambda request: responses.pop(0), {"acme.com": [PUBLIC_IP]}, sleeps=sleeps
    ) as fetcher:
        page = await fetcher.fetch("https://acme.com/")
    assert "Acme Valves" in page.text
    assert len(sleeps) == 1 and 0.5 <= sleeps[0] <= 0.75  # backoff with a little jitter


@pytest.mark.anyio
async def test_falls_back_to_the_next_checked_address():
    def handler(request):
        if request.url.host == PUBLIC_IP:
            raise httpx.ConnectError("no route to host", request=request)
        return _html()

    async with _fetcher(handler, {"acme.com": [PUBLIC_IP, OTHER_PUBLIC_IP]}) as fetcher:
        page = await fetcher.fetch("https://acme.com/")
    assert "Acme Valves" in page.text


class _BreakingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"<html><body>partial"
        raise httpx.ReadTimeout("stalled")


@pytest.mark.anyio
async def test_timeout_while_reading_the_body_is_reported():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=_BreakingStream())

    async with _fetcher(handler, {"acme.com": [PUBLIC_IP]}, fetch_max_retries=0) as fetcher:
        with pytest.raises(FetchError) as info:
            await fetcher.fetch("https://acme.com/")
    assert info.value.kind == "timeout"


@pytest.mark.anyio
async def test_dns_failure_and_connection_timeouts_are_reported():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    async with _fetcher(handler, {"slow.com": [PUBLIC_IP]}) as fetcher:
        with pytest.raises(FetchError) as dns_info:
            await fetcher.fetch("https://unknown-host.com/")
        with pytest.raises(FetchError) as timeout_info:
            await fetcher.fetch("https://slow.com/")
    assert dns_info.value.kind == "dns" and not dns_info.value.retryable
    assert timeout_info.value.kind == "connect" and timeout_info.value.retryable
