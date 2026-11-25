"""Fetching company web pages.

``PageFetcher`` is the small interface the rest of the app depends on. Implementations:

* ``HttpFetcher``    — plain HTTP(S) requests with SSRF protection and resource limits
                       (the default and the only one used in production);
* ``FixtureFetcher`` — serves saved pages from memory (tests and offline evaluation),
                       see ``screener.fixtures``.

A browser-based fetcher (for JavaScript-heavy sites) can be added later as another
implementation of the same interface, without making a browser a required dependency.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin

import httpx

from .config import Settings
from .html_text import PageLink, extract_page
from .netguard import (
    BlockedAddressError,
    ResolutionError,
    Resolver,
    UnsafeURLError,
    normalize_url,
    resolve_public_addresses,
    system_resolver,
)

USER_AGENT = "CompanyScreeningAssistant/0.1 (educational prototype)"
ALLOWED_MIME_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class FetchError(Exception):
    """A page could not be fetched. ``kind`` is a short machine-readable category."""

    def __init__(self, kind: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable


@dataclass
class FetchedPage:
    requested_url: str
    url: str  # final URL after redirects
    status_code: int
    mime_type: str
    title: str | None
    text: str
    links: list[PageLink] = field(default_factory=list)
    truncated: bool = False


class PageFetcher(Protocol):
    def normalize(self, url: str) -> str:
        """Validate and normalize a URL; raise UnsafeURLError if it is not acceptable."""
        ...

    async def fetch(self, url: str) -> FetchedPage:
        """Fetch one page; raise UnsafeURLError or FetchError on failure."""
        ...

    async def aclose(self) -> None:
        """Release resources (connections)."""
        ...


def build_page(
    *,
    requested_url: str,
    final_url: str,
    status_code: int,
    content_type: str,
    body: bytes,
    settings: Settings,
) -> FetchedPage:
    """Shared by all fetchers so text extraction is identical everywhere."""
    mime_type, charset = parse_content_type(content_type)
    if mime_type not in ALLOWED_MIME_TYPES:
        raise FetchError(
            "unsupported_content", f"unsupported content type: {mime_type or 'unknown'}"
        )
    extracted = extract_page(
        body,
        base_url=final_url,
        mime_type=mime_type,
        charset=charset,
        max_chars=settings.page_text_max_chars,
        max_links=settings.max_links_per_page,
    )
    return FetchedPage(
        requested_url=requested_url,
        url=final_url,
        status_code=status_code,
        mime_type=mime_type,
        title=extracted.title,
        text=extracted.text,
        links=extracted.links,
        truncated=extracted.truncated,
    )


def parse_content_type(value: str) -> tuple[str, str | None]:
    parts = [part.strip() for part in value.split(";")]
    mime_type = parts[0].lower() if parts else ""
    charset = None
    for part in parts[1:]:
        key, _, val = part.partition("=")
        if key.strip().lower() == "charset" and val.strip():
            charset = val.strip().strip('"').lower()
    return mime_type, charset


class HttpFetcher:
    """HTTP(S) fetcher with SSRF protection, timeouts and size limits.

    Key decisions:
    * We resolve DNS ourselves, check every address (see netguard) and then connect to
      the *checked IP*. TLS still verifies the certificate for the real host name, which
      we pass as SNI (``sni_hostname``). This closes the DNS-rebinding gap.
    * Redirects are followed manually so each hop goes through the same checks.
    * Proxy environment variables are ignored (``trust_env=False``): a proxy would do its
      own DNS lookup and bypass the address check.
    * Connections are not reused between requests (no keep-alive), so a TLS connection
      verified for one host name is never reused for another host on the same IP.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        timeout = httpx.Timeout(
            connect=settings.fetch_connect_timeout_s,
            read=settings.fetch_read_timeout_s,
            write=settings.fetch_read_timeout_s,
            pool=settings.fetch_connect_timeout_s,
        )
        self._client = httpx.AsyncClient(
            transport=transport
            or httpx.AsyncHTTPTransport(
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=0)
            ),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpFetcher:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def normalize(self, url: str) -> str:
        return normalize_url(url).url

    async def fetch(self, url: str) -> FetchedPage:
        try:
            async with asyncio.timeout(self._settings.fetch_total_timeout_s):
                return await self._fetch(url)
        except TimeoutError as exc:
            raise FetchError("timeout", "the page took too long to load", retryable=True) from exc

    async def _fetch(self, url: str) -> FetchedPage:
        target = normalize_url(url)
        requested_url = target.url
        for _hop in range(self._settings.fetch_max_redirects + 1):
            try:
                addresses = await resolve_public_addresses(
                    target.host, target.port, self._resolver, self._settings.fetch_connect_timeout_s
                )
            except ResolutionError as exc:
                raise FetchError("dns", str(exc)) from exc
            # BlockedAddressError (an UnsafeURLError) propagates unchanged.

            response = await self._send(target.url, target.host, addresses[0])
            try:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("http_status", "redirect without a Location header")
                    # Resolve relative to the *host-name* URL, then re-validate everything.
                    target = normalize_url(urljoin(target.url, location))
                    continue
                if response.status_code >= 400:
                    raise FetchError(
                        "http_status",
                        f"HTTP {response.status_code}",
                        retryable=response.status_code in RETRYABLE_STATUSES,
                    )
                content_type = response.headers.get("content-type", "")
                mime_type, _ = parse_content_type(content_type)
                if mime_type not in ALLOWED_MIME_TYPES:
                    raise FetchError(
                        "unsupported_content", f"unsupported content type: {mime_type or 'unknown'}"
                    )
                body = await self._read_limited(response)
            finally:
                await response.aclose()
            return build_page(
                requested_url=requested_url,
                final_url=target.url,
                status_code=response.status_code,
                content_type=content_type,
                body=body,
                settings=self._settings,
            )
        raise FetchError("too_many_redirects", "too many redirects")

    async def _send(self, url: str, host: str, address: str) -> httpx.Response:
        pinned_url = httpx.URL(url).copy_with(host=address)  # connect to the checked IP
        request = self._client.build_request(
            "GET",
            pinned_url,
            headers={
                "Host": host,
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                # No compression: keeps the size limit simple and avoids zip bombs.
                "Accept-Encoding": "identity",
            },
            extensions={"sni_hostname": host},  # TLS checks the certificate for `host`
        )
        try:
            return await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise FetchError("timeout", "connection timed out", retryable=True) from exc
        except httpx.ConnectError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "SSL" in str(exc):
                raise FetchError("tls", "TLS/certificate error") from exc
            raise FetchError("connect", "could not connect", retryable=True) from exc
        except httpx.TransportError as exc:
            raise FetchError("network", "network error", retryable=True) from exc

    async def _read_limited(self, response: httpx.Response) -> bytes:
        limit = self._settings.fetch_max_bytes
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise FetchError("too_large", f"page is larger than {limit} bytes")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise FetchError("too_large", f"page is larger than {limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)


__all__ = [
    "BlockedAddressError",
    "FetchError",
    "FetchedPage",
    "HttpFetcher",
    "PageFetcher",
    "UnsafeURLError",
    "build_page",
]
