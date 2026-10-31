"""Tests for the pure spam detection layer (docs/v2/feature-parity §4.1 Handler B).

Content analysis, the rolling buffer, the six metrics and the escalation
decision, all driven by an injected clock so the window arithmetic is exact
rather than sleep-dependent. The Discord/DB shell that acted on them was
removed after the #240 cutover; the moderation coordinator acts now.
"""

from __future__ import annotations

import pytest

from smarter_dev.bot import spam_engine
from smarter_dev.shared import moderation_policy as mp
from tests.bot.moderation_coordinator.replay_cases import SCAM_LINK_DOMAINS

DEFAULT_SCAM_LINK_DOMAINS = SCAM_LINK_DOMAINS

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
    """The policy's default spam engine thresholds, with overrides."""
    rule = mp.default_document().rules.spam_engine
    values = {
        name: getattr(rule, name)
        for name in spam_engine.SpamThresholds.__dataclass_fields__
    }
    return spam_engine.SpamThresholds(**{**values, **overrides})


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


def test_single_label_host_is_its_own_registrable_domain():
    assert spam_engine.registrable_domain("localhost") == "localhost"
