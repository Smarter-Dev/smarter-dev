"""Tests for the newcomer-watch catalog extension.

Two layers, matching the house pattern for catalog extensions:

1. Render/lint layer — the manifest renders against its example_config, the
   rendered script passes ``handler_lint`` (including at the maximum length a
   guild's ``agent_instruction`` can be, since it is inlined into the script and
   the lint cap is 8 KB), and every config value is baked in as a typed literal.
2. Behaviour layer — the rendered Monty script runs in the real handler runtime
   with a stubbed emitter/actor/agent: the cheap guard, both watch conditions,
   anchored parsing of the agent reply, and the allowed_actions re-validation.

The cost invariant the extension claims is asserted directly: an unwatched
message spends NOTHING (no lookup, no agent call, no emit), and a watched one is
bounded at one lookup, one agent call, one moderation action, and one mod-log
message.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from smarter_dev.extensions.catalog.newcomer_watch import MANIFEST
from smarter_dev.extensions.rendering import RenderedHandler
from smarter_dev.extensions.rendering import RenderError
from smarter_dev.extensions.rendering import render_bundle
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_runtime import run_handler_script

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / (
    "smarter_dev/extensions/catalog/newcomer_watch"
)
_MOD_LOG_CHANNEL = "123456789012345678"
_HOME_CHANNEL = "222222222222222222"
_HANDLER_KEY = "newcomer-watch"
_AUTHOR = "555000111222333444"


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
    dms: list = field(default_factory=list)
    dm_delivers: bool = True

    async def create_message(
        self, channel_id, content, ping_role_id=None, tolerate_missing_target=False
    ):
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"

    async def send_dm(self, user_id, content):
        self.dms.append((user_id, content))
        return self.dm_delivers

    async def get_thread_parent_id(self, thread_id):
        return None

    async def get_channel_guild_id(self, channel_id):
        return "G1"


@dataclass
class _Limiter:
    async def hit(self, key, limit, window_seconds=None):
        return True


@dataclass
class _Actor:
    """Stands in for AdminActor; its presence flips the runtime to the admin tier."""

    calls: list = field(default_factory=list)

    async def ban_user(self, user_id, reason=None, delete_message_seconds=0):
        self.calls.append(("ban", user_id, reason))
        return user_id

    async def kick_user(self, user_id):
        self.calls.append(("kick", user_id, None))
        return user_id

    async def timeout_user(self, user_id, duration_seconds=600):
        self.calls.append(("timeout", user_id, duration_seconds))
        return user_id


@dataclass
class _Agent:
    """The injected agent runner: records prompts, replays a scripted reply."""

    reply: str = "ACTION: none\nREASON: nothing to see"
    prompts: list = field(default_factory=list)

    async def __call__(self, prompt, has_tools, budget):
        self.prompts.append(prompt)
        return self.reply


@dataclass
class _ModActionReader:
    rows: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    async def __call__(self, user_id, limit):
        self.calls.append((user_id, limit))
        return list(self.rows)


@dataclass
class _ModActionRecorder:
    warn_count: int = 1
    calls: list = field(default_factory=list)

    async def __call__(self, user_id, reason, channel_id):
        self.calls.append((user_id, reason, channel_id))
        return self.warn_count


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _message_context(
    *,
    joined_days_ago: float | None = 400.0,
    days_since_last_message: int | None = 0,
    content: str = "hey everyone, check out my link",
    is_bot: bool = False,
    manage_messages: bool = False,
    attachments: list | None = None,
    thread_id: str | None = None,
) -> dict:
    context = {
        "trigger_type": "message",
        "guild_id": "G1",
        "message_content": content,
        "message_id": "999888777666555444",
        "author_id": _AUTHOR,
        "author_name": "newbie",
        "author_is_bot": is_bot,
        "author_account_created_at": _iso(3.0),
        "author_joined_at": None if joined_days_ago is None else _iso(joined_days_ago),
        "author_role_ids": [],
        "author_has_manage_messages": manage_messages,
        "mentioned_user_ids": [],
        "mentioned_role_ids": [],
        "mentions_everyone": False,
        "channel_parent_id": None,
        "attachments": attachments if attachments is not None else [],
        "embeds": [],
        "interaction_user_id": None,
        "is_thread": thread_id is not None,
        # Enrichment from the dispatcher, read BEFORE this message is recorded.
        "author_is_first_message": days_since_last_message is None,
        "author_days_since_last_message": days_since_last_message,
        "author_last_message_at": (
            None if days_since_last_message is None else _iso(days_since_last_message)
        ),
    }
    if thread_id is not None:
        context["thread_id"] = thread_id
        context["thread_name"] = "a thread"
    return context


async def _run(
    context: dict,
    *,
    reply: str = "ACTION: none\nREASON: nothing to see",
    rows: list | None = None,
    config: dict | None = None,
):
    emitter = _Emitter()
    actor = _Actor()
    agent = _Agent(reply=reply)
    reader = _ModActionReader(rows=rows or [])
    recorder = _ModActionRecorder()
    result = await run_handler_script(
        _rendered(config).script,
        context,
        channel_id=_HOME_CHANNEL,
        guild_id="G1",
        emitter=emitter,
        limiter=_Limiter(),
        budget=admin_budget(),
        actor=actor,
        channel_ids=[],
        memory={},
        agent_runner=agent,
        mod_action_reader=reader,
        mod_action_recorder=recorder,
    )
    return result, emitter, actor, agent, reader, recorder


def _mod_log(emitter: _Emitter) -> str:
    posted = [content for channel, content in emitter.messages if channel == _MOD_LOG_CHANNEL]
    assert len(posted) == 1, emitter.messages
    return posted[0]


# -- render / manifest layer ---------------------------------------------------


def test_manifest_shape():
    assert MANIFEST.slug == "newcomer-watch"
    assert [handler.key for handler in MANIFEST.handlers] == [_HANDLER_KEY]
    handler = MANIFEST.handlers[0]
    assert handler.trigger_type == "message"
    # Guild-wide: the populations this watches are not channel-specific, and the
    # cheap guard is what makes guild-wide affordable.
    assert handler.channel_scope == []
    # No role grants and no bot-message opt-in.
    assert handler.settings == {}


def test_config_schema():
    fields = {field.name: field for field in MANIFEST.config}
    assert set(fields) == {
        "agent_instruction",
        "allowed_actions",
        "mod_log_channel_id",
        "new_member_days",
        "inactive_days",
        "timeout_seconds",
    }
    assert fields["mod_log_channel_id"].type == "channel_id"
    assert fields["agent_instruction"].required is True
    # There is no list config type, so the action set is a comma-separated string
    # the script normalizes itself.
    assert fields["allowed_actions"].type == "string"
    assert fields["allowed_actions"].default == "none,warn"
    assert fields["new_member_days"].type == "int"
    assert fields["inactive_days"].type == "int"
    assert fields["timeout_seconds"].default == 600


def test_example_config_renders_and_lints_clean():
    rendered = _rendered()
    assert lint_script(rendered.script) is None
    assert rendered.channel_ids == []
    assert f'MOD_LOG_CHANNEL_ID = "{_MOD_LOG_CHANNEL}"' in rendered.script
    assert "NEW_MEMBER_DAYS = 7" in rendered.script
    assert "INACTIVE_DAYS = 30" in rendered.script
    assert "TIMEOUT_SECONDS = 600" in rendered.script


def test_longest_possible_instruction_still_fits_the_lint_cap():
    # agent_instruction is inlined into the script, so the 8 KB handler-lint cap
    # has to hold at the config layer's own 500-char string maximum — otherwise a
    # guild writing a long instruction would fail at install time, not here.
    config = dict(MANIFEST.example_config)
    config["agent_instruction"] = "x" * 500
    assert lint_script(_rendered(config).script) is None


def test_non_snowflake_channel_is_rejected_at_render():
    with pytest.raises(RenderError, match="mod_log_channel_id"):
        render_bundle(
            MANIFEST,
            {**MANIFEST.example_config, "mod_log_channel_id": "#mod-log"},
            _scripts(),
        )


# -- the cheap guard -----------------------------------------------------------


async def test_established_active_member_spends_nothing():
    # The whole point of a guild-wide message handler: the common path costs a
    # dict read and an ISO parse, and never reaches a lookup, agent, or emit.
    result, emitter, actor, agent, reader, _ = await _run(
        _message_context(joined_days_ago=400.0, days_since_last_message=1)
    )
    assert result.outcome == "ok", result.error
    assert reader.calls == []
    assert agent.prompts == []
    assert emitter.messages == []
    assert actor.calls == []
    assert result.usage["lookups"] == 0
    assert result.usage["agent_calls"] == 0
    assert result.usage["messages_sent"] == 0


async def test_bot_author_is_never_acted_on():
    result, _, _, agent, reader, _ = await _run(
        _message_context(joined_days_ago=1.0, is_bot=True)
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []
    assert reader.calls == []


async def test_member_with_moderation_powers_is_exempt():
    # Matches /warn's refusal to warn moderators — checked before any spend.
    result, _, _, agent, reader, _ = await _run(
        _message_context(joined_days_ago=1.0, manage_messages=True)
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []
    assert reader.calls == []


async def test_empty_message_with_no_attachment_is_skipped():
    result, _, _, agent, _, _ = await _run(
        _message_context(joined_days_ago=1.0, content="   ")
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []


async def test_attachment_only_post_from_a_new_member_is_still_reviewed():
    result, _, _, agent, _, _ = await _run(
        _message_context(
            joined_days_ago=1.0,
            content="",
            attachments=[{"url": "u", "content_type": "image/png", "filename": "f"}],
        )
    )
    assert result.outcome == "ok", result.error
    assert len(agent.prompts) == 1


async def test_uncached_join_date_alone_does_not_make_a_member_new():
    # author_joined_at is null when the member object was not cached; the
    # new-member condition fails closed rather than guessing.
    result, _, _, agent, _, _ = await _run(
        _message_context(joined_days_ago=None, days_since_last_message=0)
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []


async def test_first_ever_message_is_not_treated_as_an_absence():
    # author_is_first_message is true for every established member's first
    # message after activity tracking began, so it must not stand in for a
    # measured gap — otherwise installing the extension would agent-call the
    # whole active membership once.
    result, _, _, agent, _, _ = await _run(
        _message_context(joined_days_ago=400.0, days_since_last_message=None)
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []


# -- the two watch conditions --------------------------------------------------


async def test_new_member_is_reviewed_and_the_agent_is_told_which_condition():
    result, _, _, agent, reader, _ = await _run(
        _message_context(joined_days_ago=2.0, days_since_last_message=0),
        rows=[
            {
                "action_type": "warn",
                "reason": "spam",
                "source": "manual",
                "moderator_username": "ada",
                "duration_seconds": None,
                "channel_id": None,
                "trigger_message_id": None,
                "created_at": "2026-05-04T18:30:00+00:00",
            }
        ],
    )
    assert result.outcome == "ok", result.error
    prompt = agent.prompts[0]
    assert "joined 2 day(s) ago" in prompt
    assert "EVERY message is reviewed" in prompt
    # The guild's own instruction, the mod history, and the account age all ride
    # along, and the message is delimited and labelled untrusted.
    assert "Flag crypto or NFT promotion" in prompt
    assert "warn (2026-05-04)" in prompt
    assert "Account age 3 day(s)" in prompt
    assert "UNTRUSTED DATA" in prompt
    assert "<<<MESSAGE" in prompt
    # Exactly one lookup at the documented depth.
    assert reader.calls == [(_AUTHOR, 5)]


async def test_returning_member_is_reviewed_with_the_length_of_the_absence():
    result, _, _, agent, _, _ = await _run(
        _message_context(joined_days_ago=700.0, days_since_last_message=180)
    )
    assert result.outcome == "ok", result.error
    prompt = agent.prompts[0]
    assert "in the guild 700 day(s)" in prompt
    assert "back after 180 day(s) of silence" in prompt
    assert "weigh tenure heavily" in prompt


async def test_member_just_inside_the_activity_threshold_is_not_reviewed():
    result, _, _, agent, _, _ = await _run(
        _message_context(joined_days_ago=700.0, days_since_last_message=29)
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []


# -- parsing + re-validation ---------------------------------------------------


async def test_clean_verdict_is_silent_everywhere():
    result, emitter, actor, _, _, _ = await _run(
        _message_context(joined_days_ago=1.0),
        reply="ACTION: none\nREASON: ordinary introduction",
    )
    assert result.outcome == "ok", result.error
    assert emitter.messages == []
    assert actor.calls == []


async def test_prose_mentioning_a_forbidden_action_is_not_that_action():
    # The anchored-parsing rail: "ban" appears inside "banter" and the reply says
    # "no violation", so a substring test would ban a member for a joke.
    result, emitter, actor, _, _, _ = await _run(
        _message_context(joined_days_ago=1.0),
        reply=(
            "ACTION: none\n"
            "REASON: this is banter about a ban in another game, no violation"
        ),
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert emitter.messages == []


async def test_unparseable_reply_fails_closed():
    result, emitter, actor, _, _, _ = await _run(
        _message_context(joined_days_ago=1.0),
        reply="I think you should probably ban this person, honestly.",
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert emitter.messages == []


async def test_unknown_action_value_fails_closed():
    result, _, actor, _, _, _ = await _run(
        _message_context(joined_days_ago=1.0),
        reply="ACTION: banish\nREASON: made-up verb",
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []


async def test_action_outside_allowed_actions_is_downgraded_to_a_report():
    # The example config permits none, warn and timeout — a ban verdict must
    # become a mod-log line and nothing else.
    result, emitter, actor, _, _, recorder = await _run(
        _message_context(joined_days_ago=1.0),
        reply="ACTION: ban\nREASON: crypto spam from a day-old account",
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert recorder.calls == []
    content = _mod_log(emitter)
    assert "**no action**" in content
    assert "not permitted" in content
    assert "crypto spam from a day-old account" in content


async def test_none_is_offered_even_when_the_guild_left_it_out():
    # "actions I permit" reads as "warn,timeout" to plenty of admins. The list
    # IS the model's menu, so without a seeded "none" the model would have no
    # way to say "nothing to do" and every watched message would get an action.
    config = {**MANIFEST.example_config, "allowed_actions": "warn,timeout"}
    result, emitter, actor, agent, _, recorder = await _run(
        _message_context(joined_days_ago=1.0),
        reply="ACTION: none\nREASON: an ordinary introduction",
        config=config,
    )
    assert result.outcome == "ok", result.error
    prompt = agent.prompts[0]
    assert "PERMITTED ACTIONS — choose exactly one: none, warn, timeout" in prompt
    assert "ACTION: <one of none | warn | timeout>" in prompt
    # A clean verdict stays silent everywhere, and nothing was applied.
    assert actor.calls == []
    assert recorder.calls == []
    assert emitter.messages == []


async def test_ban_on_an_established_member_is_withheld_and_only_reported():
    # The prompt ASKS for proportionality; this is the rail. A three-year member
    # the model misreads as a scammer is reported, never banned.
    config = {**MANIFEST.example_config, "allowed_actions": "none,warn,ban"}
    result, emitter, actor, _, _, recorder = await _run(
        _message_context(joined_days_ago=1100.0, days_since_last_message=200),
        reply="ACTION: ban\nREASON: looks like a scam link",
        config=config,
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert recorder.calls == []
    content = _mod_log(emitter)
    assert "**no action**" in content
    assert "ban withheld from an established member" in content


async def test_ban_still_applies_to_a_young_freshly_joined_account():
    # The other side of the same rail: young account, joined days ago, permitted
    # — the ban the guild asked for is applied.
    config = {**MANIFEST.example_config, "allowed_actions": "none,warn,ban"}
    result, emitter, actor, _, _, _ = await _run(
        _message_context(joined_days_ago=1.0),
        reply="ACTION: ban\nREASON: crypto spam",
        config=config,
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == [("ban", _AUTHOR, "crypto spam")]
    assert "**banned**" in _mod_log(emitter)


async def test_empty_allowed_actions_degrades_to_report_only():
    config = {**MANIFEST.example_config, "allowed_actions": "  , ,"}
    result, emitter, actor, agent, _, _ = await _run(
        _message_context(joined_days_ago=1.0),
        reply="ACTION: timeout\nREASON: spam",
        config=config,
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert "**no action**" in _mod_log(emitter)
    # The agent is told the truth about what it may choose.
    assert "PERMITTED ACTIONS — choose exactly one: none" in agent.prompts[0]


# -- applying the one action ---------------------------------------------------


async def test_permitted_timeout_is_applied_once_and_reported_once():
    result, emitter, actor, _, reader, _ = await _run(
        _message_context(joined_days_ago=1.0),
        reply="ACTION: timeout\nREASON: invite-link spam",
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == [("timeout", _AUTHOR, 600)]
    content = _mod_log(emitter)
    assert "New member flagged" in content
    assert "**timed out** for 600s" in content
    assert "invite-link spam" in content
    assert "Message id: 999888777666555444" in content
    # The whole cost story for a watched fire.
    assert result.usage["lookups"] == 1
    assert result.usage["agent_calls"] == 1
    assert result.usage["mod_actions"] == 1
    assert result.usage["messages_sent"] == 1


async def test_permitted_warn_reports_the_authoritative_count():
    result, emitter, actor, _, _, recorder = await _run(
        _message_context(joined_days_ago=1.0),
        reply="ACTION: warn\nREASON: read the rules please",
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert recorder.calls == [(_AUTHOR, "read the rules please", _HOME_CHANNEL)]
    content = _mod_log(emitter)
    assert "**warned** (total warns: 1)" in content
    # The warn's own public notice lands in the trigger channel, the report in
    # the mod-log — two messages, one action.
    assert any(channel == _HOME_CHANNEL for channel, _ in emitter.messages)


async def test_returning_member_report_is_labelled_as_such():
    result, emitter, _, _, _, _ = await _run(
        _message_context(joined_days_ago=700.0, days_since_last_message=200),
        reply="ACTION: timeout\nREASON: scam link",
    )
    assert result.outcome == "ok", result.error
    assert "Returning member flagged" in _mod_log(emitter)


async def test_report_links_to_the_message_when_it_lives_in_a_thread():
    result, emitter, _, _, _, _ = await _run(
        _message_context(joined_days_ago=1.0, thread_id="444333222111000999"),
        reply="ACTION: timeout\nREASON: scam link",
    )
    assert result.outcome == "ok", result.error
    assert (
        "https://discord.com/channels/G1/444333222111000999/999888777666555444"
        in _mod_log(emitter)
    )
