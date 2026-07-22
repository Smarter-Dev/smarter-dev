"""Tests for the disboard-bumping catalog extension.

Two layers, per the house test plan:

1. Rendering/lint — the manifest renders against its example_config and every
   rendered script passes ``handler_lint`` (the registry/startup gate), with the
   role-grant allowlist closure and channel scope asserted.
2. Behavior — each rendered script is executed through ``run_handler_script``
   with a stubbed emitter/actor (the offline runtime-harness pattern) to prove the
   detector, cleanliness, king rotation + tie-break, the schedule_timer reminder
   re-fire, and the !bumpers / !bumps command paths.

Only this extension's own manifest/scripts are imported and exercised directly —
the whole-catalog registry scan is left to the final verifier (siblings are
mid-edit).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import pytest

from smarter_dev.extensions.catalog.disboard_bumping import MANIFEST
from smarter_dev.extensions.rendering import RenderError
from smarter_dev.extensions.rendering import extract_granted_role_literals
from smarter_dev.extensions.rendering import render_bundle
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_runtime import run_handler_script

_DISBOARD_ID = "302050872383242240"
_BUMP_CHANNEL = "111111111111111111"
_CROWN_ROLE = "222222222222222222"
_COMMANDS_CHANNEL = "333333333333333333"
_PING_ROLE = "444444444444444444"
_ANNOUNCEMENT_CHANNEL = "555555555555555555"

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / (
    "smarter_dev/extensions/catalog/disboard_bumping"
)


def _scripts() -> dict[str, str]:
    return {
        handler.key: (_PACKAGE_DIR / handler.script_file).read_text()
        for handler in MANIFEST.handlers
    }


def _rendered(config: dict | None = None) -> dict:
    bundle = render_bundle(MANIFEST, config or MANIFEST.example_config, _scripts())
    return {item.key: item for item in bundle}


# -- fakes ---------------------------------------------------------------------


@dataclass
class _Emitter:
    # (channel, content, ping_role_id) for every create_message.
    message_calls: list = field(default_factory=list)
    messages: list = field(default_factory=list)

    async def create_message(
        self, channel_id, content, ping_role_id=None, tolerate_missing_target=False
    ):
        self.message_calls.append((channel_id, content, ping_role_id))
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"

    async def add_reaction(self, channel_id, message_id, emoji):
        pass

    async def get_thread_parent_id(self, thread_id):
        return None

    async def get_channel_guild_id(self, channel_id):
        return "G1"


@dataclass
class _Actor:
    calls: list = field(default_factory=list)

    async def delete_message(self, channel_id, message_id):
        self.calls.append(("delete", channel_id, message_id))
        return "ok"

    async def add_role(self, user_id, role_id, reason=None):
        self.calls.append(("add_role", user_id, role_id))
        return True

    async def remove_role(self, user_id, role_id, reason=None):
        self.calls.append(("remove_role", user_id, role_id))
        return True


@dataclass
class _Limiter:
    async def hit(self, key, limit, window_seconds=None):
        return True


@dataclass
class _TimerRecorder:
    calls: list = field(default_factory=list)

    async def __call__(self, fire_at, refire_context):
        self.calls.append((fire_at, refire_context))


def _apply(state: dict, result) -> dict:
    """Fold a fire's guild-memory changes back into a running store (as the real
    caller persists them) so fires can be chained."""
    for key, value in result.guild_memory_writes.items():
        state[key] = value
    for key in result.guild_memory_deletes:
        state.pop(key, None)
    return state


async def _run_tracker(context, *, guild_memory=None, config=None, ping=True):
    """Execute the rendered bump-tracker against a context; return the pieces."""
    rendered = _rendered(config)["bump-tracker"]
    emitter = _Emitter()
    actor = _Actor()
    timer = _TimerRecorder()
    guild_memory = dict(guild_memory or {})
    result = await run_handler_script(
        rendered.script,
        context,
        channel_id=_BUMP_CHANNEL,
        guild_id="G1",
        emitter=emitter,
        limiter=_Limiter(),
        budget=admin_budget(),
        actor=actor,
        channel_ids=rendered.channel_ids,
        allowed_role_ids=rendered.settings.get("allowed_role_ids", []),
        guild_memory=guild_memory,
        timer_scheduler=timer,
        handler_id="H-tracker",
    )
    return result, emitter, actor, timer


async def _run_commands(text, *, guild_memory=None):
    rendered = _rendered()["bump-commands"]
    emitter = _Emitter()
    result = await run_handler_script(
        rendered.script,
        {"trigger_type": "message", "message_content": text, "author_id": "U9"},
        channel_id=_COMMANDS_CHANNEL,
        guild_id="G1",
        emitter=emitter,
        limiter=_Limiter(),
        budget=admin_budget(),
        actor=_Actor(),
        channel_ids=rendered.channel_ids,
        guild_memory=dict(guild_memory or {}),
    )
    return result, emitter


# -- layer 1: rendering + lint -------------------------------------------------


def test_manifest_renders_and_every_script_lints_clean():
    rendered = _rendered()
    assert set(rendered) == {"bump-tracker", "bump-commands"}
    for item in rendered.values():
        assert lint_script(item.script) is None


def test_tracker_channel_scope_and_bot_optin_and_allowlist():
    tracker = _rendered()["bump-tracker"]
    assert tracker.trigger_type == "message"
    assert tracker.channel_ids == [_BUMP_CHANNEL]
    assert tracker.settings["include_bot_messages"] is True
    # The crown-role grant/revoke literals are exactly the rendered allowlist.
    assert extract_granted_role_literals(tracker.script) == {_CROWN_ROLE}
    assert tracker.settings["allowed_role_ids"] == [_CROWN_ROLE]


def test_commands_scoped_off_the_bump_channel():
    commands = _rendered()["bump-commands"]
    assert commands.channel_ids == [_COMMANDS_CHANNEL]
    assert commands.channel_ids != [_BUMP_CHANNEL]


def test_reminder_ping_role_is_required_and_typed():
    config = dict(MANIFEST.example_config)
    config["reminder_ping_role_id"] = ""
    with pytest.raises(RenderError, match="must be a Discord id"):
        _rendered(config)


# -- layer 2: tracker detection ------------------------------------------------


def _confirmation(user_id="USERA", message_id="MSG1"):
    return {
        "trigger_type": "message",
        "author_id": _DISBOARD_ID,
        "message_id": message_id,
        "embeds": [{"description": "Bump done! :thumbsup: Check it out"}],
        "interaction_user_id": user_id,
    }


async def test_confirmed_bump_credits_ledger_crowns_and_arms_reminder():
    result, emitter, actor, timer = await _run_tracker(_confirmation())
    assert result.outcome == "ok", result.error
    writes = result.guild_memory_writes
    # Ledger got the credited bumper, flags reset, confirmation remembered.
    assert writes["disboard_bumps"][0][0] == "USERA"
    assert writes["disboard_reminded"] is False
    assert writes["disboard_confirmation_message_id"] == "MSG1"
    assert writes["disboard_last_bump_at"] == writes["disboard_bumps"][0][1]
    # First bump -> crown granted to the new king with an announcement.
    assert ("add_role", "USERA", _CROWN_ROLE) in actor.calls
    assert writes["disboard_king_id"] == "USERA"
    assert any("Bump King" in content for _, content in emitter.messages)
    king_announcement = next(
        message for message in emitter.messages if "Bump King" in message[1]
    )
    assert king_announcement[0] == _ANNOUNCEMENT_CHANNEL
    # No stray deletes (no prior reminder/confirmation), and the reminder armed.
    assert not any(call[0] == "delete" for call in actor.calls)
    assert len(timer.calls) == 1
    _, refire = timer.calls[0]
    assert refire["payload"]["bump_at"] == writes["disboard_last_bump_at"]


async def test_confirmation_rotation_deletes_previous_reminder_and_confirmation():
    seed = {
        "disboard_reminder_message_id": "OLDREM",
        "disboard_confirmation_message_id": "OLDCONF",
    }
    result, emitter, actor, timer = await _run_tracker(
        _confirmation(message_id="NEWCONF"), guild_memory=seed
    )
    assert result.outcome == "ok", result.error
    deletes = [call for call in actor.calls if call[0] == "delete"]
    assert ("delete", _BUMP_CHANNEL, "OLDREM") in deletes
    assert ("delete", _BUMP_CHANNEL, "OLDCONF") in deletes
    assert result.guild_memory_writes["disboard_confirmation_message_id"] == "NEWCONF"
    # The spent reminder pointer is cleared.
    assert result.guild_memory_writes["disboard_reminder_message_id"] is None


async def test_confirmation_rotation_from_empty_state_deletes_nothing():
    result, emitter, actor, timer = await _run_tracker(_confirmation())
    assert result.outcome == "ok", result.error
    assert not any(call[0] == "delete" for call in actor.calls)
    assert result.guild_memory_writes["disboard_confirmation_message_id"] == "MSG1"


async def test_disboard_non_confirmation_is_deleted_without_crediting():
    context = {
        "trigger_type": "message",
        "author_id": _DISBOARD_ID,
        "message_id": "COOLDOWN1",
        "embeds": [{"description": "Please wait 42 minutes before bumping again."}],
        "interaction_user_id": "USERA",
    }
    result, emitter, actor, timer = await _run_tracker(context)
    assert result.outcome == "ok", result.error
    assert actor.calls == [("delete", _BUMP_CHANNEL, "COOLDOWN1")]
    assert result.guild_memory_changed is False
    assert timer.calls == []


async def test_confirmation_without_invoker_is_deleted():
    context = _confirmation()
    context["interaction_user_id"] = None
    result, emitter, actor, timer = await _run_tracker(context)
    assert result.outcome == "ok", result.error
    assert ("delete", _BUMP_CHANNEL, "MSG1") in actor.calls
    assert result.guild_memory_changed is False


async def test_non_disboard_message_is_deleted_for_cleanliness():
    context = {
        "trigger_type": "message",
        "author_id": "555000000000000000",
        "message_id": "SPAM1",
    }
    result, emitter, actor, timer = await _run_tracker(context)
    assert result.outcome == "ok", result.error
    assert actor.calls == [("delete", _BUMP_CHANNEL, "SPAM1")]
    assert result.guild_memory_changed is False
    assert timer.calls == []


# -- layer 2: king rotation + tie-break ----------------------------------------


async def test_king_tie_break_moves_crown_to_more_recent_bumper():
    now = int(time.time())
    seed = {
        "disboard_king_id": "BBB",
        "disboard_bumps": [
            ["AAA", now - 300],
            ["BBB", now - 200],
            ["BBB", now - 100],
        ],
    }
    # AAA bumps now -> AAA and BBB tie at 2, AAA is the more recent -> AAA crowned.
    result, emitter, actor, timer = await _run_tracker(
        _confirmation(user_id="AAA", message_id="TIE1"), guild_memory=seed
    )
    assert result.outcome == "ok", result.error
    assert ("remove_role", "BBB", _CROWN_ROLE) in actor.calls
    assert ("add_role", "AAA", _CROWN_ROLE) in actor.calls
    assert result.guild_memory_writes["disboard_king_id"] == "AAA"


async def test_unchanged_top_bumper_does_not_touch_roles():
    now = int(time.time())
    seed = {
        "disboard_king_id": "AAA",
        "disboard_bumps": [["AAA", now - 50], ["AAA", now - 40]],
    }
    result, emitter, actor, timer = await _run_tracker(
        _confirmation(user_id="AAA", message_id="AGAIN1"), guild_memory=seed
    )
    assert result.outcome == "ok", result.error
    assert not any(call[0] in ("add_role", "remove_role") for call in actor.calls)
    assert not any("Bump King" in content for _, content in emitter.messages)
    # King id is unchanged, so it is never rewritten.
    assert "disboard_king_id" not in result.guild_memory_writes


async def test_ledger_prunes_entries_older_than_seven_days():
    now = int(time.time())
    seed = {
        "disboard_bumps": [["OLD", now - 8 * 86400], ["RECENT", now - 3600]],
    }
    result, emitter, actor, timer = await _run_tracker(
        _confirmation(user_id="NEW", message_id="P1"), guild_memory=seed
    )
    assert result.outcome == "ok", result.error
    stored_ids = [entry[0] for entry in result.guild_memory_writes["disboard_bumps"]]
    assert "OLD" not in stored_ids
    assert stored_ids[0] == "NEW"
    assert "RECENT" in stored_ids


# -- layer 2: reminder timer re-fire -------------------------------------------


async def test_timer_refire_posts_reminder_and_pings_role_when_configured():
    now = int(time.time())
    seed = {"disboard_last_bump_at": now, "disboard_reminded": False}
    context = {
        "trigger_type": "timer",
        "payload": {"bump_at": now},
        "scheduled_at": "2026-07-21T00:00:00+00:00",
    }
    result, emitter, actor, timer = await _run_tracker(context, guild_memory=seed)
    assert result.outcome == "ok", result.error
    # The reminder posted to the bump channel and pinged the configured role.
    assert emitter.message_calls == [
        (_BUMP_CHANNEL, emitter.messages[0][1], _PING_ROLE)
    ]
    assert f"<@&{_PING_ROLE}>" in emitter.messages[0][1]
    assert result.guild_memory_writes["disboard_reminded"] is True
    assert "disboard_reminder_message_id" in result.guild_memory_writes


async def test_stale_timer_does_not_remind_when_a_newer_bump_arrived():
    now = int(time.time())
    # The store's last bump is newer than the timer's armed-for stamp.
    seed = {"disboard_last_bump_at": now, "disboard_reminded": False}
    context = {
        "trigger_type": "timer",
        "payload": {"bump_at": now - 7200},
        "scheduled_at": "2026-07-21T00:00:00+00:00",
    }
    result, emitter, actor, timer = await _run_tracker(context, guild_memory=seed)
    assert result.outcome == "ok", result.error
    assert emitter.messages == []
    assert result.guild_memory_changed is False


async def test_timer_does_not_remind_twice():
    now = int(time.time())
    seed = {"disboard_last_bump_at": now, "disboard_reminded": True}
    context = {
        "trigger_type": "timer",
        "payload": {"bump_at": now},
        "scheduled_at": "2026-07-21T00:00:00+00:00",
    }
    result, emitter, actor, timer = await _run_tracker(context, guild_memory=seed)
    assert result.outcome == "ok", result.error
    assert emitter.messages == []


async def test_full_cycle_bump_then_reminder_then_superseding_bump():
    """Chain fires through a persisted store: bump arms a timer, the timer
    reminds once, a fresh bump resets the flag and supersedes the old timer."""
    state: dict = {}
    r1, _, _, timer1 = await _run_tracker(
        _confirmation(user_id="AAA", message_id="C1"), guild_memory=state
    )
    _apply(state, r1)
    first_bump_at = state["disboard_last_bump_at"]

    # The armed timer fires -> one reminder, flag set.
    _, timer_ctx = timer1.calls[0]
    r2, emitter2, _, _ = await _run_tracker(timer_ctx, guild_memory=state)
    _apply(state, r2)
    assert len(emitter2.messages) == 1
    assert state["disboard_reminded"] is True

    # A second bump resets the reminded flag and arms a fresh timer (the stale
    # older timer is proven a no-op deterministically in the dedicated test).
    r3, _, _, timer3 = await _run_tracker(
        _confirmation(user_id="BBB", message_id="C2"), guild_memory=state
    )
    _apply(state, r3)
    assert state["disboard_reminded"] is False
    assert state["disboard_last_bump_at"] >= first_bump_at
    assert len(timer3.calls) == 1


# -- layer 2: command surface --------------------------------------------------


async def test_bumpers_leaderboard_ranks_recent_counts():
    now = int(time.time())
    ledger = [
        ["AAA", now - 10],
        ["AAA", now - 20],
        ["AAA", now - 30],
        ["BBB", now - 40],
        ["BBB", now - 50],
        ["CCC", now - 60],
    ]
    result, emitter = await _run_commands(
        "!bumpers", guild_memory={"disboard_bumps": ledger}
    )
    assert result.outcome == "ok", result.error
    assert len(emitter.messages) == 1
    body = emitter.messages[0][1]
    assert "Top bumpers" in body
    assert "🥇 <@AAA> — 3 bumps" in body
    assert "🥈 <@BBB> — 2 bumps" in body
    assert "🥉 <@CCC> — 1 bumps" in body


async def test_bumps_lists_recent_with_timestamp_markup():
    now = int(time.time())
    ledger = [["AAA", now - 10], ["BBB", now - 20]]
    result, emitter = await _run_commands(
        "!bumps", guild_memory={"disboard_bumps": ledger}
    )
    assert result.outcome == "ok", result.error
    body = emitter.messages[0][1]
    assert f"<@AAA> — <t:{now - 10}:R>" in body
    assert "Recent bumps" in body


async def test_commands_report_empty_ledger():
    result, emitter = await _run_commands("!bumpers", guild_memory={})
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "No bumps recorded in the last 7 days."


async def test_non_command_message_is_ignored():
    result, emitter = await _run_commands(
        "just chatting", guild_memory={"disboard_bumps": [["AAA", int(time.time())]]}
    )
    assert result.outcome == "ok", result.error
    assert emitter.messages == []
