"""The public privacy notice, its Discord copy and the ``/privacy`` summary.

``privacy_notice.md`` beside this module is the one copy of the wording. The
site renders it at :data:`PRIVACY_PATH`. The ``#privacy-information`` channel
carries the whole notice, split at section boundaries into messages under
Discord's 2,000-character cap; ``docs/privacy-channel-post.md`` is that post
as generated here, committed for the admin to paste, and a test fails when it
goes stale. ``/privacy`` answers with a short summary built from the notice's
own headings and deletion sentence, so it cannot drift from it either.

The notice has to match what the code does. When storage, retention or the
deletion path changes, change the notice in the same commit, alongside
``docs/data-retention.md`` and ``docs/privacy-deletion-runbook.md``.
"""

from __future__ import annotations

import re
from datetime import date
from functools import cache
from pathlib import Path

from smarter_dev.shared.config import get_settings

PRIVACY_PATH = "/privacy"
NOTICE_TITLE = "Privacy notice"
LAST_UPDATED = date(2026, 10, 6)
DISCORD_MESSAGE_LIMIT = 2000

_NOTICE_FILE = Path(__file__).with_name("privacy_notice.md")
_SECTION_BREAK = "\n\n## "
_STORE_HEADING = "## What we store and why"
_DELETE_HEADING = "## Deleting your data"
_ASK_PREFIX = "For anything else, "
_PART_MARKER = "=== Message {number} of {total} ==="
_PROPER_NOUNS = {"Discord", "Smarter", "Polar"}


@cache
def notice_markdown() -> str:
    """The full notice, as markdown."""
    return _NOTICE_FILE.read_text(encoding="utf-8")


def privacy_url() -> str:
    """The notice's public URL on this deployment."""
    return f"{get_settings().site_base_url.rstrip('/')}{PRIVACY_PATH}"


def _section(heading: str, text: str) -> str:
    return text.split(heading, 1)[1].split(_SECTION_BREAK, 1)[0]


def discord_markdown() -> str:
    """The notice as Discord renders it: an email link becomes the bare address.

    Discord's masked links take only web URLs, so ``[x](mailto:x)`` would show
    as raw markdown there.
    """
    return re.sub(r"\[([^\]]+)\]\(mailto:[^)]+\)", r"\1", notice_markdown())


def _in_a_sentence(label: str) -> str:
    """``label`` as it reads mid-sentence: "The chat bot" -> "the chat bot".

    A first word with a capital after its first letter ("AI", "GitHub") or
    named in ``_PROPER_NOUNS`` keeps its case.
    """
    first = label.split(" ", 1)[0]
    if first in _PROPER_NOUNS or any(c.isupper() for c in first[1:]):
        return label
    return label[0].lower() + label[1:]


def _listed(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"


def command_response(url: str) -> str:
    """What ``/privacy`` answers: what we store, how to have it deleted, the link.

    Built from the notice's "What we store and why" labels and the sentence
    of its deletion section that starts "For anything else, ", so a wording
    change there changes this too; a test pins the result.
    """
    store = _section(_STORE_HEADING, discord_markdown())
    labels = [
        _in_a_sentence(label.rstrip("."))
        for label in re.findall(r"^- \*\*(.+?)\*\*", store, re.M)
    ]
    ask = next(
        line
        for line in _section(_DELETE_HEADING, discord_markdown()).splitlines()
        if line.startswith(_ASK_PREFIX)
    )
    how = ask.removeprefix(_ASK_PREFIX).split(". ", 1)[0].rstrip(".")
    how = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", how)
    return (
        f"Smarter Dev stores data about you for {_listed(labels)}. "
        f"To have most of it deleted, {how}. "
        f"The full privacy notice: {url}"
    )


def channel_post_parts(url: str) -> list[str]:
    """The whole notice as Discord messages, split at section boundaries."""
    sections = discord_markdown().strip().split(_SECTION_BREAK)
    blocks = [f"# {NOTICE_TITLE}\n{sections[0]}"]
    blocks += [f"## {section}" for section in sections[1:]]
    footer = f"{url}\n-# Last updated {LAST_UPDATED:%B} {LAST_UPDATED.day}, {LAST_UPDATED.year}"
    parts: list[str] = []
    current = ""
    for block in blocks + [footer]:
        if len(block) > DISCORD_MESSAGE_LIMIT:
            raise ValueError(
                f"a notice section is over {DISCORD_MESSAGE_LIMIT} characters"
            )
        joined = f"{current}\n\n{block}" if current else block
        if len(joined) > DISCORD_MESSAGE_LIMIT:
            parts.append(current)
            current = block
        else:
            current = joined
    parts.append(current)
    return parts


def channel_post(url: str) -> str:
    """``docs/privacy-channel-post.md``: each message under a marker line, in order."""
    parts = channel_post_parts(url)
    return "\n\n".join(
        f"{_PART_MARKER.format(number=number, total=len(parts))}\n{part}"
        for number, part in enumerate(parts, 1)
    )


if __name__ == "__main__":
    # Regenerates docs/privacy-channel-post.md:
    #   python -m smarter_dev.shared.privacy_notice > docs/privacy-channel-post.md
    print(channel_post("https://smarter.dev/privacy"))
