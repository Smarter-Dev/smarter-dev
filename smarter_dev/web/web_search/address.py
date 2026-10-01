"""Open a web address typed into a search link instead of searching for it.

A browser sends whatever the address bar can't place to its search engine,
and browsers disagree on newer top-level domains like ``getbuild.ing``. A
request only counts as an address when it is one well-formed host (with an
optional port and path) whose last label is in IANA's root zone
(``tlds.txt``, from https://data.iana.org/TLD/tlds-alpha-by-domain.txt).
Anything else is a search, with no call to Jev.

``setup.py`` and ``model.fit`` are well-formed too, since .py and .fit are real
top-level domains, so Jev decides whether the user wants to open the address
or look it up, and two more questions veto file names and code. A scheme (``https://…``), ``localhost`` or an IP address is
unambiguous and opens without asking.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

ADDRESS_MODEL = os.getenv("WEB_SEARCH_ADDRESS_MODEL", "jev-1.13.0")
# Opens when Jev leans to opening and neither veto fires. On the eval set
# (scripts/address_eval) sites score 0.58 and up on OPEN and every search 0.37
# or less; the vetoes are a margin, since no site scores above 0.21 on either
# while most file names and code score well above 0.5.
OPEN_FROM = 0.5
CODE_BELOW = 0.5
FILE_BELOW = 0.5
TIMEOUT_SECONDS = 5
# Jev list price; output tokens are free.
INPUT_PRICE_PER_MILLION_USD = 0.042

TLDS = frozenset(
    line.strip().lower()
    for line in Path(__file__).with_name("tlds.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)
_SHAPE = re.compile(
    r"^(?:(?P<scheme>https?)://)?(?P<host>[^\s/:?#@]+)(?::(?P<port>\d{1,5}))?(?P<rest>[/?#]\S*)?$",
    re.IGNORECASE,
)
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

QUESTION = "Does the user want to open TYPED as a website, rather than search the web for it?"
OPEN = (
    "TYPED is the address of a site or page to visit, such as a product, company, project, "
    "tool, service or person's site, including sites on newer top-level domains like "
    ".dev, .app, .io, .ai or .ing."
)
SEARCH = (
    "TYPED is a file name, a line or piece of code such as an object and its method or "
    "attribute, a version number, or a term the user wants to look up."
)
FILE_QUESTION = "Is TYPED a file name, such as a source file, script, document or archive?"
CODE_QUESTION = (
    "Is TYPED a piece of code, such as a variable or object followed by a method or "
    "attribute (user.name, df.style), or a phrase from code (hello.world)?"
)


@dataclass(frozen=True)
class Address:
    typed: str
    host: str
    url: str
    # A scheme, localhost or an IP address: open without asking Jev.
    explicit: bool


def _ascii(host: str) -> str | None:
    try:
        labels = [label.encode("idna").decode("ascii") for label in host.split(".")]
    except UnicodeError:
        return None
    if not all(_LABEL.match(label) for label in labels):
        return None
    return ".".join(labels)


def parse(text: str) -> Address | None:
    """The address ``text`` names, or None when it isn't one."""
    typed = text.strip()
    match = _SHAPE.match(typed)
    if match is None:
        return None
    scheme = (match["scheme"] or "").lower()
    host = match["host"].rstrip(".").lower()
    port = f":{match['port']}" if match["port"] else ""
    rest = match["rest"] or ""
    local = host == "localhost"
    try:
        ipaddress.IPv4Address(host)
        local = True
    except ValueError:
        pass
    if not local:
        host = _ascii(host)
        if host is None or "." not in host or host.rsplit(".", 1)[1] not in TLDS:
            return None
    url = f"{scheme or ('http' if local else 'https')}://{host}{port}{rest}"
    return Address(typed=typed, host=host, url=url, explicit=bool(scheme) or local)


def material(found: Address) -> str:
    # Telling Jev the ending is a registered top-level domain made code like
    # client.chat look like a site, and newer TLDs open without it.
    return (
        "The user typed this into their browser's address bar, which sent it to their "
        f"search engine.\n\nTYPED: {found.typed}"
    )


def build_client():
    import httpx2
    from typesafe_sdk import AsyncTypeSafeClient

    # gzip only: httpx2 can't decode TypeSafe's brotli responses.
    return AsyncTypeSafeClient(
        model=ADDRESS_MODEL,
        timeout=TIMEOUT_SECONDS,
        http_client=httpx2.AsyncClient(headers={"Accept-Encoding": "gzip, deflate"}),
    )


def decide(probabilities: dict[str, float]) -> bool:
    return (
        probabilities["open"] >= OPEN_FROM
        and probabilities["code"] < CODE_BELOW
        and probabilities["file"] < FILE_BELOW
    )


async def ask_jev(found: Address, client=None) -> tuple[dict[str, float], dict]:
    """Jev's probabilities that the user wants to open the address, that it is
    a file name and that it is code; and the usage."""
    from typesafe_sdk import Noul
    from typesafe_sdk import NoulCriteria

    own = client is None
    client = client or build_client()
    try:
        result = await client.system_one(
            state=material(found),
            questions={
                "open": Noul(
                    instructions=QUESTION, criteria=NoulCriteria(true=OPEN, false=SEARCH)
                ),
                "file": Noul(instructions=FILE_QUESTION),
                "code": Noul(instructions=CODE_QUESTION),
            },
        )
    finally:
        if own:
            await client.aclose()
    tokens = result.usage.input_tokens
    probabilities = {name: answer.noul for name, answer in result.nouls.items()}
    return probabilities, {
        "model": ADDRESS_MODEL,
        "input_tokens": tokens,
        "cost_usd": round(tokens * INPUT_PRICE_PER_MILLION_USD / 1_000_000, 8),
    }


async def address_to_open(text: str) -> tuple[str | None, dict]:
    """The URL to open for ``text``, or None to search it; and Jev's usage,
    empty when Jev wasn't asked."""
    found = parse(text)
    if found is None:
        return None, {}
    if found.explicit:
        return found.url, {}
    probabilities, usage = await ask_jev(found)
    usage["probabilities"] = {name: round(p, 3) for name, p in probabilities.items()}
    return (found.url if decide(probabilities) else None), usage
