"""scripts/delete_duplicate_burst_warn.py (#376): only the burst's second
warning goes, after every check; any mismatch changes nothing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationIncident
from smarter_dev.web.models import ModerationOperation
from smarter_dev.web.moderation_gateway.gateway import default_session_factory


def sessions():
    return default_session_factory()()


@pytest.fixture
def script(monkeypatch):
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "delete_duplicate_burst_warn.py"
    )
    spec = importlib.util.spec_from_file_location("delete_duplicate_warn", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_db_session_context", default_session_factory())
    return module


async def _burst(script, **duplicate):
    """The two incidents, the duplicate's operation, the two warnings, and
    an unrelated delete on the second incident."""
    async with sessions() as session:
        for incident_id, message in (
            (script.KEPT_INCIDENT, "1563916899799269537"),
            (script.DUPLICATE_INCIDENT, "1563916900130623598"),
        ):
            session.add(
                ModerationIncident(
                    id=incident_id,
                    guild_id=script.GUILD,
                    subject_user_id=script.MEMBER,
                    trigger_message_id=message,
                    active_until=0.0,
                )
            )
        session.add(
            ModerationOperation(
                id=script.DUPLICATE_OPERATION,
                incident_id=script.DUPLICATE_INCIDENT,
                guild_id=script.GUILD,
                target_user_id=script.MEMBER,
                kind="warn",
                idempotency_key=f"warn:{script.DUPLICATE_INCIDENT}:{script.MEMBER}",
                state="confirmed",
                actor="coordinator",
            )
        )
        await session.flush()

        def warn(**over):
            values = {
                "guild_id": script.GUILD,
                "target_user_id": script.MEMBER,
                "target_username": "d1sc0n3ct",
                "action_type": "warn",
                "reason": "confirmed scam",
                "source": "ai",
                "actor": "coordinator",
            }
            values.update(over)
            return ModerationAction(**values)

        session.add_all(
            [
                warn(id=script.KEPT, incident_id=script.KEPT_INCIDENT),
                warn(
                    **{
                        "id": script.DUPLICATE,
                        "incident_id": script.DUPLICATE_INCIDENT,
                        "operation_id": script.DUPLICATE_OPERATION,
                        **duplicate,
                    }
                ),
                warn(incident_id=script.DUPLICATE_INCIDENT, action_type="delete"),
            ]
        )
        await session.commit()


async def _left() -> list[tuple]:
    async with sessions() as session:
        return sorted(
            (str(row.id) == "5a99158f-d032-45b4-8fd9-fac1e464c117", row.action_type)
            for row in (await session.execute(select(ModerationAction))).scalars()
        )


async def _operations() -> int:
    async with sessions() as session:
        return len((await session.execute(select(ModerationOperation))).all())


ALL = [(False, "delete"), (False, "warn"), (True, "warn")]


async def test_check_names_the_row_and_changes_nothing(script, capsys):
    await _burst(script)
    assert await script.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "would delete moderation_actions" in out
    assert '"incident_id": "d37d0554-fbe7-4426-9788-61caec587193"' in out
    assert await _left() == ALL


async def test_delete_removes_only_the_duplicate_and_logs_it_first(script, capsys):
    await _burst(script)
    assert await script.main(["delete"]) == 0
    out = capsys.readouterr().out
    logged = out.index("deleting moderation_actions {")
    assert '"reason": "confirmed scam"' in out[logged:]
    assert await _left() == [(False, "delete"), (False, "warn")]
    assert await _operations() == 1  # 9aa5fc55 stays
    assert await script.main(["delete"]) == 0
    assert "nothing to delete" in capsys.readouterr().out


@pytest.mark.parametrize(
    "over",
    [
        {"action_type": "timeout"},
        {"target_user_id": "1"},
        {"guild_id": "1"},
        {"incident_id": "kept"},
        {"operation_id": None},
    ],
    ids=["not_a_warn", "other_member", "other_guild", "other_incident", "no_op"],
)
async def test_a_row_that_does_not_match_is_refused(script, over, capsys):
    if over.get("incident_id") == "kept":
        over = {"incident_id": script.KEPT_INCIDENT}
    await _burst(script, **over)
    assert await script.main(["delete"]) == 2
    assert "deleting" not in capsys.readouterr().out
    assert len(await _left()) == 3


async def test_refused_when_the_kept_warning_is_gone(script):
    await _burst(script)
    async with sessions() as session:
        await session.delete(await session.get(ModerationAction, script.KEPT))
        await session.commit()
    assert await script.main(["delete"]) == 2
    assert await _left() == [(False, "delete"), (True, "warn")]
