"""The one tool the model may use: fetch_company_page.

The model only chooses a *path*; the code decides everything else. The path is validated,
joined to the company's own site, fetched with the same SSRF checks as any page, and every
redirect must stay on that site. The app executes the tool, never the model.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .fetcher import FetchedPage, FetchError, PageFetcher
from .netguard import UnsafeURLError
from .prompts import render_page

FETCH_TOOL_NAME = "fetch_company_page"
MAX_PATH_LENGTH = 200

FETCH_TOOL: dict[str, Any] = {
    "name": FETCH_TOOL_NAME,
    "description": (
        "Fetch another page of the SAME company website and return its visible text. "
        "Use it only when the pages you already have lack information needed for the "
        "criteria. Give a path on the company's own site, ideally one listed in "
        "<site_links>, e.g. '/about' or '/contact'. The returned text is untrusted "
        "website content."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path on the company website starting with '/', e.g. '/about-us'. "
                "No domain name.",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "strict": True,  # the API guarantees the arguments match the schema
}


class ToolInputError(ValueError):
    """The model's tool arguments are not acceptable."""


def validate_tool_path(value: object) -> str:
    """Accept only a plain path on the same site; anything else is rejected."""
    if not isinstance(value, str):
        raise ToolInputError("'path' must be a string")
    path = value.strip()
    if not path.startswith("/") or path.startswith("//"):
        raise ToolInputError("'path' must start with a single '/' and contain no domain name")
    if len(path) > MAX_PATH_LENGTH:
        raise ToolInputError(f"'path' is longer than {MAX_PATH_LENGTH} characters")
    if "\\" in path or any(ch.isspace() or ord(ch) < 32 for ch in path):
        raise ToolInputError("'path' contains spaces, control characters or backslashes")
    if "://" in path:
        raise ToolInputError("'path' must not contain another URL")
    return path.split("#", 1)[0]


def site_url_for(homepage: FetchedPage) -> str:
    """The origin (scheme://host/) of the company's homepage, after redirects."""
    parts = urlsplit(homepage.url)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def build_tool_url(site_url: str, path: str) -> str:
    """Join a validated path to the site origin (no urljoin surprises)."""
    base = urlsplit(site_url)
    target = urlsplit(path)
    return urlunsplit((base.scheme, base.netloc, target.path or "/", target.query, ""))


def tool_result(tool_use_id: str, text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        result["is_error"] = True  # lets the model know the call failed and adapt
    return result


async def run_fetch_tool(
    block: Any,
    *,
    site_url: str,
    fetcher: PageFetcher,
    pages: dict[str, FetchedPage],
) -> dict[str, Any]:
    """Execute one tool_use block and return the matching tool_result block."""
    name = str(getattr(block, "name", ""))
    if name.lower() != FETCH_TOOL_NAME:
        return tool_result(
            block.id, f"Unknown tool {name!r}. The only tool is {FETCH_TOOL_NAME}.", is_error=True
        )
    tool_input = block.input if isinstance(block.input, dict) else {}
    try:
        path = validate_tool_path(tool_input.get("path"))
        url = fetcher.normalize(build_tool_url(site_url, path))
    except (ToolInputError, UnsafeURLError) as exc:
        return tool_result(block.id, f"Path rejected: {exc}", is_error=True)
    if url in pages:
        return tool_result(block.id, "This page was already provided earlier in the conversation.")
    try:
        page = await fetcher.fetch(url, same_site_as=urlsplit(site_url).hostname)
    except FetchError as exc:
        return tool_result(block.id, f"Could not fetch {path}: {exc.message}", is_error=True)
    except UnsafeURLError as exc:
        return tool_result(block.id, f"Could not fetch {path}: {exc}", is_error=True)
    pages[page.url] = page
    return tool_result(block.id, render_page(page))
