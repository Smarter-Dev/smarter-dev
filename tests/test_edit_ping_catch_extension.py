"""Tests for the edit-ping-catch catalog extension.

Two layers, matching the house pattern for catalog extensions:

1. Render/lint layer — the manifest renders against its example_config, the
   rendered script passes ``handler_lint`` (the same static rails the registry
   runs at startup), the staff role is baked in as a quoted literal, and a
   malformed role id is rejected at render time.
2. Behaviour layer — the rendered Monty script runs in the real handler runtime
   with a stubbed emitter/actor over every branch: the catch itself (``@everyone``
   and ``@here``), both staff exemptions, and the silent common case.

The handler is guild-wide (``channel_scope == []``) on the admin-tier
``message_edit`` trigger, which IS channel-keyed — so the notice goes to the
edit's own home channel and needs no channel constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import pytest

from smarter_dev.extensions.catalog.edit_ping_catch import MANIFEST
from smarter_dev.extensions.rendering import RenderedHandler
from smarter_dev.extensions.rendering import RenderError
from smarter_dev.extensions.rendering import render_bundle
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_runtime import run_handler_script

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / (
    "smarter_dev/extensions/catalog/edit_ping_catch"
)
_STAFF_ROLE = "222222222222222222"
_EDIT_CHANNEL = "999999999999999999"
_HANDLER_KEY = "edit-ping-catch"


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
class _Actor:
    deletions: list = field(default_factory=list)

    async def delete_message(self, channel_id, message_id):
        self.deletions.append((channel_id, message_id))
        return "ok"


@dataclass
class _Limiter:
    async def hit(self, key, limit, window_seconds=None):
        return True


def _edit_context(
    *,
    message_content: str,
    has_manage_messages: bool = False,
    author_role_ids: list[str] | None = None,
) -> dict:
    return {
        "trigger_type": "message_edit",
        "message_id": "M1",
        "message_content": message_content,
        "old_content": "hello friends",
        "author_id": "U1",
        "author_name": "ada",
        "author_account_created_at": "2020-01-01T00:00:00+00:00",
        "author_joined_at": "2024-01-01T00:00:00+00:00",
        "author_role_ids": author_role_ids if author_role_ids is not None else [],
        "author_has_manage_messages": has_manage_messages,
        "channel_parent_id": None,
    }


async def _run(context: dict):
    emitter = _Emitter()
    actor = _Actor()
    result = await run_handler_script(
        _rendered().script,
        context,
        channel_id=_EDIT_CHANNEL,
        guild_id="G1",
        emitter=emitter,
        limiter=_Limiter(),
        budget=admin_budget(),
        actor=actor,
        channel_ids=[],
        memory={},
    )
    return result, emitter, actor


# -- render / manifest layer ---------------------------------------------------


def test_manifest_shape():
    assert MANIFEST.slug == "edit-ping-catch"
    assert [handler.key for handler in MANIFEST.handlers] == [_HANDLER_KEY]
    handler = MANIFEST.handlers[0]
    assert handler.trigger_type == "message_edit"
    # Guild-wide: no channel scope, and message_edit is not a message trigger so
    # there is no bot-message opt-in to declare.
    assert handler.channel_scope == []
    assert handler.settings == {}


def test_config_schema_declares_the_staff_role():
    fields = {field.name: field for field in MANIFEST.config}
    assert set(fields) == {"staff_role_id"}
    assert fields["staff_role_id"].type == "role_id"
    assert fields["staff_role_id"].required is True


def test_example_config_renders_and_lints_clean():
    rendered = _rendered()
    assert lint_script(rendered.script) is None
    # Guild-wide install: the row carries no channel scope at all.
    assert rendered.channel_ids == []
    assert f'STAFF_ROLE_ID = "{_STAFF_ROLE}"' in rendered.script


def test_non_snowflake_staff_role_is_rejected_at_render():
    with pytest.raises(RenderError, match="staff_role_id"):
        render_bundle(MANIFEST, {"staff_role_id": "moderators"}, _scripts())


# -- behaviour layer (rendered script in the real runtime) ---------------------


async def test_everyone_edited_in_is_warned_and_deleted():
    result, emitter, actor = await _run(
        _edit_context(message_content="surprise @everyone free nitro")
    )
    assert result.outcome == "ok", result.error
    assert len(emitter.messages) == 1
    channel, content = emitter.messages[0]
    # message_edit is channel-keyed, so the notice lands in the edit's channel.
    assert channel == _EDIT_CHANNEL
    assert "<@U1>" in content
    # The offending edit is removed, in the channel the edit happened in.
    assert actor.deletions == [(_EDIT_CHANNEL, "M1")]


async def test_here_edited_in_is_caught_too():
    result, emitter, actor = await _run(
        _edit_context(message_content="psst @here look at this")
    )
    assert result.outcome == "ok", result.error
    assert len(emitter.messages) == 1
    assert actor.deletions == [(_EDIT_CHANNEL, "M1")]


async def test_notice_never_contains_a_mass_mention_itself():
    # The warning must not echo the trigger text back — an @everyone/@here inside
    # the bot's own notice would be the very thing being policed.
    _, emitter, _ = await _run(
        _edit_context(message_content="surprise @everyone free nitro")
    )
    _, content = emitter.messages[0]
    assert "@everyone" not in content
    assert "@here" not in content


async def test_manage_messages_holder_is_exempt():
    result, emitter, actor = await _run(
        _edit_context(
            message_content="heads up @everyone", has_manage_messages=True
        )
    )
    assert result.outcome == "ok", result.error
    assert emitter.messages == []
    assert actor.deletions == []


async def test_configured_staff_role_is_exempt():
    result, emitter, actor = await _run(
        _edit_context(
            message_content="heads up @everyone",
            author_role_ids=["333333333333333333", _STAFF_ROLE],
        )
    )
    assert result.outcome == "ok", result.error
    assert emitter.messages == []
    assert actor.deletions == []


async def test_unrelated_role_is_not_an_exemption():
    result, emitter, actor = await _run(
        _edit_context(
            message_content="heads up @everyone",
            author_role_ids=["333333333333333333"],
        )
    )
    assert result.outcome == "ok", result.error
    assert len(emitter.messages) == 1
    assert actor.deletions == [(_EDIT_CHANNEL, "M1")]


async def test_ordinary_edit_is_a_silent_noop():
    # The common path: edits are frequent, so a normal correction spends nothing.
    result, emitter, actor = await _run(
        _edit_context(message_content="fixed a typo, sorry everyone")
    )
    assert result.outcome == "ok", result.error
    assert emitter.messages == []
    assert actor.deletions == []
    assert result.usage["messages_sent"] == 0
    assert result.usage["mod_actions"] == 0
