"""Tests for the sus catalog extension.

Two layers, matching the house pattern for catalog extensions:

1. Render/lint layer — the manifest renders against its ``example_config``, the
   rendered script passes ``handler_lint`` (the same static rails the registry
   runs at startup), both role ids are baked in as quoted literals, and only the
   *granted* role (sus) is mirrored into the host-enforced ``allowed_role_ids``
   allowlist — the moderator role is a read-only privilege gate and must never
   become grantable.
2. Behaviour layer — the rendered Monty script runs in the real handler runtime
   over every branch: the self-flag, the privileged multi-target flag, the
   non-privileged fallback to self, the re-sus no-extend rule, the timer refire
   that removes the role (including the member-gone silent no-op), ``!list_sus``
   pruning, and the memory-boundedness rail.

The handler is one guild-wide admin ``message`` handler (``channel_scope == []``)
that serves its own timer refires by branching on ``context["trigger_type"]``.
The memory expiry map exists ONLY for ``!list_sus`` and the re-sus check — the
role itself is the ground truth, which is why every moderation surface that
reads the sus role stays accurate regardless of drift.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from smarter_dev.extensions.registry import _load_one
from smarter_dev.extensions.rendering import RenderError
from smarter_dev.extensions.rendering import extract_granted_role_literals
from smarter_dev.extensions.rendering import render_bundle
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_memory import MAX_MEMORY_BYTES
from smarter_dev.web.handler_runtime import run_handler_script
from tests.web.handler_runtime_test import _FakeActor
from tests.web.handler_runtime_test import _FakeEmitter
from tests.web.handler_runtime_test import _StubLimiter
from tests.web.handler_runtime_test import _TimerRecorder

_MODULE = "smarter_dev.extensions.catalog.sus"
_SUS_ROLE = "111111111111111111"
_MODERATOR_ROLE = "222222222222222222"
_HANDLER_KEY = "sus"
_HOME_CHANNEL = "999999999999999999"
_ONE_DAY_SECONDS = 86400
# The key the handler namespaces its expiry map under.
_MEMORY_KEY = "sus_expiry"


def _loaded():
    return _load_one(_MODULE)


def _rendered(config: dict | None = None):
    loaded = _loaded()
    bundle = render_bundle(
        loaded.manifest, config or loaded.manifest.example_config, loaded.scripts
    )
    return {item.key: item for item in bundle}[_HANDLER_KEY]


def _now() -> int:
    return int(dt.datetime.now(dt.UTC).timestamp())


def _message_context(
    content: str,
    *,
    author_id: str = "700000000000000001",
    author_role_ids: list[str] | None = None,
    mentioned_user_ids: list[str] | None = None,
) -> dict:
    return {
        "trigger_type": "message",
        "message_id": "M1",
        "message_content": content,
        "author_id": author_id,
        "author_name": "ada",
        "author_is_bot": False,
        "author_account_created_at": "2021-01-01T00:00:00+00:00",
        "author_joined_at": "2024-01-01T00:00:00+00:00",
        "author_role_ids": author_role_ids or [],
        "author_has_manage_messages": False,
        "mentioned_user_ids": mentioned_user_ids or [],
        "mentioned_role_ids": [],
        "mentions_everyone": False,
        "channel_parent_id": None,
        "attachments": [],
        "embeds": [],
        "interaction_user_id": None,
    }


def _timer_context(user_id: str) -> dict:
    return {
        "trigger_type": "timer",
        "payload": {"user_id": user_id},
        "scheduled_at": "2026-07-24T12:00:00+00:00",
    }


async def _fire(context: dict, *, memory: dict | None = None, gone=None):
    """Run the rendered script once, returning (result, actor, emitter, timers)."""
    handler = _rendered()
    actor = _FakeActor(gone=set(gone or []))
    emitter = _FakeEmitter()
    timers = _TimerRecorder()
    result = await run_handler_script(
        handler.script,
        context,
        channel_id=_HOME_CHANNEL,
        guild_id="G1",
        emitter=emitter,
        limiter=_StubLimiter(),
        budget=admin_budget(),
        actor=actor,
        channel_ids=[],
        allowed_role_ids=handler.settings["allowed_role_ids"],
        timer_scheduler=timers,
        handler_id="H1",
        memory=dict(memory or {}),
    )
    return result, actor, emitter, timers


def _flagged(result) -> dict:
    """The handler's expiry map as it would be persisted back to the row."""
    return result.memory.get(_MEMORY_KEY, {})


# -- manifest / render layer ---------------------------------------------------


