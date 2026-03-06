"""Tests for stop detection utility."""

import pytest

from smarter_dev.bot.utils.stop_detection import (
    _channel_cooldowns,
    is_channel_on_cooldown,
    is_stop_request,
    random_stop_ack,
    set_channel_cooldown,
)


class TestIsStopRequest:
    """Tests for is_stop_request()."""

    # --- Should detect as stop requests ---

    @pytest.mark.parametrize("content", [
        "stop",
        "Stop",
        "STOP",
        "stop!",
        "stop.",
        "shut up",
        "Shut up",
        "SHUT UP",
        "go away",
        "enough",
        "stfu",
        "STFU",
        "hush",
        "be quiet",
        "no more",
        "leave me alone",
        "knock it off",
        "that's enough",
        "thats enough",
        "i'm done",
        "im done",
    ])
    def test_bare_stop_phrases(self, content: str):
        assert is_stop_request(content) is True

    @pytest.mark.parametrize("content", [
        "<@123456> stop",
        "<@!123456> stop",
        "<@123456> shut up",
        "<@123456> go away",
        "<@123456> enough",
        "<@123456> stfu",
        "<@123456> leave me alone",
        "<@123456> that's enough",
        "  <@123456>  stop  ",
    ])
    def test_stop_with_mention(self, content: str):
        assert is_stop_request(content) is True

    # --- Should NOT detect as stop requests (false positives) ---

    @pytest.mark.parametrize("content", [
        "can't stop laughing",
        "don't stop",
        "never stop learning",
        "won't stop until it works",
        "stop light is red",
        "stop sign ahead",
        "stop by the store",
        "stop and think about it",
        "stop for a moment",
        "stop at the next exit",
        "full stop",
        "bus stop",
        "pit stop",
    ])
    def test_false_positives(self, content: str):
        assert is_stop_request(content) is False

    @pytest.mark.parametrize("content", [
        "how do I stop a process?",
        "please stop the server",
        "can you stop the build",
        "what is a stop loss?",
        "stop using var and use let instead",
        "I want to learn about bus stops",
    ])
    def test_longer_messages_not_stop(self, content: str):
        """Longer messages containing 'stop' are not stop requests."""
        assert is_stop_request(content) is False

    def test_empty_string(self):
        assert is_stop_request("") is False

    def test_only_mention(self):
        assert is_stop_request("<@123456>") is False

    def test_whitespace_only(self):
        assert is_stop_request("   ") is False


class TestRandomStopAck:
    """Tests for random_stop_ack()."""

    def test_active_ack_is_string(self):
        ack = random_stop_ack(had_watchers=True)
        assert isinstance(ack, str)
        assert len(ack) > 0

    def test_idle_ack_is_string(self):
        ack = random_stop_ack(had_watchers=False)
        assert isinstance(ack, str)
        assert len(ack) > 0

    def test_active_acks_differ_from_idle(self):
        """Active and idle ack pools should be distinct."""
        active_acks = {random_stop_ack(had_watchers=True) for _ in range(50)}
        idle_acks = {random_stop_ack(had_watchers=False) for _ in range(50)}
        # The sets should have no overlap (different message pools)
        assert not active_acks & idle_acks

    def test_active_acks_mention_timeout(self):
        """Active ack messages should mention the 5-minute timeout."""
        from smarter_dev.bot.utils.stop_detection import STOP_ACKS_ACTIVE
        for ack in STOP_ACKS_ACTIVE:
            assert "5" in ack, f"Active ack should mention timeout: {ack}"


class TestChannelCooldown:
    """Tests for channel cooldown functions."""

    @pytest.fixture(autouse=True)
    def clear_cooldowns(self):
        """Clear cooldown state before each test."""
        _channel_cooldowns.clear()
        yield
        _channel_cooldowns.clear()

    def test_no_cooldown_by_default(self):
        assert is_channel_on_cooldown(123) is False

    def test_set_cooldown_makes_channel_on_cooldown(self):
        set_channel_cooldown(123)
        assert is_channel_on_cooldown(123) is True

    def test_cooldown_does_not_affect_other_channels(self):
        set_channel_cooldown(123)
        assert is_channel_on_cooldown(456) is False

    def test_expired_cooldown_returns_false(self):
        set_channel_cooldown(123, duration_seconds=0)
        assert is_channel_on_cooldown(123) is False

    def test_expired_cooldown_cleans_up_entry(self):
        set_channel_cooldown(123, duration_seconds=0)
        is_channel_on_cooldown(123)
        assert 123 not in _channel_cooldowns

    def test_custom_duration(self):
        set_channel_cooldown(123, duration_seconds=600)
        assert is_channel_on_cooldown(123) is True

    def test_overwrite_existing_cooldown(self):
        set_channel_cooldown(123, duration_seconds=600)
        set_channel_cooldown(123, duration_seconds=0)
        assert is_channel_on_cooldown(123) is False
