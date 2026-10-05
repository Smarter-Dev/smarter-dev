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
from smarter_dev.shared.privacy_notice import discord_short_version
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
    assert "Moderation history is kept so the server can stay safe" in points
    assert "is not part of a deletion request" in points
    assert "only anonymous usage and cost records stay" in points
    assert "including from the chat bot's memories" in points
    assert "DM an @admin on the Smarter Dev Discord server. We delete it within 30 days." in points
    assert "within 30 days" in points


def test_every_deletion_request_goes_to_the_admin_role(notice):
    assert notice.count("DM an @admin on the") == 3
    assert "or email [admin@smarter.dev](mailto:admin@smarter.dev)." in notice
    assert "For those, DM an @admin or email admin@smarter.dev." in notice
    assert "Moderation history, including those posts, is kept so the server can stay safe, and is not part of a deletion request." in notice
    deleting = notice.split("## Deleting your data", 1)[1]
    assert "within 30 days of your request" in deleting
    assert "DM an @admin on the Smarter Dev Discord server, or email admin@smarter.dev." in deleting
    assert "Either way, we check that you own the Discord account" in deleting
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
    assert "removes you from its memories and its conversations" in deleted
    assert "Everything else above that is kept until you ask us to delete it" in deleted
    assert "cannot remove" not in notice.lower()


def test_a_deleted_person_stays_on_the_chat_bots_blocked_list(notice):
    kept = notice.split("**What we keep.**", 1)[1].split("\n\n", 1)[0]
    assert "chat bot's blocked list" in kept
    assert "reach the chat bot only as `[BLOCKED BY USER]`" in kept
    assert "does not respond to you" in kept
    assert "never reads" not in notice


def test_the_notice_says_the_blocked_list_covers_the_chat_bot_only(notice):
    assert "The blocked list covers the chat bot only" in notice


def test_shared_records_are_anonymised_not_deleted(notice):
    deleted = notice.split("**What we delete.**", 1)[1].split("**What we keep", 1)[0]
    assert "is deleted or kept with your ID and name removed" in deleted
    assert "Bytes transfers you sent or received stay in the other member's history with your ID and name removed" in deleted
    assert "Records of what the AI and automations did, such as which messages and channels were involved and what was done, are kept with your ID and name removed." in deleted


def test_the_account_names_each_sign_in_provider_and_what_it_gives(notice):
    account = _store(notice, "Your account.")
    assert "You sign in with Discord, GitHub or Google." in account
    assert "we receive your profile as that service shares it with us" in account
    assert "email address and whether it is verified, and the sign-in tokens it issues" in account
    assert "whether you use two-factor sign-in or Nitro" in account
    assert "Google's gives your first and last name, language and, for a work or school account, its domain" in account
    assert "GitHub's is your public profile" in account
    assert "can no longer sign in" not in notice


def test_self_deletion_names_what_it_leaves(notice):
    assert "removes the account, its chat conversations and attachments, and your questions about our resources." in notice
    assert "Searches you made from your dashboard are kept until you ask us to delete them, as are anything the Discord bot stores and the chat bot's memories." in notice
    assert "A copy of each question you asked about our resources" not in notice
    assert "The AI's own copies of your questions about our resources and its answers are deleted as soon as it finishes each answer, or within 7 hours if it was cut off partway." in notice
    assert "straight away" not in notice


def _store(notice: str, kind: str) -> str:
    return notice.split(f"**{kind}**", 1)[1].split(" - **", 1)[0].split(" ## ", 1)[0]


# Each kind of data the notice lists, and the retention it states for it. A
# stated period must never be shorter than the code can keep the data; the
# stores behind each figure are in docs/data-retention.md and the runbook.
RETENTION = {
    "The chat bot's memories.": "Kept permanently",
    "The chat bot's conversations.": "Kept until you ask us to delete them",
    "Messages being handled.": "Kept for 5 days",
    "Server automations.": "Kept until you ask us to delete it",
    "Records of what the AI did.": "Kept until you ask us to delete them",
    "`/help` questions and the chat bot's web searches": "Kept for 3 days",
    "Moderation.": "Kept permanently",
    "Games and community features.": "Kept until you ask us to delete them",
    "Rate limits and caches.": "Kept for 30 days",
    "Test copies.": "Kept permanently",
    "Your account.": "Kept until you delete your account",
    "Chat.": "kept until you delete them or your account",
    "Searches": "kept until you ask us to delete them",
    "Email.": "Kept until you ask us to delete it",
    "Security.": "Kept for 30 days in Pydantic Logfire",
}


@pytest.mark.parametrize("kind", RETENTION)
def test_each_kind_of_data_states_how_long_it_is_kept(notice, kind):
    assert RETENTION[kind] in _store(notice, kind)


def test_the_notice_states_figures_not_mechanisms(notice):
    lowered = notice.lower()
    for mechanism in ("up to", "placeholder", "folded", "every 15 minutes", "16 kb", "last five", "api key", "proactive", "chat agent"):
        assert mechanism not in lowered
    assert "Searches made with your search link while signed out are kept for 30 minutes" in notice
    assert "You stay signed in for 30 days after your last visit" in notice
    assert "it keeps its own copy of the question, its research and its answer, deleted as soon as it finishes, or within 7 hours if it is cut off partway" in notice
    assert "The progress it shows you while it works is kept for 2 days." in notice
    monitoring = notice.split("**Monitoring.**", 1)[1].split(" - **", 1)[0]
    assert "Everything sent to Logfire is kept for 30 days" in monitoring
    assert "Our servers also keep their own logs, with no fixed time limit" in monitoring


def test_ai_records_keep_no_words(notice):
    records = _store(notice, "Records of what the AI did.")
    assert "without the words" in records
    assert "only the bot edits them" in _store(notice, "The chat bot's memories.")
    assert "It names people by username and Discord ID" in notice


def test_the_notice_does_not_call_logs_text_free(notice):
    assert "some can include the text of a message" in notice


def test_security_events_name_no_member(notice):
    security = _store(notice, "Security.")
    assert "Failed attempts to authenticate to our API with the IP address they came from" in security
    assert "requests refused for going over a rate limit" in security
    assert "admin operations" in security
    assert "None records a member's Discord ID" in security


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
        "**Chat.**",
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
        "DM an @admin. We delete it within 30 days."
    )
    assert discord_points[:-1] == notice_points[:-1]
    assert "Smarter Dev Discord server" not in channel_post(PUBLIC_URL)


def test_the_email_address_is_in_the_full_notice_only():
    assert "admin@smarter.dev" not in " ".join(short_version())
    assert "admin@smarter.dev" not in channel_post(PUBLIC_URL)
    assert "admin@smarter.dev" not in command_response(PUBLIC_URL)