def test_manifest_shape():
    manifest = _loaded().manifest
    assert manifest.slug == "sus"
    assert [handler.key for handler in manifest.handlers] == [_HANDLER_KEY]
    handler = manifest.handlers[0]
    assert handler.trigger_type == "message"
    # Guild-wide: an empty channel scope means the command works in any channel,
    # and a message fire is channel-keyed so every reply lands in its own channel.
    assert handler.channel_scope == []
    # !sus is a human command — the bot's own chatter must never trip it.
    assert handler.settings.get("include_bot_messages") is not True


def test_config_declares_both_role_ids():
    fields = {field.name: field for field in _loaded().manifest.config}
    assert set(fields) == {"sus_role_id", "moderator_role_id"}
    assert fields["sus_role_id"].type == "role_id"
    assert fields["moderator_role_id"].type == "role_id"
    assert fields["sus_role_id"].required is True
    assert fields["moderator_role_id"].required is True


def test_example_config_renders_and_lints_clean():
    handler = _rendered()
    assert lint_script(handler.script) is None
    assert handler.channel_ids == []


def test_both_role_ids_are_baked_in_as_literals():
    script = _rendered().script
    assert f'"{_SUS_ROLE}"' in script
    assert f'"{_MODERATOR_ROLE}"' in script


def test_role_ids_come_from_config_and_are_not_hardcoded():
    """Render with ids that differ from the example config.

    Asserting only against ``example_config`` cannot tell a config-driven id
    apart from a hardcoded one — hardcoding the example's own value would pass.
    Distinct ids are what actually certify the ConfigFields are wired.
    """
    other_sus_role = "888888888888888881"
    other_moderator_role = "888888888888888882"

    script = _rendered(
        {"sus_role_id": other_sus_role, "moderator_role_id": other_moderator_role}
    ).script

    assert f'"{other_sus_role}"' in script
    assert f'"{other_moderator_role}"' in script
    assert _SUS_ROLE not in script
    assert _MODERATOR_ROLE not in script


def test_only_the_sus_role_is_grantable():
    handler = _rendered()
    granted = extract_granted_role_literals(handler.script)
    assert granted == {_SUS_ROLE}
    # Closure: every granted literal is covered by the host-owned allowlist...
    assert granted <= set(handler.settings["allowed_role_ids"])
    # ...and the moderator role is a read-only privilege gate, never grantable.
    assert _MODERATOR_ROLE not in handler.settings["allowed_role_ids"]


def test_non_snowflake_role_is_rejected_at_render():
    with pytest.raises(RenderError, match="sus_role_id"):
        _rendered({"sus_role_id": "@sus", "moderator_role_id": _MODERATOR_ROLE})


# -- self-flag (the default target) --------------------------------------------


async def test_bare_sus_flags_the_author_and_arms_a_one_day_timer():
    before = dt.datetime.now(dt.UTC)
    result, actor, emitter, timers = await _fire(_message_context("!sus"))
    after = dt.datetime.now(dt.UTC)
    assert result.outcome == "ok", result.error

    assert [call[:3] for call in actor.calls] == [
        ("add_role", "700000000000000001", _SUS_ROLE)
    ]
    assert len(timers.calls) == 1
    fire_at, refire = timers.calls[0]
    assert refire["trigger_type"] == "timer"
    assert refire["payload"] == {"user_id": "700000000000000001"}
    delay = dt.timedelta(seconds=_ONE_DAY_SECONDS)
    assert before + delay <= fire_at <= after + delay

    # One announcement, in the channel the command was run in.
    assert len(emitter.messages) == 1
    channel, content = emitter.messages[0]
    assert channel == _HOME_CHANNEL
    assert "<@700000000000000001>" in content

    # The expiry map records the flag, roughly a day out.
    expiry = _flagged(result)["700000000000000001"]
    assert _now() + _ONE_DAY_SECONDS - 5 <= expiry <= _now() + _ONE_DAY_SECONDS + 5


