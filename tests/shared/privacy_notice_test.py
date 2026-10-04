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
    assert "only anonymous usage and cost records stay" in points
    assert "including from the chat bot's memories" in points
    assert "send a direct message to anyone with the @admin role" in points
    assert "within 30 days" in points


def test_every_deletion_request_goes_to_the_admin_role(notice):
    assert notice.count("anyone with the @admin role") == 3
    deleting = notice.split("## Deleting your data", 1)[1]
    assert "within 30 days of your request" in deleting
    assert "backup" not in notice.lower()


def test_the_notice_does_not_describe_an_opt_out_as_available(notice):
    lowered = notice.lower()
    assert "opt-out" not in lowered
    assert "you can opt out" not in lowered
    assert "forget" not in lowered
    assert "blank" not in lowered
    assert "the bot never resets them" in lowered


def test_a_deletion_request_removes_the_person_from_the_chat_bot(notice):
    deleted = notice.split("**What we delete.**", 1)[1].split("**", 1)[0]
    assert "removes you from its memories" in deleted
    assert "conversation summaries" in deleted
    assert "cannot remove" not in notice.lower()


def test_a_deleted_person_stays_on_the_chat_bots_blocked_list(notice):
    kept = notice.split("**What we keep.**", 1)[1].split("\n\n", 1)[0]
    assert "chat bot's blocked list" in kept
    assert "reach the chat bot only as `[BLOCKED BY USER]`" in kept
    assert "does not respond to you" in kept
    assert "never reads" not in notice


def test_the_notice_says_the_blocked_list_covers_the_chat_bot_only(notice):
    assert "The blocked list covers the chat bot only" in notice


def test_bytes_transfers_are_anonymised_not_deleted(notice):
    deleted = notice.split("**What we delete.**", 1)[1].split("**What we keep", 1)[0]
    assert "bytes transfers you sent or received stay" in deleted
    assert "username and the reason removed" in deleted


def test_the_notice_gives_each_message_hand_off_its_limit(notice):
    messages = notice.split("**Messages.**", 1)[1].split("**The chat bot's", 1)[0]
    assert "server automation, its text is handed to the job" in messages
    assert "kept for 1 hour" in messages
    assert "dropped once they are 48 hours old (checked every 15 minutes)" in messages
    assert "up to 48 hours after it picks them up" in messages
    assert "until the reminder runs, and for up to 7 days after" in messages
    assert "up to 16 KB" in messages
    assert "That memory has no time limit: it lasts as long as the automation exists" in messages
    assert "can paraphrase or quote members" in messages
    assert "the old ones have no time limit" in messages
    unbounded = ("That history has no time limit", "That memory has no time limit", "the old ones have no time limit")
    for phrase in unbounded:
        messages = messages.replace(phrase, "")
    assert "no time limit" not in messages


def test_ai_records_keep_no_words(notice):
    records = notice.split("**Records of what the AI did.**", 1)[1].split("**Moderation", 1)[0]
    assert "The words do not" in records
    assert "the AI's own replies, running notes, reasoning and what it passed to its tools" in records
    assert "the search and the results it saw are kept for 48 hours" in records
    assert "are saved as a placeholder instead" in records
    assert "can quote" not in records
    commands = notice.split("**Commands and automations.**", 1)[1].split("\n", 1)[0]
    assert "the bot's answers are not kept" in commands


def test_the_notice_does_not_call_logs_text_free(notice):
    assert "some can include the text of a message" in notice


def test_security_events_name_no_member(notice):
    security = notice.split("**Security.**", 1)[1].split("\n", 1)[0]
    assert "failed authentication" in security
    assert "over a rate limit" in security
    assert "admin operations" in security
    assert "None records a member's Discord ID" in security
    assert "Ordinary requests are not logged" in security
    assert "Pydantic Logfire" in security
    assert "kept for 30 days." in security


def test_no_placeholder_is_left_in_the_notice(notice):
    assert "[PLACEHOLDER" not in notice


def test_the_notice_names_what_a_deletion_keeps(notice):
    kept = notice.split("**What we keep.**", 1)[1].split("**", 1)[0]
    assert "Moderation history" in kept
    assert "Anonymous usage and cost records" in kept
    assert "completed" in kept


@pytest.mark.parametrize(
    "product",
    [
        "Gym and Labs",
        "Chat, search and the AI features",
        "Your account",
        "TypeSafe",
        "Resend",
        "Pydantic Logfire",
        "Polar",
    ],
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
