"""The privacy policy's wording, its Discord copy and the /privacy summary (task #71).

The policy is a public promise about what the code does, so these tests pin
the statements that must stay true and the ones it must never make: each
retention figure against the constant behind it, what a deletion keeps and
why, and that the policy names no mechanism. Zech's rules for its shape (#86):
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
    "Data we collect",
    "Why we collect it",
    "How long we keep it",
    "Who else processes your data",
    "Deleting your data",
    "Data we retain",
    "Changes",
]


@pytest.fixture(scope="module")
def notice() -> str:
    return " ".join(notice_markdown().split())


def _section(notice: str, heading: str) -> str:
    return notice.split(f"## {heading}", 1)[1].split(" ## ", 1)[0]


def _item(text: str, label: str) -> str:
    return text.split(f"**{label}", 1)[1].split(" - ", 1)[0].split(" ## ", 1)[0]


def test_the_policy_has_one_section_per_question_and_no_short_version(notice):
    positions = [notice.index(f"## {heading}") for heading in HEADINGS]
    assert positions == sorted(positions)
    assert notice.count("## ") == len(HEADINGS)
    assert "short version" not in notice.lower()
    assert notice.startswith(
        'Smarter Dev LLC ("we") operates the Smarter Dev Discord server, its bot and smarter.dev. '
        "This policy explains what personal data we collect through them, why, how long we keep it "
        "and how to have it deleted."
    )


def test_no_section_points_at_another(notice):
    lowered = notice.lower()
    for reference in ("above", "below", "full notice", "see the", 'see "', 'under "', "this section", "listed in"):
        assert reference not in lowered


def test_no_fact_is_said_twice(notice):
    plain = " ".join(discord_markdown().split())
    sentences = [s.strip() for s in re.split(r"(?<=[.;:])\s", notice) if len(s.strip()) > 30]
    assert len(sentences) == len(set(sentences))
    for fact in (
        "admin@smarter.dev",
        "message an @admin",
        "indefinitely",
        "no fixed time limit",
        "at most 6 hours",
        "at most 30 days",
        "within 30 days",
        "30 days after your last visit",
        "until you delete",
        "until you ask us to delete",
        "removed from it",
        "Polar",
        "Card details",
        "do not include message text",
    ):
        assert plain.count(fact) == 1, fact


def test_what_we_collect_and_why_state_no_retention(notice):
    collected = _section(notice, "Data we collect") + _section(notice, "Why we collect it")
    for word in ("until", "at most", "indefinitely", "days", "hours", "delet"):
        assert word not in collected


def test_the_bot_is_in_one_server(notice):
    lowered = notice.lower()
    for plural in ("discord servers", "servers it is in", "each server", "every server"):
        assert plural not in lowered
    assert "Messages, reactions and commands you post in the server." in notice


def test_messages_are_not_archived(notice):
    collected = _section(notice, "Data we collect")
    assert (
        "Messages, reactions and commands you post in the server. We keep records derived from them, "
        "such as activity and participation, and the conversations our AI assistant takes part in. "
        "We do not archive your messages."
    ) in collected
    assert "Moderation actions taken on your account, with the messages involved." in collected


def test_the_stated_limits_are_the_code_limits(notice):
    retention = _section(notice, "How long we keep it")
    assert IN_FLIGHT_MAX == timedelta(hours=6)
    assert timedelta(seconds=ANONYMOUS_SEARCH_TTL_SECONDS) <= IN_FLIGHT_MAX
    assert "Temporary working copies and in-progress results: at most 6 hours." in retention
    assert OPERATIONAL_MAX == timedelta(days=30)
    assert "Error reports, security events, rate limits and caches: at most 30 days." in retention
    assert "Our servers' own logs: no fixed time limit." in retention
    session_max_age = yaml.safe_load((REPO / "app.yaml").read_text())["session"]["max_age"]
    assert session_max_age == 30 * 86400
    assert "Sign-in sessions: 30 days after your last visit." in retention
    assert "Account, subscription records and website content: until you delete them." in retention
    assert "Discord records, AI assistant conversations and email sign-ups: until you ask us to delete them." in retention
    assert "Moderation history and usage records: indefinitely." in retention
    assert "|" not in retention


def test_a_deletion_request_goes_to_the_admins_and_is_done_within_30_days(notice):
    deleting = _section(notice, "Deleting your data")
    assert "For everything else, message an @admin on the [Smarter Dev Discord server](https://discord.gg/de8kajxbYS) or email [admin@smarter.dev](mailto:admin@smarter.dev)." in deleting
    assert "After confirming you own the Discord account, we delete your data within 30 days, including removing you from the AI assistant's conversations." in deleting
    assert "backup" not in notice.lower()
    assert "cannot remove" not in notice.lower()


def test_after_a_request_only_the_assistant_ignores_you(notice):
    deleting = _section(notice, "Deleting your data")
    assert (
        "From then on the AI assistant ignores your messages and does not respond to you. "
        "Other features still process your new messages, and new activity starts new records."
    ) in deleting


def test_self_deletion_names_where(notice):
    deleting = _section(notice, "Deleting your data")
    assert "You can delete your website chats, questions, searches and your whole account from Account → Security & Accounts." in deleting


@pytest.mark.parametrize(
    ("kept", "says"),
    [
        ("Moderation history", "to keep the server safe."),
        ("The AI assistant's memory of the server", "with you removed from it, so it keeps knowing the server."),
        ("Your Discord ID", "so the AI assistant keeps ignoring your messages."),
        (
            "Records of usage, cost and AI decisions, and other members' transaction histories",
            "with your ID and name removed, though they still show which messages and channels were involved, "
            "so our accounts stay accurate and we can audit the AI.",
        ),
        ("Admin records", "your name, Discord ID or email address stays on it so admins know who set it up."),
        ("Our servers' own logs and messages the bot already posted on Discord", "including moderation log posts, which we do not edit."),
    ],
)
def test_what_a_deletion_keeps_is_listed_with_its_reason(notice, kept, says):
    assert says in _item(_section(notice, "Data we retain"), kept)


def test_leaving_the_assistant_says_what_is_excluded_not_how(notice):
    lowered = notice.lower()
    for word in ("blocked", "opt-out", "opt out", "opts you out", "exclusion"):
        assert word not in lowered


def test_the_policy_makes_no_promise_the_code_cannot_keep(notice):
    lowered = notice.lower()
    for promise in ("forget", "blank", "only the bot edits", "never reads", "never reset", "every copy", "straight away", "anonym", "request logs"):
        assert promise not in lowered


def test_the_policy_states_no_mechanisms(notice):
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


def test_what_is_collected_automatically(notice):
    collected = _section(notice, "Data we collect")
    assert "IP address with security events, error reports and server logs." in collected
    assert "Records of AI usage: who used it, when, where, what it cost and what it decided. These records do not include message text." in collected
    assert "Account details from the sign-in provider you choose (Discord, GitHub or Google): ID, username, avatar, email address and public profile details." in collected


@pytest.mark.parametrize(
    "processor",
    ["Google", "OpenAI", "Anthropic", "OpenRouter", "OpenCode Zen", "TypeSafe", "Brave", "Jina", "DigitalOcean", "Polar", "Resend", "Pydantic Logfire", "Discord stores everything posted there"],
)
def test_every_processor_is_named(notice, processor):
    assert processor in _section(notice, "Who else processes your data")


def test_processors_keep_their_own_copies(notice):
    who = _section(notice, "Who else processes your data")
    assert "including authors' Discord IDs" in who
    assert "Each handles data under its own privacy policy. Deleting your data from us does not delete their copies." in who


def test_no_placeholder_is_left_in_the_policy(notice):
    assert "[PLACEHOLDER" not in notice


def test_the_channel_carries_the_whole_policy_in_messages_under_the_cap():
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


def test_the_command_is_a_summary_built_from_the_policy(notice):
    response = command_response(PUBLIC_URL)
    assert response == (
        "Smarter Dev collects data about you to run the server, bot and website, "
        "to let the AI assistant take part in conversations and remember the server, "
        "to moderate the server and keep it safe, to sign you in, bill you and send you "
        "email you asked for, and to monitor cost, abuse and errors. To have most of it "
        "deleted, message an @admin on the Smarter Dev Discord server or email "
        "admin@smarter.dev. The full privacy policy: https://smarter.dev/privacy"
    )
    assert len(re.findall(r"\. ", response)) == 2
    assert len(response) <= DISCORD_MESSAGE_LIMIT


def test_the_url_follows_the_deployment(monkeypatch):
    settings = privacy_notice.get_settings()
    monkeypatch.setattr(settings, "site_base_url", "https://example.test/")
    assert privacy_notice.privacy_url() == "https://example.test/privacy"