async def test_ordinary_message_costs_nothing():
    # The handler is installed guild-wide, so the overwhelming majority of fires
    # are ordinary chatter: they must not emit, mutate, or even dirty memory.
    result, actor, emitter, timers = await _fire(
        _message_context("this thread is a bit sus honestly")
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert emitter.messages == []
    assert timers.calls == []
    assert result.memory_changed is False


async def test_sus_prefix_does_not_match_a_longer_word():
    result, actor, emitter, _ = await _fire(_message_context("!suspend everything"))
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert emitter.messages == []


# -- privileged multi-target ---------------------------------------------------


async def test_moderator_mentions_flag_every_mentioned_member():
    result, actor, emitter, timers = await _fire(
        _message_context(
            "!sus <@800000000000000001> <@800000000000000002>",
            author_role_ids=[_MODERATOR_ROLE],
            mentioned_user_ids=["800000000000000001", "800000000000000002"],
        )
    )
    assert result.outcome == "ok", result.error
    assert [call[1] for call in actor.calls] == [
        "800000000000000001",
        "800000000000000002",
    ]
    assert [refire["payload"]["user_id"] for _, refire in timers.calls] == [
        "800000000000000001",
        "800000000000000002",
    ]
    assert len(emitter.messages) == 2
    # The privileged author flagged others, not themselves.
    assert set(_flagged(result)) == {"800000000000000001", "800000000000000002"}


async def test_moderator_fan_out_is_capped():
    # Explicitly bounded so one command can never burn the per-fire role-change
    # / timer budgets (nor spam the channel) no matter how many mentions it packs.
    mentions = [f"80000000000000000{index}" for index in range(1, 6)]
    result, actor, emitter, timers = await _fire(
        _message_context(
            "!sus " + " ".join(f"<@{uid}>" for uid in mentions),
            author_role_ids=[_MODERATOR_ROLE],
            mentioned_user_ids=mentions,
        )
    )
    assert result.outcome == "ok", result.error
    assert len(actor.calls) == 3
    assert len(timers.calls) == 3
    assert len(emitter.messages) == 3
    assert [call[1] for call in actor.calls] == mentions[:3]


async def test_moderator_without_mentions_flags_themselves():
    # Bug-compatibility with the legacy silent no-op is deliberately dropped: a
    # moderator running a bare !sus self-suses like everyone else.
    result, actor, _, timers = await _fire(
        _message_context("!sus", author_role_ids=[_MODERATOR_ROLE])
    )
    assert result.outcome == "ok", result.error
    assert [call[1] for call in actor.calls] == ["700000000000000001"]
    assert len(timers.calls) == 1


async def test_non_privileged_mentions_fall_back_to_self():
    # Privilege is the configured moderator ROLE, not a Discord permission bit,
    # and a non-privileged user's mentions are ignored rather than obeyed.
    result, actor, emitter, timers = await _fire(
        _message_context(
            "!sus <@800000000000000001>",
            author_role_ids=["333333333333333333"],
            mentioned_user_ids=["800000000000000001"],
        )
    )
    assert result.outcome == "ok", result.error
    assert [call[1] for call in actor.calls] == ["700000000000000001"]
    assert [refire["payload"]["user_id"] for _, refire in timers.calls] == [
        "700000000000000001"
    ]
    assert len(emitter.messages) == 1
    assert "<@700000000000000001>" in emitter.messages[0][1]
    assert set(_flagged(result)) == {"700000000000000001"}


# -- re-sus does not extend the timer ------------------------------------------


async def test_re_sus_skips_the_role_and_the_timer_but_still_replies():
    already = {_MEMORY_KEY: {"700000000000000001": _now() + 3600}}
    result, actor, emitter, timers = await _fire(
        _message_context("!sus"), memory=already
    )
    assert result.outcome == "ok", result.error
    # Neither the grant nor a fresh timer — the original expiry stands.
    assert actor.calls == []
    assert timers.calls == []
    assert result.usage["role_changes"] == 0
    assert result.usage["timers_scheduled"] == 0
    # ...but the joke still lands.
    assert len(emitter.messages) == 1
    assert _flagged(result)["700000000000000001"] == already[_MEMORY_KEY][
        "700000000000000001"
    ]


async def test_expired_flag_can_be_re_sussed():
    # A past-due entry is not "already flagged": it is pruned, and the member is
    # flagged afresh with a full day.
    stale = {_MEMORY_KEY: {"700000000000000001": _now() - 10}}
    result, actor, _, timers = await _fire(_message_context("!sus"), memory=stale)
    assert result.outcome == "ok", result.error
    assert [call[1] for call in actor.calls] == ["700000000000000001"]
    assert len(timers.calls) == 1
    assert _flagged(result)["700000000000000001"] > _now()


# -- timer refire ---------------------------------------------------------------


async def test_timer_refire_removes_the_role_and_forgets_the_member():
    seeded = {
        _MEMORY_KEY: {
            "700000000000000001": _now() + 60,
            "700000000000000002": _now() + 120,
        }
    }
    result, actor, emitter, timers = await _fire(
        _timer_context("700000000000000001"), memory=seeded
    )
    assert result.outcome == "ok", result.error
    assert [call[:3] for call in actor.calls] == [
        ("remove_role", "700000000000000001", _SUS_ROLE)
    ]
    # The expiry is over, so the entry goes; everyone else is untouched.
    assert set(_flagged(result)) == {"700000000000000002"}
    # A refire must never announce, and must never arm another timer.
    assert emitter.messages == []
    assert timers.calls == []


async def test_timer_refire_for_a_departed_member_is_a_silent_no_op():
    # remove_role returns False when the member has left the guild — an expected
    # outcome, never an error, and the entry is still cleaned up.
    seeded = {_MEMORY_KEY: {"700000000000000009": _now() + 60}}
    result, actor, emitter, _ = await _fire(
        _timer_context("700000000000000009"),
        memory=seeded,
        gone=["700000000000000009"],
    )
    assert result.outcome == "ok", result.error
    assert result.error is None
    assert [call[:3] for call in actor.calls] == [
        ("remove_role", "700000000000000009", _SUS_ROLE)
    ]
    assert emitter.messages == []
    assert _flagged(result) == {}


async def test_timer_refire_for_an_untracked_member_still_removes_the_role():
    # The role is the ground truth; a memory map that lost the entry (pruned,
    # trimmed, or hand-edited) must not strand the role on the member.
    result, actor, _, _ = await _fire(_timer_context("700000000000000007"))
    assert result.outcome == "ok", result.error
    assert [call[:3] for call in actor.calls] == [
        ("remove_role", "700000000000000007", _SUS_ROLE)
    ]


# -- !list_sus ------------------------------------------------------------------


async def test_list_sus_renders_only_live_entries_and_prunes_the_rest():
    live = _now() + 600
    seeded = {
        _MEMORY_KEY: {
            "700000000000000001": live,
            "700000000000000002": _now() - 1,
            "700000000000000003": _now() - 99999,
        }
    }
    result, actor, emitter, _ = await _fire(
        _message_context("!list_sus"), memory=seeded
    )
    assert result.outcome == "ok", result.error
    # A viewer never mutates roles.
    assert actor.calls == []
    assert len(emitter.messages) == 1
    _, content = emitter.messages[0]
    assert "<@700000000000000001>" in content
    assert "700000000000000002" not in content
    assert "700000000000000003" not in content
    # The past-due entries are pruned from the persisted map, not just hidden.
    assert _flagged(result) == {"700000000000000001": live}


async def test_list_sus_stays_inside_the_message_limit_when_many_are_flagged():
    """The emitter truncates at 2000 chars, which would cut a line mid-mention.

    The expiry map is capped at 300 entries, so the full list can genuinely
    reach ~14 KB. A bounded page plus a count is readable; a silent cut is not.
    """
    live = _now() + 600
    seeded = {
        _MEMORY_KEY: {
            f"7000000000000{index:06d}": live for index in range(300)
        }
    }
    result, _, emitter, _ = await _fire(
        _message_context("!list_sus"), memory=seeded
    )
    assert result.outcome == "ok", result.error
    _, content = emitter.messages[0]
    assert len(content) <= 2000
    assert "and 260 more" in content


async def test_list_sus_with_nobody_flagged_says_so():
    seeded = {_MEMORY_KEY: {"700000000000000002": _now() - 1}}
    result, _, emitter, _ = await _fire(
        _message_context("!list_sus"), memory=seeded
    )
    assert result.outcome == "ok", result.error
    assert len(emitter.messages) == 1
    assert "No One Is Sus" in emitter.messages[0][1]
    assert _flagged(result) == {}


# -- memory boundedness ---------------------------------------------------------


async def test_expiry_map_is_trimmed_to_its_cap():
    # Even a pathological run of flags cannot grow the map without bound: the
    # oldest-expiring entries are dropped first (the role and its own timer are
    # unaffected — memory only backs !list_sus and the re-sus check).
    base = _now() + 1000
    seeded = {
        _MEMORY_KEY: {
            str(900000000000000000 + index): base + index for index in range(305)
        }
    }
    result, _, _, _ = await _fire(_message_context("!sus"), memory=seeded)
    assert result.outcome == "ok", result.error
    flagged = _flagged(result)
    assert len(flagged) == 300
    # The newly-flagged author survives the trim; the soonest-expiring entries go.
    assert "700000000000000001" in flagged
    assert "900000000000000000" not in flagged
    assert str(900000000000000000 + 304) in flagged


async def test_memory_stays_well_inside_the_cap_across_many_flags():
    memory: dict = {}
    for index in range(40):
        mentions = [str(910000000000000000 + index * 3 + offset) for offset in range(3)]
        result, _, _, _ = await _fire(
            _message_context(
                "!sus " + " ".join(f"<@{uid}>" for uid in mentions),
                author_role_ids=[_MODERATOR_ROLE],
                mentioned_user_ids=mentions,
            ),
            memory=memory,
        )
        assert result.outcome == "ok", result.error
        memory = result.memory
    assert len(_flagged(result)) == 120
    # Comfortably inside the 16 KB handler-memory ceiling, with the trim cap
    # keeping the worst case there too.
    assert len(json.dumps(memory).encode("utf-8")) < MAX_MEMORY_BYTES // 2
