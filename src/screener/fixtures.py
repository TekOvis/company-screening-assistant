"""An in-memory ``PageFetcher`` for tests and offline evaluation.

It never touches the network: pages come from a dictionary of saved texts. This is what
makes the evaluation reproducible — a "live" evaluation run uses the real LLM but these
saved fixtures instead of real websites (fixture domains such as *.example are never
requested from the internet).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .config import Settings
from .fetcher import FetchedPage, FetchError, build_page
from .netguard import UnsafeURLError


@dataclass(frozen=True)
class FixturePage:
    body: str
    content_type: str = "text/html; charset=utf-8"
    status_code: int = 200


class FixtureFetcher:
    def __init__(self, pages: dict[str, FixturePage | str], settings: Settings) -> None:
        self._settings = settings
        self._pages: dict[str, FixturePage] = {}
        for url, page in pages.items():
            fixture = page if isinstance(page, FixturePage) else FixturePage(body=page)
            self._pages[self.normalize(url)] = fixture
        self.requested: list[str] = []  # for tests: which URLs were fetched

    def normalize(self, url: str) -> str:
        text = url.strip()
        if "://" not in text:
            text = "https://" + text.lstrip("/")
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise UnsafeURLError("only http and https URLs with a host name are allowed")
        return urlunsplit((parts.scheme, parts.hostname, parts.path or "/", parts.query, ""))

    async def fetch(self, url: str) -> FetchedPage:
        key = self.normalize(url)
        self.requested.append(key)
        page = self._pages.get(key) or self._pages.get(_toggle_trailing_slash(key))
        if page is None:
            raise FetchError("http_status", "HTTP 404")
        if page.status_code >= 400:
            raise FetchError("http_status", f"HTTP {page.status_code}")
        return build_page(
            requested_url=key,
            final_url=key,
            status_code=page.status_code,
            content_type=page.content_type,
            body=page.body.encode("utf-8"),
            settings=self._settings,
        )

    async def aclose(self) -> None:
        return None


def _toggle_trailing_slash(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    elif not path.endswith("/"):
        path = path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
