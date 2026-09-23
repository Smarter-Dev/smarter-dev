"""The Jev replay windows match the live count and timer rules."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from smarter_dev.bot.proactive.windows import jev_batch_windows

T = datetime(2026, 9, 1, tzinfo=UTC)


def test_tenth_message_fires_immediately_and_starts_a_new_batch() -> None:
    timestamps = [T + timedelta(seconds=index) for index in range(11)]

    assert jev_batch_windows(timestamps) == [
        (T, T + timedelta(seconds=9), 10),
        (T + timedelta(seconds=10), T + timedelta(seconds=310), 1),
    ]


def test_partial_batch_uses_quiet_deadline() -> None:
    timestamps = [T, T + timedelta(minutes=2)]

    assert jev_batch_windows(timestamps) == [
        (T, T + timedelta(minutes=7), 2)
    ]


def test_oldest_message_deadline_wins_during_continuous_activity() -> None:
    timestamps = [T + timedelta(minutes=2 * index) for index in range(6)]

    assert jev_batch_windows(timestamps) == [
        (T, T + timedelta(minutes=10), 5),
        (T + timedelta(minutes=10), T + timedelta(minutes=15), 1),
    ]


def test_empty_timeline_makes_no_calls() -> None:
    assert jev_batch_windows([]) == []
