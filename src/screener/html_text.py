"""Turn an HTML (or plain-text) page into compact visible text plus same-site links.

The extracted text is what the LLM sees *and* what evidence quotes are checked against,
so both sides always use exactly the same string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

# Elements that never contain visible page text.
_NON_CONTENT_TAGS = [
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "head",
]
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
# Our prompt uses these tags as delimiters; neutralize them inside untrusted page text.
_DELIMITER_RE = re.compile(r"<(/?)(page|site_links|screening_criteria)", re.IGNORECASE)
_SKIP_LINK_PREFIXES = ("mailto:", "tel:", "javascript:", "data:", "#")
_BINARY_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".mp4",
    ".mp3",
    ".css",
    ".js",
    ".xml",
    ".json",
)
_SAFE_URL_CHARS = "/%:@!$&()*+,;=-._~"
TRUNCATION_MARK = "[... page text truncated ...]"


@dataclass(frozen=True)
class PageLink:
    path: str  # path (+ query) on the same site, e.g. "/about"
    label: str  # visible link text, shortened


@dataclass
class ExtractedPage:
    title: str | None
    text: str
    links: list[PageLink] = field(default_factory=list)
    truncated: bool = False


def normalize_space(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def neutralize_delimiters(text: str) -> str:
    """Make "<page", "</page" etc. inside page text harmless for our prompt format."""
    return _DELIMITER_RE.sub(lambda m: "&lt;" + m.group(1) + m.group(2), text)


def same_site(host_a: str | None, host_b: str | None) -> bool:
    """Treat example.com and www.example.com as the same site."""
    if not host_a or not host_b:
        return False
    return host_a.lower().removeprefix("www.") == host_b.lower().removeprefix("www.")


def extract_page(
    body: bytes,
    *,
    base_url: str,
    mime_type: str,
    charset: str | None,
    max_chars: int,
    max_links: int,
) -> ExtractedPage:
    if mime_type == "text/plain":
        raw_text = body.decode(charset or "utf-8", errors="replace")
        title, meta_description, links = None, None, []
    else:
        soup = BeautifulSoup(body, "html.parser", from_encoding=charset)
        title = normalize_space(soup.title.get_text(" ")) if soup.title else None
        meta_description = _meta_description(soup)
        links = _same_site_links(soup, base_url, max_links)
        for tag in soup.find_all(_NON_CONTENT_TAGS):
            tag.decompose()
        # Remove obviously hidden elements: a common place for text aimed at AI systems.
        # (This cannot detect text hidden by CSS classes; other defenses cover that.)
        for tag in soup.find_all(_is_hidden):
            tag.decompose()
        raw_text = soup.get_text("\n")

    lines: list[str] = []
    if title:
        lines.append(f"Title: {title}")
    if meta_description:
        lines.append(f"Meta description: {meta_description}")
    for line in raw_text.splitlines():
        line = normalize_space(line)
        if line and (not lines or lines[-1] != line):
            lines.append(line)

    text = neutralize_delimiters("\n".join(lines))
    truncated = len(text) > max_chars
    if truncated:
        cut = text.rfind("\n", 0, max_chars)
        text = text[: cut if cut > 0 else max_chars] + "\n" + TRUNCATION_MARK
    return ExtractedPage(title=title, text=text, links=links, truncated=truncated)


def _meta_description(soup: BeautifulSoup) -> str | None:
    tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.IGNORECASE)})
    content = tag.get("content") if tag else None
    if isinstance(content, str) and content.strip():
        return normalize_space(content)[:500]
    return None


def _is_hidden(tag) -> bool:
    if tag.attrs is None:  # already removed together with a parent
        return False
    if tag.has_attr("hidden"):
        return True
    if str(tag.get("aria-hidden", "")).lower() == "true":
        return True
    style = tag.get("style")
    return isinstance(style, str) and bool(_HIDDEN_STYLE_RE.search(style))


def _same_site_links(soup: BeautifulSoup, base_url: str, max_links: int) -> list[PageLink]:
    base_host = urlsplit(base_url).hostname
    seen: set[str] = set()
    links: list[PageLink] = []
    for anchor in soup.find_all("a", href=True):
        if len(links) >= max_links:
            break
        href = str(anchor["href"]).strip()
        if not href or href.lower().startswith(_SKIP_LINK_PREFIXES):
            continue
        parts = urlsplit(urljoin(base_url, href))
        if parts.scheme not in ("http", "https") or not same_site(parts.hostname, base_host):
            continue
        if parts.path.lower().endswith(_BINARY_EXTENSIONS):
            continue
        # Percent-encode so a crafted href cannot inject "<", quotes or spaces into prompts.
        path = quote(parts.path or "/", safe=_SAFE_URL_CHARS)
        if parts.query:
            path += "?" + quote(parts.query, safe=_SAFE_URL_CHARS + "?")
        if path in seen or len(path) > 200:
            continue
        seen.add(path)
        label = neutralize_delimiters(normalize_space(anchor.get_text(" ")))[:80]
        links.append(PageLink(path=path, label=label))
    return links
