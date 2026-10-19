"""Outbound HTTP with SSRF protection.

Only https URLs on an explicit host allowlist are fetched. Every redirect hop is re-checked, the
resolved IP addresses must be public, responses are size-limited, and no cookies or credentials are sent.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

from ..config import get_settings

MAX_BYTES = 3 * 1024 * 1024
UA = "PitchForceFeedback/0.1 (+calendar retrieval; operator-initiated)"


class FetchBlocked(Exception):
    pass


def _host_allowed(host: str, allowed: set[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in allowed)


def check_url(url: str, allowed: set[str] | None = None) -> None:
    allowed = allowed if allowed is not None else get_settings().calendar_hosts
    p = urlparse(url)
    if p.scheme != "https":
        raise FetchBlocked("Only https URLs are fetched.")
    if not p.hostname or not _host_allowed(p.hostname, allowed):
        raise FetchBlocked(f"Host {p.hostname!r} is not on the allowed list.")
    if p.username or p.password:
        raise FetchBlocked("URLs with credentials are not fetched.")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchBlocked(f"Could not resolve {p.hostname}.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise FetchBlocked(f"{p.hostname} resolves to a non-public address.")


def fetch(url: str, allowed: set[str] | None = None, max_redirects: int = 3, timeout: float = 15.0) -> tuple[str, str, str]:
    """Return (final_url, content_type, text)."""
    if not get_settings().outbound_fetch_enabled:
        raise FetchBlocked("Outbound fetching is disabled (OUTBOUND_FETCH_ENABLED=false).")
    current = url
    with httpx.Client(follow_redirects=False, timeout=timeout, headers={"User-Agent": UA}) as client:
        for _ in range(max_redirects + 1):
            check_url(current, allowed)
            with client.stream("GET", current) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("location")
                    if not loc:
                        raise FetchBlocked("Redirect without a location.")
                    current = urljoin(current, loc)
                    continue
                r.raise_for_status()
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise FetchBlocked("Response too large.")
                    chunks.append(chunk)
                data = b"".join(chunks)
                enc = r.encoding or "utf-8"
                return current, r.headers.get("content-type", ""), data.decode(enc, errors="replace")
    raise FetchBlocked("Too many redirects.")
