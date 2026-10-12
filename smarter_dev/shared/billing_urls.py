"""Strict origin parsing shared by billing settings and API return URLs."""

from __future__ import annotations

import re
from ipaddress import IPv6Address
from urllib.parse import urlsplit


def billing_url_origin(
    value: str, *, origin_only: bool = False
) -> tuple[str, str, int]:
    """Return a normalized HTTP(S) origin without URL parser repair.

    Configuration entries must be bare origins (an optional trailing slash is
    harmless). Request URLs may additionally contain paths and query strings.
    No wildcard/subdomain matching, userinfo, fragments or ambiguous authority.
    """
    if (
        not value
        or "\\" in value
        or "#" in value
        or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Invalid billing return URL")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (
        parsed.scheme not in {"https", "http"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or (origin_only and (parsed.path not in {"", "/"} or "?" in value))
    ):
        raise ValueError("Invalid billing return URL")
    if ":" in host:
        if not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", parsed.netloc):
            raise ValueError("Invalid billing return URL")
        host = str(IPv6Address(host))
    elif len(host) > 253 or any(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
        for label in host.split(".")
    ):
        raise ValueError("Invalid billing return URL")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid billing return URL")
    return (
        parsed.scheme,
        host,
        port if port is not None else (443 if parsed.scheme == "https" else 80),
    )
