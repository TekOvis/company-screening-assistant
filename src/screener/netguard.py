"""URL and network-address checks for outgoing requests (SSRF protection).

Every URL here is untrusted: it comes from the user, from a web page (links) or from a
server (redirects). Before connecting we:

1. check the URL shape: http/https only, default ports only, no credentials, a real
   domain name (no IP literals, no "localhost"-style or reserved names);
2. resolve the host name ourselves and accept it only if *every* resolved address is a
   public internet address (no loopback, private, link-local, cloud metadata, CGNAT,
   multicast or reserved ranges, including IPv4 addresses hidden inside IPv6 forms).

The fetcher then connects to exactly the address checked here, so a DNS answer that
changes between "check" and "connect" (DNS rebinding) cannot redirect the request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"http": 80, "https": 443}

# Names that are never public websites (RFC 6761 special-use and common internal names).
BLOCKED_HOST_NAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}
BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".corp",
    ".home.arpa",
    ".example",
    ".invalid",
    ".test",
    ".onion",
)

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NUMERIC_LABEL_RE = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")
# Characters kept as-is when re-encoding the path and query (RFC 3986, minus quotes).
_SAFE_PATH = "/%:@!$&()*+,;=-._~"
_SAFE_QUERY = _SAFE_PATH + "?"

NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class UnsafeURLError(ValueError):
    """The URL is malformed or points to a place we refuse to connect to."""


@dataclass(frozen=True)
class SafeURL:
    url: str  # normalized URL without fragment
    scheme: str
    host: str  # lower-case ASCII (IDNA) host name
    port: int  # effective port (80 or 443)


def normalize_url(raw: str) -> SafeURL:
    """Validate and normalize a URL. Adds https:// when the scheme is missing."""
    text = raw.strip()
    if not text:
        raise UnsafeURLError("empty URL")
    if len(text) > MAX_URL_LENGTH:
        raise UnsafeURLError("URL is too long")
    if any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in text):
        raise UnsafeURLError("URL contains spaces, control characters or backslashes")
    if not _SCHEME_RE.match(text):
        text = "https://" + text.lstrip("/")

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"only http and https URLs are allowed, got {scheme!r}")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("invalid port") from exc
    if port is not None and port != ALLOWED_SCHEMES[scheme]:
        raise UnsafeURLError("only the default ports 80 (http) and 443 (https) are allowed")

    host = check_host_name(parts.hostname or "")
    path = quote(parts.path or "/", safe=_SAFE_PATH)
    query = quote(parts.query, safe=_SAFE_QUERY)
    url = urlunsplit((scheme, host, path, query, ""))
    return SafeURL(url=url, scheme=scheme, host=host, port=ALLOWED_SCHEMES[scheme])


def check_host_name(host: str) -> str:
    """Return the ASCII form of a host name or raise UnsafeURLError."""
    host = host.strip().rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL has no host name")
    if looks_like_ip_address(host):
        raise UnsafeURLError("IP addresses are not accepted; use the company's domain name")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeURLError("invalid host name") from exc
    if ascii_host in BLOCKED_HOST_NAMES or ascii_host.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafeURLError("local, internal or reserved host names are not allowed")
    if len(ascii_host) > 253 or "." not in ascii_host:
        raise UnsafeURLError("host name must be a full domain name such as example.com")
    if not all(_LABEL_RE.match(label) for label in ascii_host.split(".")):
        raise UnsafeURLError("invalid host name")
    return ascii_host


def looks_like_ip_address(host: str) -> bool:
    """True for IP literals, including tricky forms like 2130706433, 0x7f.1 or 127.1."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        pass
    labels = [label for label in host.split(".") if label]
    return bool(labels) and all(_NUMERIC_LABEL_RE.match(label) for label in labels)


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses.

    Python's ``is_global`` alone is not enough: it is True for multicast (224.0.0.1) and
    for NAT64 forms such as 64:ff9b::7f00:1, which embeds 127.0.0.1.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:  # ::ffff:a.b.c.d
            return is_public_address(ip.ipv4_mapped)
        if ip in NAT64_PREFIX:  # 64:ff9b::a.b.c.d
            return is_public_address(ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF))
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    """Resolve a host name with the operating system's resolver."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class BlockedAddressError(UnsafeURLError):
    """The host name resolves to a non-public address."""


class ResolutionError(Exception):
    """The host name could not be resolved (a timeout is worth retrying, NXDOMAIN is not)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


async def resolve_public_addresses(
    host: str, port: int, resolver: Resolver, timeout_s: float
) -> list[str]:
    """Resolve ``host`` and return its addresses, or raise if any of them is not public.

    Rejecting the whole name when even one address is private avoids "mixed" DNS answers
    being used to sneak a private address past the check.
    """
    try:
        async with asyncio.timeout(timeout_s):
            addresses = await resolver(host, port)
    except TimeoutError as exc:
        raise ResolutionError(f"DNS lookup for {host} timed out", retryable=True) from exc
    except OSError as exc:  # socket.gaierror is a subclass of OSError
        raise ResolutionError(f"could not resolve {host}") from exc
    if not addresses:
        raise ResolutionError(f"could not resolve {host}")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop IPv6 zone id
        except ValueError as exc:
            raise BlockedAddressError(f"{host} resolved to an invalid address") from exc
        if not is_public_address(ip):
            raise BlockedAddressError(f"{host} resolves to a non-public address; refusing")
    return addresses
