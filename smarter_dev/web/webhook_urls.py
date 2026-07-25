"""Validation of Discord webhook URLs, shared by every deleter.

Two call sites delete leaked webhooks — the admin-handler scripting function
(:mod:`smarter_dev.web.admin_actions`) and the core content filter
(:mod:`smarter_dev.bot.content_filter`) — and both must answer the same
question: *is this string a real Discord webhook we may issue a DELETE for?*

That answer lives here once. The two used to carry mirrored copies of the
regex, which drifted: the content filter grew normalization for the shapes
leaks actually arrive in (no scheme, uppercase host, ``?wait=true``) and the
admin one did not, so the same leaked URL was killed by one path and refused by
the other. Anything that widens or tightens webhook validation belongs in this
module, never in a caller.

The security property is the anchor, not the normalization: a string only ever
becomes a REST call when its canonical form is exactly
``https://<discord host>/api/webhooks/<id>/<token>``. Normalization changes
*how* a real Discord webhook may be spelled; it never changes *which* host is
accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The ONLY URL shape a webhook DELETE may be issued for, matched against the
# NORMALIZED url. Anchored end-to-end so a crafted string can never become an
# arbitrary-host request: the host must be a real Discord webhook host
# (canary/ptb subdomains and the legacy discordapp.com alias included), the path
# exactly /api/webhooks/<id>/<token>, and nothing after the token — which also
# rejects path traversal. The id is all-digits, the token a url-safe word run.
_WEBHOOK_URL_RE = re.compile(
    r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/"
    r"(?P<id>\d+)/(?P<token>[\w-]+)$"
)

# Leading ``<scheme>://`` of a url, when it has one at all.
_URL_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://")

# Substring that marks a link as *claiming* to be a webhook. Candidates are
# matched loosely so a foreign-host lookalike can be reported and rejected
# rather than silently ignored.
WEBHOOK_PATH_MARKER = "/api/webhooks/"


@dataclass(frozen=True)
class WebhookReference:
    """The id/token pair extracted from a validated Discord webhook URL."""

    webhook_id: str
    token: str


def normalize_webhook_url(url: str) -> str:
    """``url`` in the canonical form the webhook validator matches against.

    The scheme and the host are lowercased (both are case-insensitive per RFC
    3986, and leaks arrive as ``HTTPS://DISCORD.com/...``), a missing scheme
    becomes ``https`` (spammers paste bare ``discord.com/api/webhooks/...``),
    and the query and fragment are dropped (Discord's own documentation emits
    ``?wait=true``).

    Everything else — host spelling, path, and the case-sensitive token — is
    left exactly as written, so this only ever widens *how* a real Discord
    webhook may be spelled and never widens *which* host is accepted: an
    explicit ``http://`` is not upgraded, and a lookalike host stays a lookalike.
    """
    text = str(url).strip()
    scheme_match = _URL_SCHEME_RE.match(text)
    if scheme_match is None:
        scheme = "https"
        remainder = text
    else:
        scheme = scheme_match.group("scheme").lower()
        remainder = text[scheme_match.end() :]
    remainder = re.split(r"[?#]", remainder, maxsplit=1)[0]
    authority, slash, path = remainder.partition("/")
    return f"{scheme}://{authority.lower()}{slash}{path}"


def parse_discord_webhook_url(url: str) -> WebhookReference | None:
    """The id/token of a real Discord webhook URL, None for anything else.

    ``url`` is normalized first, so the common leak shapes (no scheme, an
    uppercase scheme or host, a trailing ``?wait=true``) all resolve to the same
    canonical url. Anything whose canonical form is not exactly
    ``https://<discord host>/api/webhooks/<id>/<token>`` — a foreign host, a
    lookalike domain, plain http, a traversal attempt, a missing token — returns
    None and must never be turned into a REST call.
    """
    match = _WEBHOOK_URL_RE.match(normalize_webhook_url(url))
    if match is None:
        return None
    return WebhookReference(webhook_id=match.group("id"), token=match.group("token"))


def is_discord_webhook_url(url: str) -> bool:
    """True when ``url`` is a deletable Discord webhook URL."""
    return parse_discord_webhook_url(url) is not None
