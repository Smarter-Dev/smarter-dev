"""The privacy notice's wording, and the short versions cut from it (task #71).

The notice is a public promise about what the code does, so these tests pin
the statements that must stay true and the ones it must never make: the chat
bot's memories are permanent and never reset, moderation history survives a
deletion request, and no opt-out or automatic deletion exists yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smarter_dev.shared import privacy_notice
from smarter_dev.shared.privacy_notice import channel_post
from smarter_dev.shared.privacy_notice import command_response
from smarter_dev.shared.privacy_notice import notice_markdown
from smarter_dev.shared.privacy_notice import short_version

REPO = Path(__file__).resolve().parents[2]
CHANNEL_POST = REPO / "docs" / "privacy-channel-post.md"
PUBLIC_URL = "https://smarter.dev/privacy"
DISCORD_MESSAGE_LIMIT = 2000


@pytest.fixture(scope="module")
def notice() -> str:
    return " ".join(notice_markdown().split())


def test_the_short_version_carries_the_required_statements():
    points = " ".join(short_version())
    assert len(short_version()) == 5
    assert "permanent memories" in points
    assert "never resets" in points
    assert "Moderation history is kept" in points
    assert "anonymised" in points
    assert "direct message to the Smarter Dev admin" in points


def test_the_notice_never_promises_what_does_not_exist(notice):
    lowered = notice.lower()
    assert "no opt-out or automatic deletion yet" in lowered
    assert "opt out of" not in lowered
    assert "forget" not in lowered
    assert "blank" not in lowered
    assert "the bot never resets them" in lowered


def test_the_notice_says_what_a_deletion_request_cannot_remove_yet(notice):
    cannot = notice.split("**What we cannot remove yet.**", 1)[1]
    cannot = cannot.split("##", 1)[0]
    assert "What the chat bot remembers about you" in cannot
    assert "summaries" in cannot
    assert "logs, backups" in cannot.lower()
    assert "proactive agent's history has no timer" in cannot


def test_the_notice_names_what_a_deletion_keeps(notice):
    kept = notice.split("**What we keep.**", 1)[1].split("**", 1)[0]
    assert "Moderation history" in kept
    assert "anonymised" in kept
    assert "completed" in kept


@pytest.mark.parametrize(
    "product",
    ["Gym and Labs", "Chat, search and the AI features", "Your account"],
)
def test_the_notice_covers_the_site(notice, product):
    assert product in notice


def test_the_command_response_is_the_short_version_and_the_link():
    response = command_response(PUBLIC_URL)
    for point in short_version():
        assert point in response
    assert response.endswith(PUBLIC_URL)
    assert len(response) <= DISCORD_MESSAGE_LIMIT


def test_the_channel_post_is_the_short_version_and_the_link():
    post = channel_post(PUBLIC_URL)
    for point in short_version():
        assert point in post
    assert PUBLIC_URL in post
    assert "/privacy" in post
    assert len(post) <= DISCORD_MESSAGE_LIMIT


def test_the_committed_channel_post_is_current():
    """``docs/privacy-channel-post.md`` is what the admin pastes; regenerate it
    with ``python -m smarter_dev.shared.privacy_notice > docs/privacy-channel-post.md``
    after a wording change."""
    assert CHANNEL_POST.read_text() == channel_post(PUBLIC_URL) + "\n"


def test_the_url_follows_the_deployment(monkeypatch):
    settings = privacy_notice.get_settings()
    monkeypatch.setattr(settings, "site_base_url", "https://example.test/")
    assert privacy_notice.privacy_url() == "https://example.test/privacy"
