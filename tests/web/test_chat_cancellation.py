from __future__ import annotations

import json
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from smarter_dev.web.chat import runtime
from smarter_dev.web.chat.cancellation import cancellation_channel
from smarter_dev.web.chat.cancellation import parse_notice


def test_cancellation_notice_targets_turn_child_and_worker_lease():
    turn_id = uuid4()
    child_id = uuid4()
    notice = parse_notice(
        json.dumps(
            {
                "reason": "superseded",
                "subagent_id": str(child_id),
                "worker_lease_token": "old-lease",
            }
        )
    )

    assert cancellation_channel(turn_id).endswith(str(turn_id))
    assert notice is not None
    assert notice.reason == "superseded"
    assert notice.subagent_id == child_id
    assert notice.worker_lease_token == "old-lease"


def test_invalid_cancellation_notice_is_ignored():
    assert parse_notice("not-json") is None
    assert parse_notice("{}") is None


@pytest.mark.asyncio
async def test_redis_stop_signal_interrupts_running_agent(monkeypatch):
    turn_id = uuid4()

    class FakePubSub:
        async def listen(self):
            yield {
                "type": "message",
                "data": json.dumps({"reason": "stop"}),
            }

    @asynccontextmanager
    async def fake_subscription(_turn_id):
        yield FakePubSub()

    async def durable_state_is_active(**_kwargs):
        return None

    monkeypatch.setattr(runtime, "cancellation_subscription", fake_subscription)
    monkeypatch.setattr(
        runtime, "_check_durable_cancellation_state", durable_state_is_active
    )

    with pytest.raises(runtime.RunCancelled):
        await runtime.wait_for_cancellation(turn_id=turn_id)
