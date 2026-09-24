"""Which other bot pods Kubernetes says exist."""

from __future__ import annotations

import pytest

from smarter_dev.bot import peer_pods


def pod(name: str, phase: str, *, deleting: bool = False) -> dict:
    metadata = {"name": name}
    if deleting:
        metadata["deletionTimestamp"] = "2026-09-24T20:00:00Z"
    return {"metadata": metadata, "status": {"phase": phase}}


def test_every_other_pod_counts_until_it_has_finished() -> None:
    pods = [
        pod("me", "Running"),
        pod("new-pending", "Pending"),  # may start any moment
        pod("old-running", "Running"),
        pod("old-terminating", "Running", deleting=True),  # still shutting down
        pod("old-unknown", "Unknown"),  # node lost: cannot rule it out
        pod("old-succeeded", "Succeeded"),
        pod("old-failed", "Failed"),
    ]
    assert peer_pods.present_elsewhere(pods, "me") == [
        "new-pending",
        "old-running",
        "old-terminating",
        "old-unknown",
    ]
    assert peer_pods.present_elsewhere([pod("me", "Running")], "me") == []


async def test_outside_kubernetes_there_are_none(monkeypatch) -> None:
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert await peer_pods.other_bot_pods() == []


@pytest.fixture
def in_cluster(monkeypatch, tmp_path):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("HOSTNAME", "me")
    monkeypatch.setattr(peer_pods, "SERVICE_ACCOUNT", tmp_path)


async def test_lists_the_other_pods(in_cluster, monkeypatch) -> None:
    pods = [pod("me", "Running"), pod("old", "Running")]

    async def listed() -> list[dict]:
        return pods

    monkeypatch.setattr(peer_pods, "list_bot_pods", listed)
    assert await peer_pods.other_bot_pods() == ["old"]
    pods[1] = pod("old", "Succeeded")
    assert await peer_pods.other_bot_pods() == []


async def test_an_api_failure_is_an_error_however_long_it_lasts(in_cluster, monkeypatch) -> None:
    """Never an answer: nothing turns a run of failures into "none left"."""

    async def failing() -> list[dict]:
        raise OSError("forbidden")

    monkeypatch.setattr(peer_pods, "list_bot_pods", failing)
    for _ in range(3):
        with pytest.raises(OSError):
            await peer_pods.other_bot_pods()
    assert not hasattr(peer_pods, "GIVE_UP_AFTER_SECONDS")
