"""Tests for the member-milestones catalog extension.

Two layers, exercising ONLY this extension's own manifest + scripts (the
whole-catalog registry scan lives in tests/test_extension_registry.py):

1. Render layer — the manifest renders against its example_config and every
   rendered script passes handler_lint (render_bundle runs the same static rails
   the registry runs at startup), and the materialised handler shape is correct
   (guild-scoped member_* triggers keep channel_ids empty; the announce channel
   is a script constant, not a scoped channel).
2. Behaviour layer — the rendered Monty scripts run in the real handler runtime
   with a stubbed emitter for the key paths: milestone high-water gating /
   re-baseline / null-count guard, and the booster once-per-transition thank-you
   preserving both boost stats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from smarter_dev.extensions.catalog.member_milestones import MANIFEST
from smarter_dev.extensions.rendering import RenderedHandler, render_bundle
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_runtime import run_handler_script

_EXTENSION_DIR = Path(__file__).parent.parent / (
    "smarter_dev/extensions/catalog/member_milestones"
)
_ANNOUNCE_CHANNEL = "123456789012345678"


def _load_scripts() -> dict[str, str]:
    return {
        handler.key: (_EXTENSION_DIR / handler.script_file).read_text()
        for handler in MANIFEST.handlers
    }


def _render(config: dict | None = None) -> dict[str, RenderedHandler]:
    bundle = render_bundle(
        MANIFEST, config or MANIFEST.example_config, _load_scripts()
    )
    return {item.key: item for item in bundle}


# -- render / manifest layer ---------------------------------------------------


def test_manifest_shape():
    assert MANIFEST.slug == "member-milestones"
    keys = {h.key for h in MANIFEST.handlers}
    assert keys == {"milestone-announce", "booster-thanks"}
    by_key = {h.key: h for h in MANIFEST.handlers}
    assert by_key["milestone-announce"].trigger_type == "member_join"
    assert by_key["booster-thanks"].trigger_type == "member_role_change"
    # Guild-scoped member_* triggers: NO channel scope, NO grantable roles, and
    # (not being message triggers) no bot-message opt-in.
    for handler in MANIFEST.handlers:
        assert handler.channel_scope == []
        assert handler.settings == {}


def test_config_schema():
    fields = {f.name: f for f in MANIFEST.config}
    assert fields["announce_channel_id"].type == "channel_id"
    assert fields["announce_channel_id"].required is True
    assert fields["milestone_step"].type == "int"
    assert fields["milestone_step"].required is False
    assert fields["milestone_step"].default == 250


def test_example_config_renders_and_lints_clean():
    rendered = _render()
    assert set(rendered) == {"milestone-announce", "booster-thanks"}
    for item in rendered.values():
        # render_bundle already lints; assert explicitly for a sharp failure.
        assert lint_script(item.script) is None
        # The announce channel is baked into the script, never a scoped channel.
        assert item.channel_ids == []
        assert f'"{_ANNOUNCE_CHANNEL}"' in item.script


def test_milestone_step_bakes_as_bare_int_literal():
    rendered = _render()
    assert "MILESTONE_STEP = 250" in rendered["milestone-announce"].script
    assert f'ANNOUNCE_CHANNEL_ID = "{_ANNOUNCE_CHANNEL}"' in (
        rendered["milestone-announce"].script
    )


def test_milestone_step_default_applied_when_omitted():
    # Optional int field: omitting it falls back to the schema default (250).
    rendered = _render({"announce_channel_id": _ANNOUNCE_CHANNEL})
    assert "MILESTONE_STEP = 250" in rendered["milestone-announce"].script


# -- behaviour layer (rendered scripts in the real runtime) --------------------


@dataclass
class _FakeEmitter:
    messages: list = field(default_factory=list)

    async def create_message(
        self,
        channel_id: str,
        content: str,
        ping_role_id: str | None = None,
        tolerate_missing_target: bool = False,
    ) -> str:
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"


@dataclass
class _StubLimiter:
    async def hit(self, key: str, limit: int, window_seconds: int | None = None) -> bool:
        return True


class _StubActor:
    """Marker only — its presence flips the runtime into admin tier so a
    guild-scoped handler may send to an explicit channel."""


async def _run(script: str, context: dict, *, memory: dict | None = None):
    emitter = _FakeEmitter()
    result = await run_handler_script(
        script,
        context,
        channel_id="",
        guild_id="G1",
        emitter=emitter,
        limiter=_StubLimiter(),
        budget=admin_budget(),
        actor=_StubActor(),
        channel_ids=[],
        memory=memory or {},
    )
    return result, emitter


def _join_context(count: int | None) -> dict:
    return {
        "trigger_type": "member_join",
        "member_id": "42",
        "is_bot": False,
        "guild_human_member_count": count,
    }


async def test_milestone_announces_when_crossing_a_new_step():
    script = _render()["milestone-announce"].script
    result, emitter = await _run(script, _join_context(250))
    assert result.outcome == "ok", result.error
    assert emitter.messages == [
        (_ANNOUNCE_CHANNEL, "🎉 We just passed 250 members! Welcome aboard! 🎉")
    ]
    # High-water mark advanced so the same milestone never re-announces.
    assert result.memory["highest"] == 250


async def test_milestone_silent_between_steps_but_tracks_high():
    script = _render()["milestone-announce"].script
    result, emitter = await _run(script, _join_context(251), memory={"highest": 250})
    assert result.outcome == "ok", result.error
    assert emitter.messages == []  # 251 is the same 250-step, no re-announce
    assert result.memory["highest"] == 251


async def test_milestone_announces_the_crossed_boundary_not_the_raw_count():
    script = _render()["milestone-announce"].script
    # Jump from 260 to 500 crosses the 500 boundary; the announced number is the
    # milestone (500), not the exact member count.
    result, emitter = await _run(script, _join_context(500), memory={"highest": 260})
    assert result.outcome == "ok", result.error
    assert emitter.messages == [
        (_ANNOUNCE_CHANNEL, "🎉 We just passed 500 members! Welcome aboard! 🎉")
    ]


async def test_milestone_rebaselines_after_a_purge_without_announcing():
    script = _render()["milestone-announce"].script
    # A ban wave drops the count a full step below the old peak: re-baseline the
    # high-water mark (so a later recovery re-announces) and stay silent now.
    result, emitter = await _run(script, _join_context(200), memory={"highest": 500})
    assert result.outcome == "ok", result.error
    assert emitter.messages == []
    assert result.memory["highest"] == 200


async def test_milestone_null_count_is_a_safe_noop():
    # guild_human_member_count is best-effort; a null must not crash the fire.
    script = _render()["milestone-announce"].script
    result, emitter = await _run(script, _join_context(None))
    assert result.outcome == "ok", result.error
    assert emitter.messages == []


def _role_change_context(*, boost_added: bool) -> dict:
    return {
        "trigger_type": "member_role_change",
        "member_id": "42",
        "member_display_name": "Ada",
        "is_boost_role_added": boost_added,
        "premium_subscription_count": 14,
        "boosting_member_count": 9,
    }


async def test_booster_thanks_preserves_both_stats():
    script = _render()["booster-thanks"].script
    result, emitter = await _run(script, _role_change_context(boost_added=True))
    assert result.outcome == "ok", result.error
    assert len(emitter.messages) == 1
    channel, content = emitter.messages[0]
    assert channel == _ANNOUNCE_CHANNEL
    assert "Ada Has Boosted The Server" in content
    # Distinct figures: 14 total boosts from 9 boosting members.
    assert "14 boosts from 9 boosting members" in content


async def test_booster_silent_when_boost_role_not_added():
    # member_role_change fires on any role delta; only a boost addition announces.
    script = _render()["booster-thanks"].script
    result, emitter = await _run(script, _role_change_context(boost_added=False))
    assert result.outcome == "ok", result.error
    assert emitter.messages == []
