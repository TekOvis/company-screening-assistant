import ipaddress
import socket

import pytest

from screener.netguard import (
    BlockedAddressError,
    ResolutionError,
    UnsafeURLError,
    is_public_address,
    looks_like_ip_address,
    normalize_url,
    resolve_public_addresses,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "https://example.com/"),
        ("https://Example.COM/About?x=1#team", "https://example.com/About?x=1"),
        ("http://example.com:80/a%20b", "http://example.com/a%20b"),
        ("https://münchen.de/", "https://xn--mnchen-3ya.de/"),
        ("//example.com/path", "https://example.com/path"),
        ('https://example.com/"><script>', "https://example.com/%22%3E%3Cscript%3E"),
    ],
)
def test_normalize_url_accepts_and_normalizes(raw, expected):
    assert normalize_url(raw).url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pass@example.com/",
        "https://example.com:8080/",
        "http://localhost/",
        "http://api.localhost/",
        "http://printer.local/",
        "http://metadata.google.internal/",
        "https://acme.example/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://2130706433/",  # 127.0.0.1 written as one decimal number
        "http://0x7f.1/",  # 127.0.0.1 in hex shorthand
        "http://intranet/",  # not a full domain name
        "https://exa mple.com/",
        "https://example.com\\@evil.com/",
    ],
)
def test_normalize_url_rejects_unsafe_urls(raw):
    with pytest.raises(UnsafeURLError):
        normalize_url(raw)


def test_reserved_names_get_a_clear_message():
    with pytest.raises(UnsafeURLError, match="reserved host names"):
        normalize_url("http://localhost/")


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("2606:4700:4700::1111", True),
        ("::ffff:8.8.8.8", True),
        ("64:ff9b::808:808", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # cloud metadata endpoint
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),
        ("224.0.0.1", False),  # multicast: Python's is_global is True for this one!
        ("255.255.255.255", False),
        ("192.0.2.1", False),
        ("198.18.0.1", False),
        ("240.0.0.1", False),
        ("::1", False),
        ("::", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("fd12:3456::1", False),
        ("ff02::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("64:ff9b::7f00:1", False),  # NAT64 form of 127.0.0.1 (is_global is True!)
        ("2002:7f00:1::", False),  # 6to4
        ("2001:db8::1", False),  # documentation range
    ],
)
def test_is_public_address(address, public):
    assert is_public_address(ipaddress.ip_address(address)) is public


@pytest.mark.parametrize(
    ("host", "is_ip"),
    [
        ("127.0.0.1", True),
        ("2130706433", True),
        ("0x7f.1", True),
        ("::1", True),
        ("cafe.de", False),
        ("example.com", False),
        ("1password.com", False),
    ],
)
def test_looks_like_ip_address(host, is_ip):
    assert looks_like_ip_address(host) is is_ip


def _resolver(mapping):
    async def resolve(host, port):
        if host not in mapping:
            raise socket.gaierror("unknown host")
        return mapping[host]

    return resolve


@pytest.mark.anyio
async def test_resolve_accepts_only_public_answers():
    resolver = _resolver(
        {
            "good.com": ["93.184.216.34", "2606:2800:220:1::1"],
            "rebind.com": ["93.184.216.34", "10.0.0.7"],  # one private answer is enough to block
            "inside.com": ["127.0.0.1"],
        }
    )
    assert await resolve_public_addresses("good.com", 443, resolver, 1.0) == [
        "93.184.216.34",
        "2606:2800:220:1::1",
    ]
    with pytest.raises(BlockedAddressError):
        await resolve_public_addresses("rebind.com", 443, resolver, 1.0)
    with pytest.raises(BlockedAddressError):
        await resolve_public_addresses("inside.com", 443, resolver, 1.0)
    with pytest.raises(ResolutionError):
        await resolve_public_addresses("missing.com", 443, resolver, 1.0)
