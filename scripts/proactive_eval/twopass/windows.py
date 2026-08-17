"""Burst-based activation windows for the two-pass bot.

Active ingest: the watcher fires once a burst of messages goes quiet for
``quiet_seconds``, but no later than ``max_wait_seconds`` after the burst's
first message. Passive watch: a tick every ``passive_seconds`` during quiet
stretches (in replay these ticks are always empty — active ingest has
already consumed every message — but they are recorded so the run shows the
real wake schedule).
"""

from __future__ import annotations

from datetime import datetime, timedelta

QUIET_SECONDS = 15
MAX_WAIT_SECONDS = 60
PASSIVE_SECONDS = 900


def burst_windows(
    timestamps: list[datetime],
    *,
    quiet_seconds: int = QUIET_SECONDS,
    max_wait_seconds: int = MAX_WAIT_SECONDS,
) -> list[tuple[datetime, datetime]]:
    """(first message, fire time) spans for each message burst."""
    quiet = timedelta(seconds=quiet_seconds)
    max_wait = timedelta(seconds=max_wait_seconds)
    spans = []
    first = last = timestamps[0]
    for timestamp in timestamps[1:]:
        fire_at = min(last + quiet, first + max_wait)
        if timestamp >= fire_at:
            spans.append((first, fire_at))
            first = last = timestamp
        else:
            last = timestamp
    spans.append((first, min(last + quiet, first + max_wait)))
    return spans


def two_pass_windows(
    timestamps: list[datetime],
    *,
    quiet_seconds: int = QUIET_SECONDS,
    max_wait_seconds: int = MAX_WAIT_SECONDS,
    passive_seconds: int = PASSIVE_SECONDS,
) -> list[tuple[datetime, datetime]]:
    """Burst windows plus passive ticks in the quiet gaps between them.

    Passive ticks stop strictly before the next burst's first message, so a
    tick can never steal messages from the burst that owns them.
    """
    bursts = burst_windows(
        timestamps,
        quiet_seconds=quiet_seconds,
        max_wait_seconds=max_wait_seconds,
    )
    passive = timedelta(seconds=passive_seconds)
    windows = []
    for index, (first, fire_at) in enumerate(bursts):
        windows.append((first, fire_at))
        if index + 1 >= len(bursts):
            break
        next_first = bursts[index + 1][0]
        previous_end = fire_at
        tick = fire_at + passive
        while tick < next_first:
            windows.append((previous_end, tick))
            previous_end = tick
            tick += passive
    return windows
