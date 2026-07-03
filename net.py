"""Outbound HTTP: a single capped, gzip-aware GET used by every fetcher.

Every server-side fetch funnels through ``http_get``, which is also where SSRF
protection lives: the target host is resolved and rejected if it points at a
private / loopback / link-local / reserved address. Because ``/api/discover`` and
``/api/feeds`` fetch user-supplied URLs, this stops the server being used to reach
internal services (cloud metadata, admin panels, localhost) it shouldn't.
"""
import gzip
import socket
import ipaddress
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import config


class BlockedURLError(ValueError):
    """Raised when a URL is refused by the SSRF guard."""


def _ip_is_blocked(ip):
    addr = ipaddress.ip_address(ip)
    # IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) must be judged on the mapped v4 addr.
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def guard_url(url):
    """Validate a URL before fetching it. Raises BlockedURLError if unsafe.

    Only http/https is allowed, and (when BLOCK_PRIVATE_IPS is on) the host must
    resolve exclusively to public addresses. This is resolve-and-check: a
    determined DNS-rebinding attacker could still flip the record between this
    check and the connection (documented in SECURITY.md), but it blocks the
    common SSRF cases cheaply and without breaking legitimate public feeds.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedURLError(f"URL scheme '{parsed.scheme}' is not allowed.")
    host = parsed.hostname
    if not host:
        raise BlockedURLError("URL has no host.")
    if not config.BLOCK_PRIVATE_IPS:
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURLError(f"Could not resolve host '{host}': {e}")
    for info in infos:
        ip = info[4][0]
        if _ip_is_blocked(ip):
            raise BlockedURLError(f"Refusing to fetch a private/internal address ({host} → {ip}).")


def http_get(url, with_ctype=False):
    guard_url(url)
    req = Request(url, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip",
    })
    with urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
        raw = r.read(config.MAX_BYTES)
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        ctype = r.headers.get("Content-Type", "")
    return (raw, ctype) if with_ctype else raw


def favicon_for(site_url, feed_url):
    """A small icon URL for a feed, derived from its site (or feed) host."""
    host = urlparse(site_url or feed_url or "").netloc.lower()
    return f"https://icons.duckduckgo.com/ip3/{host}.ico" if host else ""
