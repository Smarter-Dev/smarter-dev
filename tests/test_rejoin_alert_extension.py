"""Tests for the rejoin-alert catalog extension.

Two layers, matching the house pattern for catalog extensions:

1. Render/lint layer — the manifest renders against its example_config, the
   rendered script passes ``handler_lint``, and the mod-log channel is baked in
   as a quoted literal (``member_join`` is guild-scoped, so there is no home
   channel and every send must name its target).
2. Behaviour layer — the rendered Monty script runs in the real handler runtime
   with a stubbed emitter/mod-action reader: the alert itself, the silent
   clean-member path, the bot-join short circuit (which must not even spend the
   lookup), and a null ``created_at`` row.

The cost invariant the spec states is asserted directly: at most ONE
``list_mod_actions`` lookup and at most ONE message per join, behind the cheap
``member_join`` gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import pytest

from smarter_dev.extensions.catalog.rejoin_alert import MANIFEST
from smarter_dev.extensions.rendering import RenderedHandler
from smarter_dev.extensions.rendering import RenderError
from smarter_dev.extensions.rendering import render_bundle
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_runtime import run_handler_script

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / (
    "smarter_dev/extensions/catalog/rejoin_alert"
)
_MOD_LOG_CHANNEL = "123456789012345678"
_HANDLER_KEY = "rejoin-alert"


def _scripts() -> dict[str, str]:
    return {
        handler.key: (_PACKAGE_DIR / handler.script_file).read_text()
        for handler in MANIFEST.handlers
    }


def _rendered(config: dict | None = None) -> RenderedHandler:
    bundle = render_bundle(MANIFEST, config or MANIFEST.example_config, _scripts())
    return {item.key: item for item in bundle}[_HANDLER_KEY]


# -- fakes ---------------------------------------------------------------------


@dataclass
class _Emitter:
    messages: list = field(default_factory=list)

    async def create_message(
        self, channel_id, content, ping_role_id=None, tolerate_missing_target=False
    ):
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"

    async def get_thread_parent_id(self, thread_id):
        return None

    async def get_channel_guild_id(self, channel_id):
        return "G1"


@dataclass
class _Limiter:
    async def hit(self, key, limit, window_seconds=None):
        return True


class _StubActor:
    """Marker only — its presence flips the runtime into the admin tier, which is
    what exposes list_mod_actions and cross-channel sends."""


@dataclass
class _ModActionReader:
    """Stands in for the DB-backed list_mod_actions reader (guild-bound host-side)."""

    rows: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    async def __call__(self, user_id, limit):
        self.calls.append((user_id, limit))
        return list(self.rows)


def _action_row(
    action_type: str = "ban",
    created_at: str | None = "2026-05-04T18:30:00+00:00",
) -> dict:
    return {
        "action_type": action_type,
        "reason": "spam",
        "source": "manual",
        "moderator_username": "ada",
        "duration_seconds": None,
        "channel_id": "C9",
        "trigger_message_id": "M9",
        "created_at": created_at,
    }


def _join_context(*, is_bot: bool = False) -> dict:
    return {
        "trigger_type": "member_join",
        "member_id": "555000111222333444",
        "username": "recidivist",
        "display_name": "Recidivist",
        "is_bot": is_bot,
        "account_created_at": "2021-03-02T00:00:00+00:00",
        "has_custom_avatar": False,
        "guild_member_count": 1200,
        "guild_human_member_count": 1150,
    }


async def _run(context: dict, rows: list | None = None):
    emitter = _Emitter()
    reader = _ModActionReader(rows=rows or [])
    result = await run_handler_script(
        _rendered().script,
        context,
        # member_join is guild-scoped: no triggering channel, exactly as the
        # admin fire job passes it.
        channel_id="",
        guild_id="G1",
        emitter=emitter,
        limiter=_Limiter(),
        budget=admin_budget(),
        actor=_StubActor(),
        channel_ids=[],
        memory={},
        mod_action_reader=reader,
    )
    return result, emitter, reader


# -- render / manifest layer ---------------------------------------------------


def test_manifest_shape():
    assert MANIFEST.slug == "rejoin-alert"
    assert [handler.key for handler in MANIFEST.handlers] == [_HANDLER_KEY]
    handler = MANIFEST.handlers[0]
    assert handler.trigger_type == "member_join"
    # Guild-scoped trigger: no channel scope — the mod-log channel is a script
    # constant, not the fire's home channel.
    assert handler.channel_scope == []
    assert handler.settings == {}


def test_config_schema_declares_the_mod_log_channel():
    fields = {field.name: field for field in MANIFEST.config}
    assert set(fields) == {"mod_log_channel_id"}
    assert fields["mod_log_channel_id"].type == "channel_id"
    assert fields["mod_log_channel_id"].required is True


def test_example_config_renders_and_lints_clean():
    rendered = _rendered()
    assert lint_script(rendered.script) is None
    assert rendered.channel_ids == []
    assert f'MOD_LOG_CHANNEL_ID = "{_MOD_LOG_CHANNEL}"' in rendered.script


def test_non_snowflake_channel_is_rejected_at_render():
    with pytest.raises(RenderError, match="mod_log_channel_id"):
        render_bundle(MANIFEST, {"mod_log_channel_id": "#mod-log"}, _scripts())


# -- behaviour layer (rendered script in the real runtime) ---------------------


async def test_member_with_history_alerts_the_mod_log_once():
    result, emitter, reader = await _run(
        _join_context(),
        rows=[
            _action_row("ban", "2026-05-04T18:30:00+00:00"),
            _action_row("warn", "2026-01-09T10:00:00+00:00"),
        ],
    )
    assert result.outcome == "ok", result.error
    assert len(emitter.messages) == 1
    channel, content = emitter.messages[0]
    assert channel == _MOD_LOG_CHANNEL
    assert "Member Rejoined" in content
    assert "recidivist" in content
    assert "(555000111222333444)" in content
    # Count is a floor (the read is depth-capped), rendered "n+".
    assert "2+ prior mod actions" in content
    # Rows arrive newest-first, so row 0 is the most recent action.
    assert "most recent: ban on 2026-05-04" in content


async def test_exactly_one_lookup_at_the_spec_depth():
    _, _, reader = await _run(_join_context(), rows=[_action_row()])
    assert reader.calls == [("555000111222333444", 5)]


async def test_clean_member_join_is_silent():
    result, emitter, reader = await _run(_join_context(), rows=[])
    assert result.outcome == "ok", result.error
    assert emitter.messages == []
    # The lookup still happens (that is how "clean" is established) but only once,
    # and it never turns into a message.
    assert len(reader.calls) == 1
    assert result.usage["lookups"] == 1
    assert result.usage["messages_sent"] == 0


async def test_bot_join_short_circuits_before_the_lookup():
    # Joins burst during raids and bot adds carry no member history — spend
    # nothing at all on them.
    result, emitter, reader = await _run(
        _join_context(is_bot=True), rows=[_action_row()]
    )
    assert result.outcome == "ok", result.error
    assert reader.calls == []
    assert emitter.messages == []
    assert result.usage["lookups"] == 0


async def test_row_without_a_timestamp_still_alerts():
    # created_at is nullable on the row shape; a missing date must not crash the
    # fire or render a truncated placeholder.
    result, emitter, _ = await _run(
        _join_context(), rows=[_action_row("kick", None)]
    )
    assert result.outcome == "ok", result.error
    _, content = emitter.messages[0]
    assert "most recent: kick" in content
    assert "an unknown date" in content


async def test_alert_stays_within_one_message_and_one_lookup():
    # The whole cost story for the handler: even at full read depth it is one
    # lookup and one message.
    result, emitter, reader = await _run(
        _join_context(), rows=[_action_row() for _ in range(5)]
    )
    assert result.outcome == "ok", result.error
    assert result.usage["lookups"] == 1
    assert result.usage["messages_sent"] == 1
    assert "5+ prior mod actions" in emitter.messages[0][1]
