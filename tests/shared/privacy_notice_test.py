"""The privacy notice's wording, its Discord copy and the /privacy summary (task #71).

The notice is a public promise about what the code does, so these tests pin
the statements that must stay true and the ones it must never make: each
retention figure against the constant behind it, what a deletion keeps and
why, and that the notice names no mechanism. Zech's rules for its shape (#86):
no short version, no references between sections, no fact said twice.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from smarter_dev.shared import privacy_notice
from smarter_dev.shared.privacy_notice import DISCORD_MESSAGE_LIMIT
from smarter_dev.shared.privacy_notice import channel_post
from smarter_dev.shared.privacy_notice import channel_post_parts
from smarter_dev.shared.privacy_notice import command_response
from smarter_dev.shared.privacy_notice import discord_markdown
from smarter_dev.shared.privacy_notice import notice_markdown
from smarter_dev.shared.retention_policy import IN_FLIGHT_MAX
from smarter_dev.shared.retention_policy import OPERATIONAL_MAX
from smarter_dev.web.web_search.anonymous import (
    TTL_SECONDS as ANONYMOUS_SEARCH_TTL_SECONDS,
)

REPO = Path(__file__).resolve().parents[2]
CHANNEL_POST = REPO / "docs" / "privacy-channel-post.md"
PUBLIC_URL = "https://smarter.dev/privacy"
HEADINGS = [
    "What we store and why",
    "How long we keep it",
    "Who else handles your data",
    "Deleting your data",
    "Changes",
]


@pytest.fixture(scope="module")
def notice() -> str:
    return " ".join(notice_markdown().split())


def _section(notice: str, heading: str) -> str:
    return notice.split(f"## {heading}", 1)[1].split(" ## ", 1)[0]


def _item(text: str, label: str) -> str:
    return text.split(f"**{label}", 1)[1].split(" - ", 1)[0].split(" ## ", 1)[0]


def test_the_notice_has_one_section_per_question_and_no_short_version(notice):
    positions = [notice.index(f"## {heading}") for heading in HEADINGS]
    assert positions == sorted(positions)
    assert notice.count("## ") == len(HEADINGS)
    assert "short version" not in notice.lower()
    assert notice.startswith(
        "Smarter Dev LLC runs the Smarter Dev Discord server, its bot and smarter.dev. "
        "This is what we store about you, how long we keep it, and how to have it deleted."
    )


def test_no_section_points_at_another(notice):
    lowered = notice.lower()
    for reference in ("above", "below", "full notice", "see the", 'see "', 'under "', "this section", "listed in"):
        assert reference not in lowered


def test_no_sentence_is_said_twice(notice):
    plain = " ".join(discord_markdown().split())
    sentences = [s.strip() for s in re.split(r"(?<=[.;:])\s", notice) if len(s.strip()) > 30]
    assert len(sentences) == len(set(sentences))
    for fact in (
        "admin@smarter.dev",
        "DM an @admin",
        "forever",
        "permanently",
        "no time limit",
        "at most 6 hours",
        "at most 30 days",
        "within 30 days",
        "30 minutes",
        "until you ask us to delete",
        "Polar",
        "stay signed in",
    ):
        assert plain.count(fact) == 1, fact


def test_what_we_store_states_no_retention(notice):
    stored = _section(notice, "What we store and why")
    for word in ("until", "at most", "forever", "permanent", "days", "hours", "delete"):
        assert word not in stored.replace("deleted messages", "")


def test_the_bot_is_in_one_server(notice):
    lowered = notice.lower()
    for plural in ("discord servers", "servers it is in", "each server", "every server"):
        assert plural not in lowered
    assert "One memory of the server" in _item(notice, "The chat bot.")
    assert "The bot reads messages in the Smarter Dev Discord server" in notice


def test_the_stated_limits_are_the_code_limits(notice):
    retention = _section(notice, "How long we keep it")
    assert IN_FLIGHT_MAX == timedelta(hours=6)
    assert "Copies the bot or the AI holds while it works, and the progress it shows you: at most 6 hours." in retention
    assert ANONYMOUS_SEARCH_TTL_SECONDS == 30 * 60
    assert "A search made with your search link while signed out: 30 minutes." in retention
    assert OPERATIONAL_MAX == timedelta(days=30)
    assert "Operations data: at most 30 days." in retention
    session_max_age = yaml.safe_load((REPO / "app.yaml").read_text())["session"]["max_age"]
    assert session_max_age == 30 * 86400
    assert "You stay signed in for 30 days after your last visit." in retention
    assert "Our servers' own logs have no fixed time limit." in retention
    assert "sign-in sessions" not in _item(notice, "Operations.")
    assert "Your account, membership record and website content: until you delete them." in retention
    assert "Chat bot conversations, Discord feature records, automation text and email sign-ups: until you ask us to delete them." in retention


def test_a_deletion_request_goes_to_the_admins_and_is_done_within_30_days(notice):
    deleting = _section(notice, "Deleting your data")
    assert "DM an @admin on the [Smarter Dev Discord server](https://discord.gg/de8kajxbYS) or email [admin@smarter.dev](mailto:admin@smarter.dev)." in deleting
    assert "We confirm you own the Discord account, and within 30 days the chat bot removes you from its memory and conversations and we delete the rest, except:" in deleting
    assert "backup" not in notice.lower()
    assert "cannot remove" not in notice.lower()


def test_self_deletion_names_where_and_what_it_leaves(notice):
    deleting = _section(notice, "Deleting your data")
    assert 'Delete your chats, questions about our resources and searches yourself where each is shown, or all at once in Account → Security & Accounts → "Your data"; anything still being answered is skipped.' in deleting
    assert "Deleting your account there removes all of these and your search link." in deleting


@pytest.mark.parametrize(
    ("kept", "says"),
    [
        ("Moderation history", "kept forever, to keep the server safe."),
        ("The chat bot's memory of the server", "kept permanently without you, so it keeps knowing the server."),
        ("Records of usage, cost and what the AI and automations did", "with no time limit and without your ID and name, though they still show which messages and channels were involved"),
        ("Bytes transfers", "in other members' histories, without your ID and name"),
        ("Your Discord ID", "so the chat bot leaves out your messages and does not respond to you."),
        ("Your name, Discord ID or email address", "on anything you set up as an admin, so admins can see who set it up."),
        ("Logs, and the bot's Discord posts and DMs", "which we do not edit."),
    ],
)
def test_what_a_deletion_keeps_is_listed_with_its_reason(notice, kept, says):
    assert says in _item(_section(notice, "Deleting your data"), kept)


def test_leaving_the_chat_bot_says_what_is_excluded_not_how(notice):
    kept_id = _item(_section(notice, "Deleting your data"), "Your Discord ID")
    assert "Other features still process your new messages, and new activity starts new records." in kept_id
    lowered = notice.lower()
    for word in ("blocked", "opt-out", "opt out", "opts you out"):
        assert word not in lowered


def test_the_notice_makes_no_promise_the_code_cannot_keep(notice):
    lowered = notice.lower()
    for promise in ("forget", "blank", "only the bot edits", "never reads", "never reset", "every copy", "straight away", "anonymous"):
        assert promise not in lowered


def test_the_notice_states_no_mechanisms(notice):
    lowered = notice.lower()
    for mechanism in (
        "up to",
        "roughly",
        "placeholder",
        "hourly",
        "every 15 minutes",
        "sweep",
        "skrift",
        "redis",
        "proactive",
        "chat agent",
        "api key",
        "`[",
        "_",
    ):
        assert mechanism not in lowered.replace("admin@smarter.dev", "")


def test_the_account_names_each_sign_in_provider_and_what_it_gives(notice):
    account = _item(notice, "Your account.")
    assert "Your profile as Discord, GitHub or Google shares it (ID, username, avatar, email and the public details on your profile) and the sign-in tokens it issues, to sign you in." in account


def test_ai_records_and_logs_say_what_text_they_hold(notice):
    assert "Whom the AI acted on, where, when, at what cost and what it decided, without the text of any message or reply, so we can check its behaviour and cost." in _item(notice, "AI records.")
    assert "error traces, which can include IDs and message text, so the services work, stay secure and can be fixed." in _item(notice, "Operations.")
    assert "naming people by username and Discord ID" in _item(notice, "The chat bot.")


def test_security_events_are_named(notice):
    operations = _item(notice, "Operations.")
    assert "failed API sign-ins with their IP address, rate-limit refusals and admin operations" in operations


@pytest.mark.parametrize(
    "processor",
    ["Google", "OpenAI", "Anthropic", "OpenRouter", "OpenCode Zen", "TypeSafe", "Brave", "Jina", "DigitalOcean", "Polar", "Resend", "Pydantic Logfire", "Discord holds what you post there"],
)
def test_every_processor_is_named(notice, processor):
    assert processor in _section(notice, "Who else handles your data")


def test_processors_keep_their_own_copies(notice):
    who = _section(notice, "Who else handles your data")
    assert "the chat bot includes each author's Discord ID" in who
    assert "Each keeps data under its own terms, and deleting yours here does not delete their copies." in who


def test_no_placeholder_is_left_in_the_notice(notice):
    assert "[PLACEHOLDER" not in notice


def test_the_channel_carries_the_whole_notice_in_messages_under_the_cap():
    parts = channel_post_parts(PUBLIC_URL)
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= DISCORD_MESSAGE_LIMIT
    joined = "\n\n".join(parts)
    title = f"# {privacy_notice.NOTICE_TITLE}\n"
    footer = f"\n\n{PUBLIC_URL}\n-# Last updated October 6, 2026"
    assert joined.startswith(title) and joined.endswith(footer)
    assert joined.removeprefix(title).removesuffix(footer) == discord_markdown().strip()
    assert discord_markdown() == notice_markdown().replace(
        "[admin@smarter.dev](mailto:admin@smarter.dev)", "admin@smarter.dev"
    )
    assert "mailto" not in joined
    for part in parts[1:]:
        assert part.startswith("## ") or part.startswith(PUBLIC_URL)


def test_the_committed_channel_post_is_current():
    """``docs/privacy-channel-post.md`` is what the admin pastes, one message
    per marker; regenerate it with
    ``python -m smarter_dev.shared.privacy_notice > docs/privacy-channel-post.md``
    after a wording change."""
    text = CHANNEL_POST.read_text()
    assert text == channel_post(PUBLIC_URL) + "\n"
    parts = channel_post_parts(PUBLIC_URL)
    markers = re.findall(r"^=== Message (\d+) of (\d+) ===$", text, re.M)
    assert markers == [(str(n), str(len(parts))) for n in range(1, len(parts) + 1)]


def test_the_command_is_a_summary_built_from_the_notice(notice):
    response = command_response(PUBLIC_URL)
    assert response == (
        "Smarter Dev stores data about you for Discord features, the chat bot, "
        "moderation, automations, AI records, your account, website content, "
        "billing, email and operations. To have most of it deleted, DM an @admin on the "
        "Smarter Dev Discord server or email admin@smarter.dev. The full privacy "
        "notice: https://smarter.dev/privacy"
    )
    assert len(re.findall(r"\. ", response)) == 2
    assert len(response) <= DISCORD_MESSAGE_LIMIT


def test_the_url_follows_the_deployment(monkeypatch):
    settings = privacy_notice.get_settings()
    monkeypatch.setattr(settings, "site_base_url", "https://example.test/")
    assert privacy_notice.privacy_url() == "https://example.test/privacy"
