"""The privacy notice's wording, and the short versions cut from it (task #71).

The notice is a public promise about what the code does, so these tests pin
the statements that must stay true and the ones it must never make: the chat
bot's memories are permanent, moderation history survives a deletion
request, opting out of the chat bot comes only with a deletion, and the
notice names no mechanism.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from smarter_dev.shared import privacy_notice
from smarter_dev.shared.privacy_notice import channel_post
from smarter_dev.shared.privacy_notice import command_response
from smarter_dev.shared.privacy_notice import discord_short_version
from smarter_dev.shared.privacy_notice import notice_markdown
from smarter_dev.shared.privacy_notice import short_version
from smarter_dev.shared.retention_policy import IN_FLIGHT_MAX
from smarter_dev.shared.retention_policy import OPERATIONAL_MAX
from smarter_dev.web.web_search.anonymous import (
    TTL_SECONDS as ANONYMOUS_SEARCH_TTL_SECONDS,
)

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
    assert "The bot reads messages in the Smarter Dev Discord server, so its features can work" in points
    assert "Moderation history is kept forever to keep the server safe." in points
    assert "deletion request" not in points
    assert "A deletion keeps moderation history, usage and cost records with your ID and name removed, and a few other records the full notice lists" in points
    assert "including from the chat bot's memories" in points
    assert "DM an @admin on the Smarter Dev Discord server or email admin@smarter.dev. We delete it within 30 days." in points


def _section(notice: str, heading: str) -> str:
    return notice.split(f"## {heading}", 1)[1].split(" ## ", 1)[0]


def _item(notice: str, label: str) -> str:
    return notice.split(f"**{label}", 1)[1].split(" - **", 1)[0].split(" ## ", 1)[0]


def test_the_notice_is_laid_out_as_a_policy(notice):
    headings = [
        "The short version",
        "Who we are",
        "What we store and why",
        "How long we keep it",
        "Who else handles your data",
        "Deleting your data",
        "What we keep, and why",
        "Opting out of the chat bot",
        "Changes",
    ]
    positions = [notice.index(f"## {heading}") for heading in headings]
    assert positions == sorted(positions)
    stored = _section(notice, "What we store and why")
    for word in ("Kept until", "Kept for", "kept permanently", "at most", "until you", "deleted when"):
        assert word not in stored


def test_the_bot_is_in_one_server(notice):
    lowered = notice.lower()
    for plural in ("discord servers", "servers it is in", "each server", "every server", "for each guild"):
        assert plural not in lowered
    assert "one memory of the server" in _item(notice, "The chat bot.")
    assert "The bot reads messages in the Smarter Dev Discord server, so its features can work" in channel_post(PUBLIC_URL)


def test_every_deletion_request_goes_to_the_admin_role(notice):
    assert notice.count("DM an @admin on the") == 3
    assert "or email [admin@smarter.dev](mailto:admin@smarter.dev)." in notice
    deleting = _section(notice, "Deleting your data")
    assert "DM an @admin on the Smarter Dev Discord server, or email admin@smarter.dev." in deleting
    assert "Either way, we check that you own the Discord account" in deleting
    assert "within 30 days of your request" in deleting
    assert "backup" not in notice.lower()


# The five retention classes in docs/data-retention.md, each stated once as a
# fact. A stated limit must never be shorter than the code keeps the data.
def test_how_long_states_each_retention_class(notice):
    retention = _section(notice, "How long we keep it")
    assert "are kept for at most 6 hours, and most are deleted sooner, when the work finishes." in retention
    assert "A search made with your search link while signed out lasts 30 minutes." in retention
    assert "Rate limits, caches, security events, and errors and traces are kept for at most 30 days." in retention
    assert "You stay signed in for 30 days after your last visit." in retention
    assert "Our servers' own logs have no fixed time limit." in retention
    assert "Your account and what you make on the website are kept until you delete them or we delete them at your request." in retention
    assert "The chat bot's conversations, the other Discord features' records, automation text and email sign-ups are kept until you ask us to delete them." in retention
    assert "The chat bot's memories and moderation history are kept permanently." in retention
    assert "hold no message text. They have no time limit. A deletion request removes your ID and name from them." in retention


def test_the_stated_limits_are_the_code_limits(notice):
    retention = _section(notice, "How long we keep it")
    assert IN_FLIGHT_MAX == timedelta(hours=6)
    assert OPERATIONAL_MAX == timedelta(days=30)
    assert ANONYMOUS_SEARCH_TTL_SECONDS == 30 * 60
    assert "at most 6 hours" in retention
    assert "at most 30 days" in retention
    assert "lasts 30 minutes" in retention


def test_a_deletion_request_removes_the_person_from_the_chat_bot(notice):
    deleting = _section(notice, "Deleting your data")
    assert "The chat bot removes you from its memories and its conversations." in deleting
    assert "Everything else above that we keep until you ask, including your website account, is deleted or kept with your ID and name removed." in deleting
    assert "Apart from what we keep below, everything that can still mention you is gone within 30 days of your request." in deleting
    assert "cannot remove" not in notice.lower()


def test_self_deletion_names_where_and_what_it_leaves(notice):
    yourself = _item(notice, "Doing it yourself.")
    assert 'Account → Security & Accounts → "Your data" lets you delete your chats, your questions about our resources or your searches' in yourself
    assert "Anything still being answered is skipped; delete it once it finishes." in yourself
    assert "Each goes with its answers, files and every copy we keep, apart from our logs." in yourself
    assert "Deleting your account on the same page removes it with all of those and your search link." in yourself
    assert "What the Discord bot stores, including the chat bot's memories, is deleted only when you ask us." in yourself


@pytest.mark.parametrize(
    ("kept", "reason"),
    [
        ("Moderation history", "It is kept forever to keep the server safe."),
        ("The chat bot's memories.", "so they are never reset. A request removes you from them."),
        ("Records of usage, cost and what the AI and automations did", "such as which messages and channels were involved and what was done, with your ID and name removed."),
        ("Bytes transfers", "in the other member's history with your ID and name removed"),
        ("Your Discord ID alone on the chat bot's blocked list", "so the chat bot keeps leaving you out"),
        ("Your name, Discord ID or email address as the person who set up", "automations, extensions, campaigns, events or scheduled messages as a server admin"),
        ("A bare record that your request was completed", "with nothing that identifies you"),
        ("Logs and the bot's posts on Discord", "which are not edited to remove you"),
        ("Copies held by Discord and the services named above", "under their own terms"),
    ],
)
def test_what_a_deletion_keeps_is_listed_with_its_reason(notice, kept, reason):
    keep = _section(notice, "What we keep, and why")
    assert reason in _item(keep, kept)


def test_opting_out_says_what_is_excluded_not_how(notice):
    opting = _section(notice, "Opting out of the chat bot")
    assert "Asking us to delete your data also opts you out of the chat bot." in opting
    assert "your messages are left out of what the chat bot sees, and it does not respond to you. There is no separate opt-out." in opting
    assert "This covers the chat bot only: other AI features, such as `/help`, forum replies, server automations and moderation, still process your new messages" in opting
    assert "features like bytes start new records the next time you post" in opting
    lowered = notice.lower()
    assert "blocked by user" not in lowered
    assert "you can opt out" not in lowered
    assert "opt-out" not in lowered.replace("there is no separate opt-out.", "")


def test_the_notice_makes_no_promise_the_code_cannot_keep(notice):
    lowered = notice.lower()
    assert "forget" not in lowered
    assert "blank" not in lowered
    assert "only the bot edits" not in lowered
    assert "never reads" not in lowered
    assert "can no longer sign in" not in notice
    assert "straight away" not in notice


def test_the_account_names_each_sign_in_provider_and_what_it_gives(notice):
    account = _item(notice, "Your account.")
    assert "You sign in with Discord, GitHub or Google." in account
    assert "we receive your profile as that service shares it with us" in account
    assert "email address and whether it is verified, and the sign-in tokens it issues" in account
    assert "whether you use two-factor sign-in or Nitro" in account
    assert "Google's gives your first and last name, language and, for a work or school account, its domain" in account
    assert "GitHub's is your public profile" in account


def test_the_notice_states_no_mechanisms(notice):
    lowered = notice.lower()
    for mechanism in (
        "up to",
        "roughly",
        "placeholder",
        "folded",
        "hourly",
        "every 15 minutes",
        "16 kb",
        "last five",
        "api key",
        "proactive",
        "chat agent",
        "sweep",
        "skrift",
        "redis",
        "`[",
        "_",
    ):
        assert mechanism not in lowered.replace("admin@smarter.dev", "")


def test_ai_records_keep_no_words(notice):
    records = _item(notice, "Records of what the AI did.")
    assert "what it decided, but not the text of any message or reply, so we can check its behaviour and cost." in records
    assert "It names people by username and Discord ID" in notice


def test_the_notice_does_not_call_logs_text_free(notice):
    assert "some can include the text of a message" in notice


def test_security_events_name_no_member(notice):
    security = _item(notice, "Keeping things running and secure.")
    assert "Failed attempts to authenticate to our API with the IP address they came from" in security
    assert "requests refused for going over a rate limit" in security
    assert "admin operations" in security
    assert "none records a member's Discord ID" in security


def test_no_placeholder_is_left_in_the_notice(notice):
    assert "[PLACEHOLDER" not in notice


@pytest.mark.parametrize(
    "product",
    [
        "Gym and Labs",
        "**What you make on the website.**",
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
    for point in discord_short_version():
        assert point in response
    assert response.endswith(PUBLIC_URL)
    assert len(response) <= DISCORD_MESSAGE_LIMIT


def test_the_channel_post_is_the_short_version_and_the_link():
    post = channel_post(PUBLIC_URL)
    for point in discord_short_version():
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


def test_discord_drops_only_the_server_from_the_deletion_line():
    notice_points = short_version()
    discord_points = discord_short_version()
    assert discord_points[-1] == (
        "To have your data deleted, including from the chat bot's memories, "
        "DM an @admin or email admin@smarter.dev. We delete it within 30 days."
    )
    assert discord_points[:-1] == notice_points[:-1]
    assert channel_post(PUBLIC_URL).count("Smarter Dev Discord server") == 1


def test_the_short_versions_give_the_email_address():
    assert "admin@smarter.dev" in " ".join(short_version())
    assert "admin@smarter.dev" in channel_post(PUBLIC_URL)
    assert "admin@smarter.dev" in command_response(PUBLIC_URL)
