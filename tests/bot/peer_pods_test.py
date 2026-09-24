"""The check that no other bot pod still runs before taking a free lease."""

from __future__ import annotations

import pytest

from smarter_dev.bot import peer_pods


def pod(name: str, state: str | None) -> dict:
    statuses = [{"state": {state: {}}}] if state else []
    return {"metadata": {"name": name}, "status": {"containerStatuses": statuses}}


def test_only_other_pods_with_a_running_container_count() -> None:
    pods = [
        pod("me", "running"),
        pod("old-terminating", "running"),  # deleted but still shutting down
        pod("old-exited", "terminated"),
        pod("new-pending", None),
    ]
    assert peer_pods.running_elsewhere(pods, "me") == ["old-terminating"]
    assert peer_pods.running_elsewhere([pod("me", "running")], "me") == []


async def test_outside_kubernetes_there_is_nobody_to_wait_for(monkeypatch) -> None:
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert await peer_pods.predecessor_gone()


@pytest.fixture
def in_cluster(monkeypatch, tmp_path):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("HOSTNAME", "me")
    monkeypatch.setattr(peer_pods, "SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setattr(peer_pods, "_failing_since", None)


async def test_waits_while_another_bot_pod_runs(in_cluster, monkeypatch) -> None:
    pods = [pod("me", "running"), pod("old", "running")]

    async def listed() -> list[dict]:
        return pods

    monkeypatch.setattr(peer_pods, "list_bot_pods", listed)
    assert not await peer_pods.predecessor_gone()
    pods[1] = pod("old", "terminated")
    assert await peer_pods.predecessor_gone()


async def test_an_unreachable_api_waits_then_gives_up(in_cluster, monkeypatch) -> None:
    async def failing() -> list[dict]:
        raise OSError("forbidden")

    monkeypatch.setattr(peer_pods, "list_bot_pods", failing)
    assert not await peer_pods.predecessor_gone()
    monkeypatch.setattr(peer_pods, "GIVE_UP_AFTER_SECONDS", -1.0)
    assert await peer_pods.predecessor_gone()
