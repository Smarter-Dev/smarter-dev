"""Tests for the bot-core spam engine (docs/v2/feature-parity §4.1 Handler B).

The engine is split into a pure layer (content analysis, rolling buffer, the six
metrics, the escalation decision) and a thin Discord/DB shell. Everything below
the shell is driven by an injected clock so the window arithmetic is exact
rather than sleep-dependent.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import hikari
import pytest

from smarter_dev.bot import spam_engine
from smarter_dev.web.models import DEFAULT_SCAM_LINK_DOMAINS
from smarter_dev.web.models import ModerationFilterConfig

GUILD_ID = 100
CHANNEL_ID = 200
MESSAGE_ID = 300
AUTHOR_ID = 400
STAFF_ROLE_ID = 500
MOD_PING_ROLE_ID = "600"
MOD_ALERT_CHANNEL_ID = "700"
SCAM_LOG_CHANNEL_ID = "800"

NOW = 1_000_000.0


def _thresholds(**overrides) -> spam_engine.SpamThresholds:
    return spam_engine.SpamThresholds.from_config(
        ModerationFilterConfig(guild_id=str(GUILD_ID), **overrides)
    )


def _entry(
    timestamp: float,
    *,
    author_id: str = str(AUTHOR_ID),
    channel_id: str = str(CHANNEL_ID),
    content: str = "hello",
    scam_link_domains: tuple[str, ...] = DEFAULT_SCAM_LINK_DOMAINS,
) -> spam_engine.BufferedMessage:
    return spam_engine.build_buffered_message(
        timestamp=timestamp,
        author_id=author_id,
        channel_id=channel_id,
        content=content,
        scam_link_domains=scam_link_domains,
    )


# ---------------------------------------------------------------------------
# Content analysis
# ---------------------------------------------------------------------------


def _analyze(content: str, scam_link_domains=DEFAULT_SCAM_LINK_DOMAINS):
    return spam_engine.analyze_message_content(content, scam_link_domains)


@pytest.mark.parametrize("content", ["hey @everyone look", "@here please", "@everyone"])
def test_mass_mention_tokens_are_detected(content):
    assert _analyze(content).contains_mass_mention is True


def test_plain_user_mention_is_not_a_mass_mention():
    analysis = _analyze("hey <@1234> look at this")
    assert analysis.contains_mass_mention is False


@pytest.mark.parametrize(
    "content",
    ["free NITRO for everyone", "steam gift for you", "big giveaway happening"],
)
def test_nitro_keywords_are_detected_case_insensitively(content):
    assert _analyze(content).contains_nitro_keyword is True


def test_ordinary_message_has_no_nitro_keyword():
    assert _analyze("what time is standup").contains_nitro_keyword is False


@pytest.mark.parametrize(
    "content",
    [
        "claim here https://dlscord.gift/abc123",
        "https://d1scord.gift/x",
        "http://www.dlscord.gift/x",
        "join https://t.me/somechannel",
        "dm me on https://chat.whatsapp.com/AbCd",
    ],
)
def test_scam_links_are_detected(content):
    assert _analyze(content).scam_links


@pytest.mark.parametrize(
    "content",
    [
        "here is a real one https://discord.gift/abc123",
        "https://www.discord.gift/abc123",
        "check https://discord.com/channels/1/2/3",
        "no links at all here",
    ],
)
def test_legitimate_links_are_not_flagged_as_scams(content):
    assert _analyze(content).scam_links == ()


# ---------------------------------------------------------------------------
# Configurable scam-link domain list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://t.me/channel", True),
        ("https://x.t.me/channel", True),  # subdomain of a listed entry
        ("https://T.ME/CHANNEL", True),  # host comparison is case-insensitive
        ("https://nott.me/channel", False),  # the dot boundary is required
        ("https://tt.me/channel", False),
        ("https://example.com/t.me", False),  # only the host is matched
    ],
)
def test_configured_domains_match_the_host_and_its_subdomains(link, expected):
    assert spam_engine.is_scam_link(link, ("t.me",)) is expected


def test_scam_domain_list_comes_from_config_not_from_code():
    assert spam_engine.is_scam_link("https://lure.example/x", ("lure.example",)) is True
    assert spam_engine.is_scam_link("https://t.me/x", ("lure.example",)) is False


def test_empty_domain_list_still_flags_gift_lookalikes():
    """An empty list disables the domain rule, never the hardcoded pattern rule."""
    assert spam_engine.is_scam_link("https://dlscord.gift/x", ()) is True
    assert spam_engine.is_scam_link("https://t.me/x", ()) is False


def test_legitimate_gift_domain_survives_the_domain_list():
    assert spam_engine.is_scam_link("https://discord.gift/x", ("discord.gift",)) is False


def test_sanitize_link_defangs_scheme_and_dots():
    sanitized = spam_engine.sanitize_link("https://dlscord.gift/abc")

    assert "https://" not in sanitized
    assert "dlscord.gift" not in sanitized
    assert "dlscord[.]gift" in sanitized


def test_sanitize_content_truncates_and_neutralizes_mass_mentions():
    content = "@everyone " + ("x" * 2000)

    sanitized = spam_engine.sanitize_content(content, max_length=900)

    assert len(sanitized) <= 900
    assert "@everyone" not in sanitized


# ---------------------------------------------------------------------------
# Metric 1 — message rate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message_count", "expected"),
    [(4, False), (5, False), (6, True)],
)
def test_message_rate_fires_only_above_threshold(message_count, expected):
    thresholds = _thresholds(message_rate_threshold=5, message_rate_window_seconds=5)
    entries = [_entry(NOW - 4 + index * 0.1) for index in range(message_count)]

    assert spam_engine.exceeds_message_rate(entries, NOW, thresholds) is expected


def test_message_rate_ignores_entries_older_than_the_window():
    thresholds = _thresholds(message_rate_threshold=5, message_rate_window_seconds=5)
    old = [_entry(NOW - 30 - index) for index in range(10)]
    recent = [_entry(NOW - 1 + index * 0.1) for index in range(5)]

    assert spam_engine.exceeds_message_rate(old + recent, NOW, thresholds) is False


def test_message_rate_ignores_entries_from_before_the_last_warning():
    """The burst that earned the warning must not also earn the mute."""
    thresholds = _thresholds(message_rate_threshold=5, message_rate_window_seconds=5)
    burst = [_entry(NOW - 4 + index * 0.1) for index in range(6)]
    follow_up = [_entry(NOW)]

    assert spam_engine.exceeds_message_rate(burst + follow_up, NOW, thresholds) is True
    assert (
        spam_engine.exceeds_message_rate(
            burst + follow_up, NOW, thresholds, last_warned_at=NOW - 3.4
        )
        is False
    )


def test_message_rate_fires_again_on_a_burst_newer_than_the_warning():
    thresholds = _thresholds(message_rate_threshold=5, message_rate_window_seconds=5)
    warned_burst = [_entry(NOW - 20 + index * 0.1) for index in range(6)]
    new_burst = [_entry(NOW - 4 + index * 0.1) for index in range(6)]

    assert (
        spam_engine.exceeds_message_rate(
            warned_burst + new_burst, NOW, thresholds, last_warned_at=NOW - 19.4
        )
        is True
    )


# ---------------------------------------------------------------------------
# Metric 2 — channel spread
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel_count", "expected"),
    [(2, False), (3, False), (4, True)],
)
def test_channel_spread_fires_only_above_threshold(channel_count, expected):
    thresholds = _thresholds(channel_spread_threshold=3, channel_spread_window_seconds=15)
    entries = [
        _entry(NOW - 10 + index, channel_id=f"channel-{index}")
        for index in range(channel_count)
    ]

    assert spam_engine.exceeds_channel_spread(entries, NOW, thresholds) is expected


def test_channel_spread_ignores_channels_outside_the_window():
    thresholds = _thresholds(channel_spread_threshold=3, channel_spread_window_seconds=15)
    entries = [
        _entry(NOW - 100, channel_id="old-a"),
        _entry(NOW - 90, channel_id="old-b"),
        _entry(NOW - 3, channel_id="new-a"),
        _entry(NOW - 2, channel_id="new-b"),
        _entry(NOW - 1, channel_id="new-c"),
    ]

    assert spam_engine.exceeds_channel_spread(entries, NOW, thresholds) is False


def test_channel_spread_ignores_channels_from_before_the_last_warning():
    """The spread that earned the warning must not also earn the mute."""
    thresholds = _thresholds(channel_spread_threshold=3, channel_spread_window_seconds=15)
    entries = [
        _entry(NOW - 10, channel_id="a"),
        _entry(NOW - 9, channel_id="b"),
        _entry(NOW - 8, channel_id="c"),
        _entry(NOW - 7, channel_id="d"),
        _entry(NOW, channel_id="a"),
    ]

    assert spam_engine.exceeds_channel_spread(entries, NOW, thresholds) is True
    assert (
        spam_engine.exceeds_channel_spread(entries, NOW, thresholds, last_warned_at=NOW - 7)
        is False
    )


def test_channel_spread_fires_again_on_channels_newer_than_the_warning():
    thresholds = _thresholds(channel_spread_threshold=3, channel_spread_window_seconds=15)
    entries = [
        _entry(NOW - 10, channel_id="a"),
        _entry(NOW - 3, channel_id="b"),
        _entry(NOW - 2, channel_id="c"),
        _entry(NOW - 1, channel_id="d"),
        _entry(NOW, channel_id="e"),
    ]

    assert (
        spam_engine.exceeds_channel_spread(entries, NOW, thresholds, last_warned_at=NOW - 10)
        is True
    )


# ---------------------------------------------------------------------------
# Metric 3 — duplicate messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("copies", "expected"),
    [(1, False), (2, True), (3, True)],
)
def test_duplicate_message_fires_on_a_repeat(copies, expected):
    thresholds = _thresholds(duplicate_message_min_length=15, duplicate_message_window_seconds=60)
    content = "buy my product now"  # longer than the 15-character minimum
    entries = [_entry(NOW - 10 + index, content=content) for index in range(copies)]

    assert spam_engine.has_duplicate_message(entries, NOW, thresholds) is expected


@pytest.mark.parametrize(
    ("content_length", "expected"),
    [(14, False), (15, False), (16, True)],
)
def test_duplicate_message_only_counts_content_longer_than_the_minimum(content_length, expected):
    thresholds = _thresholds(duplicate_message_min_length=15, duplicate_message_window_seconds=60)
    content = "y" * content_length
    entries = [_entry(NOW - 5, content=content), _entry(NOW - 1, content=content)]

    assert spam_engine.has_duplicate_message(entries, NOW, thresholds) is expected


def test_duplicate_message_ignores_repeats_older_than_the_window():
    thresholds = _thresholds(duplicate_message_min_length=15, duplicate_message_window_seconds=60)
    content = "buy my product now"
    entries = [_entry(NOW - 300, content=content), _entry(NOW - 1, content=content)]

    assert spam_engine.has_duplicate_message(entries, NOW, thresholds) is False


def test_duplicate_message_ignores_repeats_from_before_the_last_warning():
    """A repeat already punished by a warning must not re-trigger the metric."""
    thresholds = _thresholds(duplicate_message_min_length=15, duplicate_message_window_seconds=60)
    content = "buy my product now"
    entries = [_entry(NOW - 50, content=content), _entry(NOW - 1, content=content)]

    assert spam_engine.has_duplicate_message(entries, NOW, thresholds, last_warned_at=NOW - 20) is False
    assert spam_engine.has_duplicate_message(entries, NOW, thresholds, last_warned_at=NOW - 55) is True


# ---------------------------------------------------------------------------
# Metric 4 — mass mention
# ---------------------------------------------------------------------------


def test_mass_mention_fires_inside_the_window_only():
    thresholds = _thresholds(mass_mention_window_seconds=15)
    inside = [_entry(NOW - 14, content="@everyone free stuff")]
    outside = [_entry(NOW - 16, content="@everyone free stuff")]

    assert spam_engine.has_mass_mention(inside, NOW, thresholds) is True
    assert spam_engine.has_mass_mention(outside, NOW, thresholds) is False


def test_mass_mention_does_not_fire_without_a_mention():
    thresholds = _thresholds(mass_mention_window_seconds=15)

    assert spam_engine.has_mass_mention([_entry(NOW, content="hello all")], NOW, thresholds) is False


def test_mass_mention_ignores_mentions_from_before_the_last_warning():
    """The ping that earned the warning must not keep firing for 15 seconds."""
    thresholds = _thresholds(mass_mention_window_seconds=15)
    entries = [
        _entry(NOW - 3, content="@everyone free stuff"),
        _entry(NOW, content="anyway, what time is standup"),
    ]

    assert spam_engine.has_mass_mention(entries, NOW, thresholds) is True
    assert (
        spam_engine.has_mass_mention(entries, NOW, thresholds, last_warned_at=NOW - 3) is False
    )


def test_mass_mention_fires_again_on_a_ping_newer_than_the_warning():
    thresholds = _thresholds(mass_mention_window_seconds=15)
    entries = [
        _entry(NOW - 3, content="@everyone free stuff"),
        _entry(NOW, content="@everyone seriously look"),
    ]

    assert spam_engine.has_mass_mention(entries, NOW, thresholds, last_warned_at=NOW - 3) is True


def test_current_message_mass_mention_reads_the_latest_entry_only():
    """The DELETE decision is about the message in hand, not the window."""
    pinged_now = [_entry(NOW - 3, content="hello"), _entry(NOW, content="@everyone hi")]
    pinged_earlier = [_entry(NOW - 3, content="@everyone hi"), _entry(NOW, content="hello")]

    assert spam_engine.current_message_has_mass_mention(pinged_now) is True
    assert spam_engine.current_message_has_mass_mention(pinged_earlier) is False
    assert spam_engine.current_message_has_mass_mention([]) is False


# ---------------------------------------------------------------------------
# Metric 5 — nitro scam
# ---------------------------------------------------------------------------


def test_nitro_scam_requires_both_a_mass_mention_and_a_keyword():
    thresholds = _thresholds(mass_mention_window_seconds=15)
    mention_only = [_entry(NOW, content="@everyone hello")]
    keyword_only = [_entry(NOW, content="free nitro for all")]
    both = [_entry(NOW, content="@everyone free nitro")]

    assert spam_engine.has_nitro_scam(mention_only, NOW, thresholds) is False
    assert spam_engine.has_nitro_scam(keyword_only, NOW, thresholds) is False
    assert spam_engine.has_nitro_scam(both, NOW, thresholds) is True


def test_nitro_scam_catches_a_mention_and_keyword_split_across_messages():
    thresholds = _thresholds(mass_mention_window_seconds=15)
    entries = [
        _entry(NOW - 5, content="@everyone"),
        _entry(NOW - 1, content="claim your free nitro here"),
    ]

    assert spam_engine.has_nitro_scam(entries, NOW, thresholds) is True


def test_nitro_scam_deliberately_correlates_across_a_warning():
    """Metric 5 is the one metric NOT clamped to the last warning.

    A lone ``@everyone`` earns a warning on its own, so clamping metric 5 would
    permanently blind it to the exact pattern it exists for: the ping and the
    bait posted as two separate messages.
    """
    thresholds = _thresholds(mass_mention_window_seconds=15)
    entries = [
        _entry(NOW - 3, content="@everyone"),
        _entry(NOW, content="claim your free nitro here"),
    ]

    assert spam_engine.has_nitro_scam(entries, NOW, thresholds) is True


# ---------------------------------------------------------------------------
# Metric 6 — scam link
# ---------------------------------------------------------------------------


def test_scam_link_metric_reads_the_latest_message():
    scam = [_entry(NOW - 5, content="hello"), _entry(NOW, content="https://dlscord.gift/x")]
    clean = [_entry(NOW - 5, content="https://dlscord.gift/x"), _entry(NOW, content="hello")]

    assert spam_engine.has_scam_link(scam) is True
    assert spam_engine.has_scam_link(clean) is False
    assert spam_engine.has_scam_link([]) is False


def test_discord_gift_is_not_a_scam_link():
    entries = [_entry(NOW, content="here you go https://discord.gift/realcode")]

    assert spam_engine.has_scam_link(entries) is False


# ---------------------------------------------------------------------------
# Escalation decision
# ---------------------------------------------------------------------------


def _metrics(**overrides) -> spam_engine.SpamMetrics:
    fields = {
        "message_rate": False,
        "channel_spread": False,
        "duplicate_message": False,
        "mass_mention": False,
        "nitro_scam": False,
        "scam_link": False,
        "current_message_mass_mention": False,
        "current_message_nitro_keyword": False,
    }
    fields.update(overrides)
    return spam_engine.SpamMetrics(**fields)


def test_no_metric_means_no_action():
    decision = spam_engine.decide_spam_response(_metrics(), warned_recently=False)

    assert decision.action == spam_engine.SPAM_ACTION_NONE
    assert decision.should_delete_message is False


def test_first_violation_warns():
    decision = spam_engine.decide_spam_response(_metrics(message_rate=True), warned_recently=False)

    assert decision.action == spam_engine.SPAM_ACTION_WARN
    assert decision.reasons
    assert decision.should_delete_message is False


def test_violation_after_a_recent_warning_escalates_to_mute():
    decision = spam_engine.decide_spam_response(_metrics(message_rate=True), warned_recently=True)

    assert decision.action == spam_engine.SPAM_ACTION_MUTE


def test_nitro_scam_mutes_without_a_prior_warning():
    decision = spam_engine.decide_spam_response(
        _metrics(mass_mention=True, nitro_scam=True, current_message_mass_mention=True),
        warned_recently=False,
    )

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert decision.should_delete_message is True


def test_nitro_scam_deletes_the_bait_half_of_a_split_scam():
    decision = spam_engine.decide_spam_response(
        _metrics(mass_mention=True, nitro_scam=True, current_message_nitro_keyword=True),
        warned_recently=False,
    )

    assert decision.should_delete_message is True


def test_nitro_scam_does_not_delete_a_message_that_carries_neither_half():
    decision = spam_engine.decide_spam_response(
        _metrics(mass_mention=True, nitro_scam=True), warned_recently=False
    )

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert decision.should_delete_message is False


def test_scam_link_mutes_and_deletes_without_a_prior_warning():
    decision = spam_engine.decide_spam_response(_metrics(scam_link=True), warned_recently=False)

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert decision.should_delete_message is True


def test_mass_mention_alone_warns_but_still_deletes():
    decision = spam_engine.decide_spam_response(
        _metrics(mass_mention=True, current_message_mass_mention=True), warned_recently=False
    )

    assert decision.action == spam_engine.SPAM_ACTION_WARN
    assert decision.should_delete_message is True


def test_window_scoped_mass_mention_does_not_delete_an_innocent_current_message():
    """Deleting on the strength of an OLDER message deletes the wrong message."""
    decision = spam_engine.decide_spam_response(
        _metrics(mass_mention=True, message_rate=True), warned_recently=False
    )

    assert decision.action == spam_engine.SPAM_ACTION_WARN
    assert decision.should_delete_message is False


# ---------------------------------------------------------------------------
# Rolling buffer
# ---------------------------------------------------------------------------


def test_buffer_prunes_entries_older_than_the_retention_window():
    buffer = spam_engine.SpamMessageBuffer(retention_seconds=60)
    buffer.record(_entry(NOW - 300))
    buffer.record(_entry(NOW - 120))
    buffer.record(_entry(NOW))

    assert buffer.total_entries() == 1
    assert buffer.entries_for_author(str(AUTHOR_ID))[0].timestamp == NOW


def test_buffer_caps_entries_per_author_under_a_flood():
    buffer = spam_engine.SpamMessageBuffer(
        retention_seconds=300, max_entries_per_author=32, max_tracked_authors=64
    )

    for index in range(500):
        buffer.record(_entry(NOW + index * 0.01, content=f"flood {index}"))

    assert buffer.total_entries() == 32
    assert buffer.tracked_author_count() == 1


def test_buffer_caps_the_number_of_tracked_authors_under_a_raid():
    buffer = spam_engine.SpamMessageBuffer(
        retention_seconds=300, max_entries_per_author=32, max_tracked_authors=64
    )

    for index in range(500):
        buffer.record(_entry(NOW + index * 0.01, author_id=f"author-{index}"))

    assert buffer.tracked_author_count() == 64
    assert buffer.total_entries() <= 64 * 32


def test_buffer_eviction_keeps_the_most_recently_active_authors():
    buffer = spam_engine.SpamMessageBuffer(
        retention_seconds=300, max_entries_per_author=8, max_tracked_authors=2
    )
    buffer.record(_entry(NOW, author_id="oldest"))
    buffer.record(_entry(NOW + 1, author_id="middle"))
    buffer.record(_entry(NOW + 2, author_id="newest"))

    assert buffer.entries_for_author("oldest") == ()
    assert buffer.entries_for_author("middle")
    assert buffer.entries_for_author("newest")


def test_buffer_stays_bounded_across_a_long_running_mixed_load():
    buffer = spam_engine.SpamMessageBuffer(
        retention_seconds=60, max_entries_per_author=32, max_tracked_authors=64
    )

    for index in range(600):
        buffer.record(
            _entry(
                NOW + index,  # one message per simulated second
                author_id=f"author-{index % 90}",
                content=f"message {index}",
            )
        )

    assert buffer.total_entries() <= 64 * 32
    assert buffer.tracked_author_count() <= 64
    newest = NOW + 599
    assert all(
        entry.timestamp > newest - 60
        for author_entries in buffer.snapshot().values()
        for entry in author_entries
    )


# ---------------------------------------------------------------------------
# Buffer bounds derived from config
# ---------------------------------------------------------------------------


def test_buffer_retention_covers_the_longest_configured_window():
    """A 1800s duplicate window is useless if the buffer forgets after 300s."""
    bounds = spam_engine.derive_buffer_bounds(
        _thresholds(duplicate_message_window_seconds=1800)
    )

    assert bounds.retention_seconds >= 1800


def test_buffer_holds_enough_entries_to_reach_the_rate_threshold():
    """A threshold of 80 can never fire from a buffer that holds 64 entries."""
    bounds = spam_engine.derive_buffer_bounds(_thresholds(message_rate_threshold=80))

    assert bounds.max_entries_per_author > 80


def test_buffer_bounds_clamp_a_pathological_config_and_say_so(caplog):
    thresholds = _thresholds(
        duplicate_message_window_seconds=10_000_000, message_rate_threshold=10_000_000
    )

    with caplog.at_level("WARNING", logger=spam_engine.logger.name):
        bounds = spam_engine.derive_buffer_bounds(thresholds)

    assert bounds.retention_seconds == spam_engine.ABSOLUTE_MAX_RETENTION_SECONDS
    assert bounds.max_entries_per_author == spam_engine.ABSOLUTE_MAX_ENTRIES_PER_AUTHOR
    assert "duplicate_message_window_seconds" in caplog.text
    assert "message_rate_threshold" in caplog.text


def test_buffer_adopts_new_bounds_without_losing_recent_entries():
    buffer = spam_engine.SpamMessageBuffer(
        retention_seconds=60, max_entries_per_author=2, max_tracked_authors=8
    )
    buffer.record(_entry(NOW, content="one"))
    buffer.record(_entry(NOW + 1, content="two"))

    buffer.apply_bounds(
        spam_engine.SpamBufferBounds(
            retention_seconds=600, max_entries_per_author=8, max_tracked_authors=8
        )
    )
    buffer.record(_entry(NOW + 2, content="three"))

    assert len(buffer.entries_for_author(str(AUTHOR_ID))) == 3


# ---------------------------------------------------------------------------
# Guild state
# ---------------------------------------------------------------------------


def test_guild_state_tracks_warnings_within_the_reoffense_window():
    thresholds = _thresholds(warning_reoffense_window_seconds=120)
    state = spam_engine.GuildSpamState()
    state.record_warning(str(AUTHOR_ID), NOW)

    assert state.was_warned_recently(str(AUTHOR_ID), NOW + 119, thresholds) is True
    assert state.was_warned_recently(str(AUTHOR_ID), NOW + 121, thresholds) is False


def test_guild_state_mute_expires():
    state = spam_engine.GuildSpamState()
    state.record_mute(str(AUTHOR_ID), muted_until=NOW + 100)

    assert state.is_muted(str(AUTHOR_ID), NOW + 50) is True
    assert state.is_muted(str(AUTHOR_ID), NOW + 150) is False


def test_guild_state_prune_drops_expired_warnings_and_mutes():
    thresholds = _thresholds(warning_reoffense_window_seconds=120)
    state = spam_engine.GuildSpamState()
    state.record_warning("warned", NOW)
    state.record_mute("muted", muted_until=NOW + 10)

    state.prune(NOW + 1000, thresholds)

    assert state.last_warned_at("warned") is None
    assert state.is_muted("muted", NOW + 1000) is False


# ---------------------------------------------------------------------------
# Author exemption
# ---------------------------------------------------------------------------


def test_bot_authors_are_exempt():
    assert spam_engine.is_exempt_author(
        is_bot=True, has_manage_messages=False, author_role_ids=(), staff_exempt_role_ids=()
    ) is True


def test_manage_messages_holders_are_exempt():
    assert spam_engine.is_exempt_author(
        is_bot=False, has_manage_messages=True, author_role_ids=(), staff_exempt_role_ids=()
    ) is True


def test_configured_staff_roles_are_exempt():
    assert spam_engine.is_exempt_author(
        is_bot=False,
        has_manage_messages=False,
        author_role_ids=("1", "2"),
        staff_exempt_role_ids=("2", "3"),
    ) is True


def test_ordinary_members_are_not_exempt():
    assert spam_engine.is_exempt_author(
        is_bot=False,
        has_manage_messages=False,
        author_role_ids=("1",),
        staff_exempt_role_ids=("2",),
    ) is False


# ---------------------------------------------------------------------------
# Listener shell
# ---------------------------------------------------------------------------


class _FakeRest:
    def __init__(self, *, delete_raises: Exception | None = None) -> None:
        self.created_messages: list[dict] = []
        self.edited_members: list[dict] = []
        self.deleted_messages: list[tuple] = []
        self.delete_raises = delete_raises
        self.create_message_raises: Exception | None = None
        self.edit_member_raises: Exception | None = None

    async def create_message(self, channel, content, **kwargs):
        if self.create_message_raises is not None:
            raise self.create_message_raises
        self.created_messages.append({"channel": str(channel), "content": content, **kwargs})
        return SimpleNamespace(id=999)

    async def edit_member(self, guild, user, **kwargs):
        if self.edit_member_raises is not None:
            raise self.edit_member_raises
        self.edited_members.append({"guild": str(guild), "user": str(user), **kwargs})

    async def delete_message(self, channel, message, **kwargs):
        if self.delete_raises is not None:
            raise self.delete_raises
        self.deleted_messages.append((str(channel), str(message)))


class _FakeBot:
    def __init__(self, rest: _FakeRest) -> None:
        self.rest = rest

    def get_me(self):
        return SimpleNamespace(id=1, username="smarter-dev")


class _FakeGuild:
    def __init__(self, role_permissions: dict[int, hikari.Permissions], owner_id: int = 0) -> None:
        self._role_permissions = role_permissions
        self.owner_id = owner_id

    def get_role(self, role_id):
        permissions = self._role_permissions.get(int(role_id))
        if permissions is None:
            return None
        return SimpleNamespace(id=role_id, permissions=permissions)


def _event(
    content: str,
    *,
    channel_id: int = CHANNEL_ID,
    message_id: int = MESSAGE_ID,
    author_id: int = AUTHOR_ID,
    is_bot: bool = False,
    role_ids: tuple[int, ...] = (),
    role_permissions: dict[int, hikari.Permissions] | None = None,
):
    guild = _FakeGuild(role_permissions or {})
    member = SimpleNamespace(id=author_id, role_ids=list(role_ids))
    return SimpleNamespace(
        guild_id=GUILD_ID,
        channel_id=channel_id,
        message_id=message_id,
        content=content,
        author=SimpleNamespace(id=author_id, username="spammer", is_bot=is_bot),
        member=member,
        get_guild=lambda: guild,
    )


@pytest.fixture
def engine_harness(monkeypatch):
    """Patches the config read, the audit write, and the mod_action dispatch."""
    config = ModerationFilterConfig(
        guild_id=str(GUILD_ID),
        spam_engine_enabled=True,
        staff_exempt_role_ids=[str(STAFF_ROLE_ID)],
        mod_alert_channel_id=MOD_ALERT_CHANNEL_ID,
        mod_ping_role_id=MOD_PING_ROLE_ID,
        scam_log_channel_id=SCAM_LOG_CHANNEL_ID,
    )
    recorded_actions: list[dict] = []
    dispatched: list = []

    async def fake_get_config(session, guild_id):
        return harness.config

    async def fake_create_action(session, **kwargs):
        recorded_actions.append(kwargs)
        return SimpleNamespace(**kwargs)

    async def fake_dispatch(action) -> None:
        dispatched.append(action)

    async def _noop(*args, **kwargs) -> None:
        return None

    @contextlib.asynccontextmanager
    async def fake_session_context():
        yield SimpleNamespace(commit=_noop, add=lambda *a, **k: None, flush=_noop)

    monkeypatch.setattr(
        spam_engine.moderation_filter_config_ops, "get_config", fake_get_config
    )
    monkeypatch.setattr(spam_engine.mod_action_ops, "create_action", fake_create_action)
    monkeypatch.setattr(spam_engine, "dispatch_mod_action", fake_dispatch)
    monkeypatch.setattr(spam_engine, "get_db_session_context", fake_session_context)

    spam_engine.reset_spam_states()
    rest = _FakeRest()
    harness = SimpleNamespace(
        config=config,
        bot=_FakeBot(rest),
        rest=rest,
        state=spam_engine.GuildSpamState(),
        recorded_actions=recorded_actions,
        dispatched=dispatched,
    )
    return harness


async def _flood(harness, count: int, *, start: float = NOW, content: str = "spam") -> spam_engine.SpamDecision:
    decision = spam_engine.NO_SPAM_ACTION
    for index in range(count):
        decision = await spam_engine.check_spam_engine(
            harness.bot,
            _event(f"{content} {index}", message_id=MESSAGE_ID + index),
            state=harness.state,
            now=start + index * 0.1,
        )
    return decision


async def test_disabled_engine_takes_no_action(engine_harness):
    engine_harness.config.spam_engine_enabled = False

    decision = await _flood(engine_harness, 10)

    assert decision.action == spam_engine.SPAM_ACTION_NONE
    assert engine_harness.rest.created_messages == []


async def test_missing_config_takes_no_action(engine_harness, monkeypatch):
    async def no_config(session, guild_id):
        return None

    monkeypatch.setattr(spam_engine.moderation_filter_config_ops, "get_config", no_config)

    decision = await _flood(engine_harness, 10)

    assert decision.action == spam_engine.SPAM_ACTION_NONE


async def test_bot_authors_are_skipped(engine_harness):
    for index in range(10):
        decision = await spam_engine.check_spam_engine(
            engine_harness.bot,
            _event(f"spam {index}", is_bot=True),
            state=engine_harness.state,
            now=NOW + index * 0.1,
        )

    assert decision.action == spam_engine.SPAM_ACTION_NONE
    assert engine_harness.rest.created_messages == []


async def test_manage_messages_holders_are_skipped(engine_harness):
    role_permissions = {STAFF_ROLE_ID + 1: hikari.Permissions.MANAGE_MESSAGES}
    for index in range(10):
        decision = await spam_engine.check_spam_engine(
            engine_harness.bot,
            _event(
                f"spam {index}",
                role_ids=(STAFF_ROLE_ID + 1,),
                role_permissions=role_permissions,
            ),
            state=engine_harness.state,
            now=NOW + index * 0.1,
        )

    assert decision.action == spam_engine.SPAM_ACTION_NONE
    assert engine_harness.rest.created_messages == []


async def test_configured_staff_role_is_skipped(engine_harness):
    for index in range(10):
        decision = await spam_engine.check_spam_engine(
            engine_harness.bot,
            _event(f"spam {index}", role_ids=(STAFF_ROLE_ID,)),
            state=engine_harness.state,
            now=NOW + index * 0.1,
        )

    assert decision.action == spam_engine.SPAM_ACTION_NONE
    assert engine_harness.rest.created_messages == []


async def test_message_rate_violation_warns_in_channel(engine_harness):
    decision = await _flood(engine_harness, 6)

    assert decision.action == spam_engine.SPAM_ACTION_WARN
    assert len(engine_harness.rest.created_messages) == 1
    warning = engine_harness.rest.created_messages[0]
    assert warning["channel"] == str(CHANNEL_ID)
    assert "Please stop" in warning["content"]
    assert warning["mentions_everyone"] is False
    assert engine_harness.rest.edited_members == []


async def test_reoffense_within_the_warning_window_escalates_to_mute(engine_harness):
    warn_decision = await _flood(engine_harness, 6)
    assert warn_decision.action == spam_engine.SPAM_ACTION_WARN

    mute_decision = await _flood(engine_harness, 6, start=NOW + 60, content="more spam")

    assert mute_decision.action == spam_engine.SPAM_ACTION_MUTE
    assert len(engine_harness.rest.edited_members) == 1
    timeout = engine_harness.rest.edited_members[0]
    assert timeout["user"] == str(AUTHOR_ID)
    assert timeout["communication_disabled_until"] is not None


async def test_reoffense_after_the_warning_window_warns_again(engine_harness):
    await _flood(engine_harness, 6)

    decision = await _flood(engine_harness, 6, start=NOW + 500, content="later spam")

    assert decision.action == spam_engine.SPAM_ACTION_WARN
    assert engine_harness.rest.edited_members == []


async def test_nitro_scam_mutes_immediately_and_records_the_action(engine_harness):
    decision = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone claim your free nitro now"),
        state=engine_harness.state,
        now=NOW,
    )

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert len(engine_harness.rest.edited_members) == 1
    assert len(engine_harness.recorded_actions) == 1
    action = engine_harness.recorded_actions[0]
    assert action["action_type"] == "timeout"
    assert action["source"] == "handler"
    assert action["reason"] == spam_engine.AUTO_MODERATION_REASON
    assert action["target_user_id"] == str(AUTHOR_ID)
    assert action["duration_seconds"] == engine_harness.config.mute_duration_seconds
    assert action["channel_id"] == str(CHANNEL_ID)
    assert action["trigger_message_id"] == str(MESSAGE_ID)
    assert len(engine_harness.dispatched) == 1


async def test_mute_alert_includes_jump_link_and_sanitized_content(engine_harness):
    await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone claim your free nitro now"),
        state=engine_harness.state,
        now=NOW,
    )

    alerts = [
        message
        for message in engine_harness.rest.created_messages
        if message["channel"] == MOD_ALERT_CHANNEL_ID
    ]
    assert len(alerts) == 1
    alert = alerts[0]
    assert f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/{MESSAGE_ID}" in alert["content"]
    assert f"<@&{MOD_PING_ROLE_ID}>" in alert["content"]
    assert "@everyone" not in alert["content"]


async def test_mute_alert_cannot_ping_everyone(engine_harness):
    await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone free nitro https://dlscord.gift/x"),
        state=engine_harness.state,
        now=NOW,
    )

    alerts = [
        message
        for message in engine_harness.rest.created_messages
        if message["channel"] == MOD_ALERT_CHANNEL_ID
    ]
    alert = alerts[0]
    assert alert["mentions_everyone"] is False
    assert alert["user_mentions"] is False
    assert alert["role_mentions"] == [hikari.Snowflake(MOD_PING_ROLE_ID)]


async def test_mute_alert_pings_nothing_when_no_role_is_configured(engine_harness):
    engine_harness.config.mod_ping_role_id = None

    await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone free nitro"),
        state=engine_harness.state,
        now=NOW,
    )

    alert = [
        message
        for message in engine_harness.rest.created_messages
        if message["channel"] == MOD_ALERT_CHANNEL_ID
    ][0]
    assert alert["role_mentions"] is False
    assert alert["mentions_everyone"] is False


async def test_scam_link_deletes_the_message_and_posts_to_the_scam_log(engine_harness):
    decision = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("claim it https://dlscord.gift/abc123"),
        state=engine_harness.state,
        now=NOW,
    )

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert decision.should_delete_message is True
    assert engine_harness.rest.deleted_messages == [(str(CHANNEL_ID), str(MESSAGE_ID))]

    scam_logs = [
        message
        for message in engine_harness.rest.created_messages
        if message["channel"] == SCAM_LOG_CHANNEL_ID
    ]
    assert len(scam_logs) == 1
    assert "dlscord[.]gift" in scam_logs[0]["content"]
    assert "https://dlscord.gift" not in scam_logs[0]["content"]


async def test_discord_gift_link_is_not_moderated(engine_harness):
    decision = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("a real gift for you https://discord.gift/abc123"),
        state=engine_harness.state,
        now=NOW,
    )

    assert decision.action == spam_engine.SPAM_ACTION_NONE
    assert engine_harness.rest.deleted_messages == []
    assert engine_harness.rest.created_messages == []


async def test_already_deleted_message_does_not_abort_the_escalation(engine_harness):
    engine_harness.rest.delete_raises = hikari.NotFoundError(
        url="url", headers={}, raw_body=b"", message="already deleted"
    )

    decision = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("claim it https://dlscord.gift/abc123"),
        state=engine_harness.state,
        now=NOW,
    )

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert len(engine_harness.rest.edited_members) == 1
    assert any(
        message["channel"] == SCAM_LOG_CHANNEL_ID
        for message in engine_harness.rest.created_messages
    )


async def test_muting_state_suppresses_further_action(engine_harness):
    await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone free nitro"),
        state=engine_harness.state,
        now=NOW,
    )
    timeouts_after_first = len(engine_harness.rest.edited_members)

    for index in range(10):
        decision = await spam_engine.check_spam_engine(
            engine_harness.bot,
            _event("@everyone free nitro again", message_id=MESSAGE_ID + index + 1),
            state=engine_harness.state,
            now=NOW + 1 + index,
        )

    assert timeouts_after_first == 1
    assert len(engine_harness.rest.edited_members) == 1
    assert decision.action == spam_engine.SPAM_ACTION_NONE
    assert len(engine_harness.recorded_actions) == 1


async def test_muting_state_stops_suppressing_once_it_expires(engine_harness):
    engine_harness.config.mute_duration_seconds = 30

    await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone free nitro"),
        state=engine_harness.state,
        now=NOW,
    )
    decision = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone free nitro again", message_id=MESSAGE_ID + 1),
        state=engine_harness.state,
        now=NOW + 31,
    )

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert len(engine_harness.rest.edited_members) == 2


async def test_innocent_message_after_a_mass_mention_warning_is_left_alone(engine_harness):
    """The warn step must be actionable: stopping has to work.

    ``@everyone`` at t=0 earns a warning and a delete. An unrelated message at
    t=3 must not be muted or deleted on the strength of that same ping.
    """
    warned = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone check this out"),
        state=engine_harness.state,
        now=NOW,
    )
    assert warned.action == spam_engine.SPAM_ACTION_WARN
    assert engine_harness.rest.deleted_messages == [(str(CHANNEL_ID), str(MESSAGE_ID))]

    follow_up = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("sorry about that, what time is standup", message_id=MESSAGE_ID + 1),
        state=engine_harness.state,
        now=NOW + 3,
    )

    assert follow_up.action == spam_engine.SPAM_ACTION_NONE
    assert engine_harness.rest.edited_members == []
    assert engine_harness.rest.deleted_messages == [(str(CHANNEL_ID), str(MESSAGE_ID))]


async def test_repeat_mass_mention_after_a_warning_still_mutes(engine_harness):
    """New evidence after the warning must still escalate."""
    await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone check this out"),
        state=engine_harness.state,
        now=NOW,
    )

    reoffense = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone seriously look here", message_id=MESSAGE_ID + 1),
        state=engine_harness.state,
        now=NOW + 3,
    )

    assert reoffense.action == spam_engine.SPAM_ACTION_MUTE
    assert len(engine_harness.rest.edited_members) == 1
    assert engine_harness.rest.deleted_messages == [
        (str(CHANNEL_ID), str(MESSAGE_ID)),
        (str(CHANNEL_ID), str(MESSAGE_ID + 1)),
    ]


async def test_quiet_period_after_a_rate_warning_does_not_escalate(engine_harness):
    """The burst that earned the warning must not also earn the mute."""
    warned = await _flood(engine_harness, 6)
    assert warned.action == spam_engine.SPAM_ACTION_WARN

    follow_up = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("ok I will stop", message_id=MESSAGE_ID + 99),
        state=engine_harness.state,
        now=NOW + 1.0,
    )

    assert follow_up.action == spam_engine.SPAM_ACTION_NONE
    assert engine_harness.rest.edited_members == []


async def test_long_duplicate_window_survives_buffer_retention(engine_harness):
    """A 1800s duplicate window must actually see a repeat 400s later."""
    engine_harness.config.duplicate_message_window_seconds = 1800
    repeated = "please buy my crypto course"

    await spam_engine.check_spam_engine(
        engine_harness.bot, _event(repeated), now=NOW
    )
    decision = await spam_engine.check_spam_engine(
        engine_harness.bot, _event(repeated, message_id=MESSAGE_ID + 1), now=NOW + 400
    )

    assert decision.action == spam_engine.SPAM_ACTION_WARN
    assert spam_engine.SPAM_REASON_DUPLICATE in decision.reasons


async def test_high_message_rate_threshold_is_reachable(engine_harness):
    """A threshold of 80 must be able to fire, not be capped by the buffer."""
    engine_harness.config.message_rate_threshold = 80
    engine_harness.config.message_rate_window_seconds = 20
    engine_harness.config.duplicate_message_min_length = 500

    decision = spam_engine.NO_SPAM_ACTION
    for index in range(81):
        decision = await spam_engine.check_spam_engine(
            engine_harness.bot,
            _event(f"chatter {index}", message_id=MESSAGE_ID + index),
            now=NOW + index * 0.1,
        )

    assert decision.action == spam_engine.SPAM_ACTION_WARN
    assert spam_engine.SPAM_REASON_MESSAGE_RATE in decision.reasons


async def test_rejected_timeout_still_deletes_the_scam_link(engine_harness):
    """A BadRequestError on the timeout must not disable enforcement."""
    engine_harness.rest.edit_member_raises = hikari.BadRequestError(
        url="url", headers={}, raw_body=b"", message="communication_disabled_until too far"
    )

    decision = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("claim it https://dlscord.gift/abc123"),
        state=engine_harness.state,
        now=NOW,
    )

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert engine_harness.rest.deleted_messages == [(str(CHANNEL_ID), str(MESSAGE_ID))]
    assert engine_harness.recorded_actions == []
    assert any(
        message["channel"] == MOD_ALERT_CHANNEL_ID
        for message in engine_harness.rest.created_messages
    )


async def test_configured_scam_domain_is_moderated(engine_harness):
    engine_harness.config.scam_link_domains = ["lure.example"]

    decision = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("dm me at https://go.lure.example/x"),
        state=engine_harness.state,
        now=NOW,
    )

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert spam_engine.SPAM_REASON_SCAM_LINK in decision.reasons
    assert engine_harness.rest.deleted_messages == [(str(CHANNEL_ID), str(MESSAGE_ID))]


async def test_domain_removed_from_the_config_is_no_longer_moderated(engine_harness):
    engine_harness.config.scam_link_domains = []

    decision = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("join https://t.me/somechannel"),
        state=engine_harness.state,
        now=NOW,
    )

    assert decision.action == spam_engine.SPAM_ACTION_NONE
    assert engine_harness.rest.deleted_messages == []


async def test_channel_spread_violation_warns(engine_harness):
    for index in range(4):
        decision = await spam_engine.check_spam_engine(
            engine_harness.bot,
            _event(f"hello there {index}", channel_id=CHANNEL_ID + index),
            state=engine_harness.state,
            now=NOW + index,
        )

    assert decision.action == spam_engine.SPAM_ACTION_WARN
    assert spam_engine.SPAM_REASON_CHANNEL_SPREAD in decision.reasons


async def test_duplicate_message_violation_warns(engine_harness):
    repeated = "please buy my crypto course"
    for index in range(2):
        decision = await spam_engine.check_spam_engine(
            engine_harness.bot,
            _event(repeated, message_id=MESSAGE_ID + index),
            state=engine_harness.state,
            now=NOW + index * 10,
        )

    assert decision.action == spam_engine.SPAM_ACTION_WARN
    assert spam_engine.SPAM_REASON_DUPLICATE in decision.reasons


async def test_engine_buffer_stays_bounded_under_sustained_load(engine_harness):
    # Thresholds far out of reach: this measures buffering, not escalation.
    engine_harness.config.message_rate_threshold = 10_000
    engine_harness.config.channel_spread_threshold = 10_000
    engine_harness.config.duplicate_message_min_length = 10_000
    engine_harness.config.mass_mention_window_seconds = 0
    state = spam_engine.GuildSpamState(
        buffer=spam_engine.SpamMessageBuffer(
            retention_seconds=60, max_entries_per_author=32, max_tracked_authors=32
        )
    )

    for index in range(600):
        await spam_engine.check_spam_engine(
            engine_harness.bot,
            _event(f"message {index}", message_id=MESSAGE_ID + index),
            state=state,
            now=NOW + index * 0.05,
        )

    assert state.buffer.total_entries() <= 32 * 32


async def test_refused_timeout_records_no_audit_row_but_still_alerts(engine_harness):
    """An action that did not land must not produce an audit row."""
    engine_harness.rest.edit_member_raises = hikari.ForbiddenError(
        url="url", headers={}, raw_body=b"", message="missing MODERATE_MEMBERS"
    )

    decision = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone free nitro"),
        state=engine_harness.state,
        now=NOW,
    )

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert engine_harness.recorded_actions == []
    assert engine_harness.dispatched == []
    assert any(
        message["channel"] == MOD_ALERT_CHANNEL_ID
        for message in engine_harness.rest.created_messages
    )


async def test_refused_timeout_does_not_arm_the_re_entrancy_guard(engine_harness):
    """A mute Discord refused must not silence the engine for the mute duration.

    ``record_mute`` used to run unconditionally, so an author Discord never muted
    was skipped by the re-entrancy guard for the full 24h — every later scam link
    from them left standing.
    """
    engine_harness.rest.edit_member_raises = hikari.ForbiddenError(
        url="url", headers={}, raw_body=b"", message="missing MODERATE_MEMBERS"
    )

    first = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("claim it https://dlscord.gift/abc123"),
        state=engine_harness.state,
        now=NOW,
    )
    second = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("claim it https://dlscord.gift/def456", message_id=MESSAGE_ID + 1),
        state=engine_harness.state,
        now=NOW + 5,
    )

    assert first.action == spam_engine.SPAM_ACTION_MUTE
    assert second.action == spam_engine.SPAM_ACTION_MUTE
    assert second.should_delete_message is True
    assert len(engine_harness.rest.deleted_messages) == 2


async def test_applied_timeout_still_arms_the_re_entrancy_guard(engine_harness):
    """The guard must keep working when the timeout actually lands."""
    first = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("claim it https://dlscord.gift/abc123"),
        state=engine_harness.state,
        now=NOW,
    )
    second = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("claim it https://dlscord.gift/def456", message_id=MESSAGE_ID + 1),
        state=engine_harness.state,
        now=NOW + 5,
    )

    assert first.action == spam_engine.SPAM_ACTION_MUTE
    assert second.action == spam_engine.SPAM_ACTION_NONE
    assert len(engine_harness.rest.edited_members) == 1


async def test_mute_without_a_configured_alert_channel_still_times_out(engine_harness):
    engine_harness.config.mod_alert_channel_id = None

    decision = await spam_engine.check_spam_engine(
        engine_harness.bot,
        _event("@everyone free nitro"),
        state=engine_harness.state,
        now=NOW,
    )

    assert decision.action == spam_engine.SPAM_ACTION_MUTE
    assert len(engine_harness.rest.edited_members) == 1
    assert engine_harness.rest.created_messages == []


async def test_missing_send_permission_does_not_break_the_warn_path(engine_harness):
    engine_harness.rest.create_message_raises = hikari.ForbiddenError(
        url="url", headers={}, raw_body=b"", message="cannot send"
    )

    decision = await _flood(engine_harness, 6)

    assert decision.action == spam_engine.SPAM_ACTION_WARN
    assert engine_harness.rest.created_messages == []


def test_uncached_guild_or_member_resolves_to_no_permissions():
    """Fail closed on exemption: unknown members get scanned, never skipped."""
    member = SimpleNamespace(id=AUTHOR_ID, role_ids=[])

    assert spam_engine.resolve_member_permissions(None, member) == hikari.Permissions.NONE
    assert spam_engine.resolve_member_permissions(_FakeGuild({}), None) == hikari.Permissions.NONE


def test_guild_owner_resolves_to_administrator():
    guild = _FakeGuild({}, owner_id=AUTHOR_ID)
    member = SimpleNamespace(id=AUTHOR_ID, role_ids=[])

    permissions = spam_engine.resolve_member_permissions(guild, member)

    assert spam_engine.has_moderator_permissions(permissions) is True


def test_single_label_host_is_its_own_registrable_domain():
    assert spam_engine.registrable_domain("localhost") == "localhost"


def test_guild_state_registry_returns_one_state_per_guild():
    spam_engine.reset_spam_states()
    thresholds = _thresholds()
    first = spam_engine.get_guild_spam_state("1", thresholds, NOW)
    second = spam_engine.get_guild_spam_state("1", thresholds, NOW)
    other = spam_engine.get_guild_spam_state("2", thresholds, NOW)

    assert first is second
    assert first is not other

    spam_engine.reset_spam_states()
    assert spam_engine.get_guild_spam_state("1", thresholds, NOW) is not first


def test_registry_state_follows_the_current_config():
    """A config edit must re-bound the buffer of an already-running guild."""
    spam_engine.reset_spam_states()
    state = spam_engine.get_guild_spam_state("1", _thresholds(), NOW)

    widened = _thresholds(duplicate_message_window_seconds=1800)
    same_state = spam_engine.get_guild_spam_state("1", widened, NOW)

    assert same_state is state
    assert state.buffer.bounds == spam_engine.derive_buffer_bounds(widened)


def test_idle_guild_state_is_released():
    """A guild that goes quiet after a raid must not retain its buffer forever."""
    spam_engine.reset_spam_states()
    thresholds = _thresholds()
    raided = spam_engine.get_guild_spam_state("raided", thresholds, NOW)
    raided.record_message(_entry(NOW, content="@everyone free nitro"))
    raided.prune(NOW, thresholds)

    long_after = NOW + spam_engine.ABSOLUTE_MAX_RETENTION_SECONDS + 1
    spam_engine.get_guild_spam_state("someone-else", thresholds, long_after)

    assert spam_engine.tracked_guild_count() == 1
    assert spam_engine.get_guild_spam_state("raided", thresholds, long_after) is not raided


def test_active_guild_state_survives_the_idle_sweep():
    spam_engine.reset_spam_states()
    thresholds = _thresholds()
    busy = spam_engine.get_guild_spam_state("busy", thresholds, NOW)
    busy.record_message(_entry(NOW, content="hello there"))
    busy.prune(NOW, thresholds)

    assert spam_engine.get_guild_spam_state("busy", thresholds, NOW + 30) is busy


def test_leaving_a_guild_releases_its_state():
    spam_engine.reset_spam_states()
    thresholds = _thresholds()
    spam_engine.get_guild_spam_state("departed", thresholds, NOW)

    assert spam_engine.release_guild_spam_state("departed") is True
    assert spam_engine.tracked_guild_count() == 0
    assert spam_engine.release_guild_spam_state("departed") is False
