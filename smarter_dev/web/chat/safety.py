"""URL and filename safety boundaries for agent-controlled reads/uploads."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import PurePath
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    pass


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


async def validate_public_url(url: str) -> str:
    """Reject credentials, non-HTTP schemes, and private/special destinations."""
    parsed = urlsplit((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrlError("only http and https URLs are supported")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeUrlError("URL must have a public host and no credentials")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise UnsafeUrlError("private hosts are not supported")
    try:
        if not _is_public_ip(host):
            raise UnsafeUrlError("private addresses are not supported")
        return url
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(
                host, parsed.port or 443, type=socket.SOCK_STREAM
            ),
        )
    except socket.gaierror as exc:
        raise UnsafeUrlError("host could not be resolved") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_is_public_ip(address) for address in addresses):
        raise UnsafeUrlError("host resolves to a private or unsafe address")
    return url


def safe_filename(value: str) -> str:
    """Return a basename after rejecting paths/control characters."""
    name = (value or "").strip()
    if not name or "\x00" in name or any(ord(c) < 32 for c in name):
        raise ValueError("invalid filename")
    if PurePath(name).name != name or "/" in name or "\\" in name:
        raise ValueError("paths are not allowed in filenames")
    return name
