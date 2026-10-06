"""Skrift's queued notifications are in-flight data (task #71).

The Resources progress (restated question, research steps, title) is sent as
queued notifications, so their stored lifetime in ``app.yaml`` must match the
in-flight sweep window in ``retention_policy``. The number lives in two files;
this pins them together.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from skrift.config import NotificationsConfig

from smarter_dev.shared.retention_policy import IN_FLIGHT_MAX
from smarter_dev.shared.retention_policy import IN_FLIGHT_SWEEP_WINDOW

REPO = Path(__file__).resolve().parents[2]
SKRIFT_SWEEP_INTERVAL_SECONDS = 600


@pytest.fixture(params=["app.yaml", "app.development.yaml"])
def notifications(request) -> NotificationsConfig:
    config = yaml.safe_load((REPO / request.param).read_text())
    return NotificationsConfig(**config["notifications"])


def test_queued_notifications_live_for_the_in_flight_sweep_window(notifications):
    assert notifications.queued_ttl_seconds == IN_FLIGHT_SWEEP_WINDOW.total_seconds()


def test_the_sweep_lag_still_ends_inside_the_in_flight_limit(notifications):
    worst = notifications.queued_ttl_seconds + SKRIFT_SWEEP_INTERVAL_SECONDS
    assert worst < IN_FLIGHT_MAX.total_seconds()


def test_timeseries_notifications_keep_skrifts_default(notifications):
    """Nothing here sends a timeseries notification, so none is stored."""
    assert (
        notifications.timeseries_ttl_seconds
        == NotificationsConfig().timeseries_ttl_seconds
    )
