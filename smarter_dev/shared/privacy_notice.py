"""The public privacy notice, and the short versions cut from it.

``privacy_notice.md`` beside this module is the one copy of the wording. The
site renders all of it at :data:`PRIVACY_PATH`; the ``/privacy`` bot command
and the ``#privacy-information`` channel post both use its "The short version"
section, so the three cannot drift apart. Read in Discord, they drop the
notice's "on the Smarter Dev Discord server": the page needs it, because
people reach it from outside Discord. ``docs/privacy-channel-post.md`` is
the channel post as generated here, committed for the admin to paste; a test
fails when it goes stale.

The notice has to match what the code does. When storage, retention or the
deletion path changes, change the notice in the same commit, alongside
``docs/data-retention.md`` and ``docs/privacy-deletion-runbook.md``.
"""

from __future__ import annotations

from datetime import date
from functools import cache
from pathlib import Path

from smarter_dev.shared.config import get_settings

PRIVACY_PATH = "/privacy"
NOTICE_TITLE = "Privacy notice"
LAST_UPDATED = date(2026, 10, 4)

_NOTICE_FILE = Path(__file__).with_name("privacy_notice.md")
_SHORT_VERSION_HEADING = "## The short version"
_SERVER_SUFFIX = " on the Smarter Dev Discord server"


@cache
def notice_markdown() -> str:
    """The full notice, as markdown."""
    return _NOTICE_FILE.read_text(encoding="utf-8")


def short_version() -> tuple[str, ...]:
    """The bullets of the notice's "The short version" section, in order."""
    section = notice_markdown().split(_SHORT_VERSION_HEADING, 1)[1]
    section = section.split("\n## ", 1)[0]
    return tuple(
        line[2:].strip() for line in section.splitlines() if line.startswith("- ")
    )


def discord_short_version() -> tuple[str, ...]:
    """The short version as read inside Discord, where the server goes without saying."""
    return tuple(point.replace(_SERVER_SUFFIX, "") for point in short_version())


def privacy_url() -> str:
    """The notice's public URL on this deployment."""
    return f"{get_settings().site_base_url.rstrip('/')}{PRIVACY_PATH}"


def command_response(url: str) -> str:
    """What ``/privacy`` answers: the short version and the link."""
    bullets = "\n".join(f"- {point}" for point in discord_short_version())
    return (
        f"**How Smarter Dev handles your data**\n{bullets}\n\n"
        f"Read the full privacy notice: {url}"
    )


def channel_post(url: str) -> str:
    """The ``#privacy-information`` post: the short version and the link."""
    bullets = "\n".join(f"- {point}" for point in discord_short_version())
    return (
        f"# {NOTICE_TITLE}\n"
        "What Smarter Dev stores about you here and on smarter.dev, and how to "
        "have it deleted.\n\n"
        f"{bullets}\n\n"
        f"The full notice: {url}\n"
        "Use `/privacy` anywhere in the server to get this link again.\n"
        f"-# Last updated {LAST_UPDATED:%B} {LAST_UPDATED.day}, {LAST_UPDATED.year}"
    )


if __name__ == "__main__":
    # Regenerates docs/privacy-channel-post.md:
    #   python -m smarter_dev.shared.privacy_notice > docs/privacy-channel-post.md
    print(channel_post("https://smarter.dev/privacy"))
