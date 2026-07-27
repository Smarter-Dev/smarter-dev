"""Tests for the keyword-watch catalog extension.

Two layers, matching the house pattern for catalog extensions:

1. Render/lint layer — the manifest renders against its example_config, the
   rendered script passes ``handler_lint`` (including with an ``agent_instruction``
   at the config layer's 500-char maximum, since every free-text field is inlined
   into the script and the lint cap is 8 KB), and each config value is baked in
   as a typed literal.
2. Behaviour layer — the rendered Monty script runs in the real handler runtime
   with a stubbed emitter/actor/agent over the whole decision block: the cheap
   keyword gate and its word-boundary semantics, the safety guards, anchored
   parsing of the agent reply, and the allowed_actions re-validation.

The cost invariant the extension claims is asserted directly: an unmatched
message spends NOTHING (no lookup, no agent call, no emit), and a matched one is
bounded at one lookup, one agent call, at most one moderation action, and one
mod-log message.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from smarter_dev.extensions.catalog.keyword_watch import MANIFEST
from smarter_dev.extensions.rendering import RenderedHandler
from smarter_dev.extensions.rendering import RenderError
from smarter_dev.extensions.rendering import render_bundle
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_runtime import run_handler_script

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / (
    "smarter_dev/extensions/catalog/keyword_watch"
)
_MOD_LOG_CHANNEL = "123456789012345678"
_HOME_CHANNEL = "222222222222222222"
_HANDLER_KEY = "keyword-watch"
_AUTHOR = "555000111222333444"
_MESSAGE = "999888777666555444"


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
    content: str,
    *,
    account_days_ago: float = 900.0,
    joined_days_ago: float | None = 400.0,
    is_bot: bool = False,
    manage_messages: bool = False,
    is_first_message: bool = False,
    thread_id: str | None = None,
) -> dict:
    context = {
        "trigger_type": "message",
        "guild_id": "G1",
        "message_content": content,
        "message_id": _MESSAGE,
        "author_id": _AUTHOR,
        "author_name": "ada",
        "author_is_bot": is_bot,
        "author_account_created_at": _iso(account_days_ago),
        "author_joined_at": None if joined_days_ago is None else _iso(joined_days_ago),
        "author_role_ids": [],
        "author_has_manage_messages": manage_messages,
        "mentioned_user_ids": [],
        "mentioned_role_ids": [],
        "mentions_everyone": False,
        "channel_parent_id": None,
        "attachments": [],
        "embeds": [],
        "interaction_user_id": None,
        "is_thread": thread_id is not None,
        # Enrichment from the dispatcher, read BEFORE this message is recorded.
        "author_is_first_message": is_first_message,
        "author_days_since_last_message": None if is_first_message else 1,
        "author_last_message_at": None if is_first_message else _iso(1),
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
    dm_delivers: bool = True,
    warn_count: int = 1,
):
    emitter = _Emitter(dm_delivers=dm_delivers)
    actor = _Actor()
    agent = _Agent(reply=reply)
    reader = _ModActionReader(rows=rows or [])
    recorder = _ModActionRecorder(warn_count=warn_count)
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
    posted = [
        content for channel, content in emitter.messages if channel == _MOD_LOG_CHANNEL
    ]
    assert len(posted) == 1, emitter.messages
    return posted[0]


# -- render / manifest layer ---------------------------------------------------


def test_manifest_shape():
    assert MANIFEST.slug == "keyword-watch"
    assert [handler.key for handler in MANIFEST.handlers] == [_HANDLER_KEY]
    handler = MANIFEST.handlers[0]
    assert handler.trigger_type == "message"
    # Guild-wide: watched terms are not channel-specific, and the keyword gate is
    # what makes guild-wide affordable.
    assert handler.channel_scope == []
    # No role grants, and no bot-message opt-in — a bot is never a target here.
    assert handler.settings == {}


def test_config_schema():
    fields = {field.name: field for field in MANIFEST.config}
    assert set(fields) == {
        "keywords",
        "agent_instruction",
        "allowed_actions",
        "mod_log_channel_id",
        "timeout_seconds",
    }
    assert fields["mod_log_channel_id"].type == "channel_id"
    assert fields["keywords"].required is True
    assert fields["agent_instruction"].required is True
    # There is no list config type, so both the term list and the action set are
    # comma-separated strings the script normalizes itself.
    assert fields["keywords"].type == "string"
    assert fields["allowed_actions"].type == "string"
    assert fields["timeout_seconds"].type == "int"
    assert fields["timeout_seconds"].default == 600


def test_example_config_renders_and_lints_clean():
    rendered = _rendered()
    assert lint_script(rendered.script) is None
    assert rendered.channel_ids == []
    assert f'MOD_LOG_CHANNEL_ID = "{_MOD_LOG_CHANNEL}"' in rendered.script
    assert "TIMEOUT_SECONDS = 600" in rendered.script
    assert '"free nitro, steam gift, crypto giveaway"' in rendered.script


def _prose(length: int) -> str:
    """Realistic filler: a repeated blob of ``x`` trips the lint's opaque-blob rail."""
    return ("flag scam bait and phishing lures aimed at members here. " * 12)[:length]


def test_longest_possible_instruction_still_fits_the_lint_cap():
    # agent_instruction is inlined into the script, so the 8 KB handler-lint cap
    # has to hold at the config layer's own 500-char string maximum — otherwise a
    # guild writing a long instruction would fail at install time, not here.
    config = dict(MANIFEST.example_config)
    config["agent_instruction"] = _prose(500)
    assert lint_script(_rendered(config).script) is None


def test_oversized_free_text_is_refused_at_render_not_at_runtime():
    # The documented size budget: all three free-text fields at their 500-char
    # maximum overflow the 8 KB script cap. That must surface as a RenderError
    # from render_bundle — before any handler row is written — never as a fire
    # that errors in production.
    config = dict(MANIFEST.example_config)
    config["keywords"] = _prose(500)
    config["agent_instruction"] = _prose(500)
    config["allowed_actions"] = _prose(500)
    with pytest.raises(RenderError, match="8192-byte limit"):
        render_bundle(MANIFEST, config, _scripts())


def test_non_snowflake_channel_is_rejected_at_render():
    with pytest.raises(RenderError, match="mod_log_channel_id"):
        render_bundle(
            MANIFEST,
            {**MANIFEST.example_config, "mod_log_channel_id": "#mod-log"},
            _scripts(),
        )


# -- the cheap keyword gate ----------------------------------------------------


async def test_ordinary_chatter_spends_nothing():
    # The whole point of a guild-wide message handler: the common path is a pair
    # of bool tests and a string compare, and never reaches a lookup, an agent
    # call, or an emit.
    result, emitter, actor, agent, reader, _ = await _run(
        _message_context("morning everyone, how's the deploy going?")
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
    # Checked before the term scan: a bot cannot be moderated by this handler, so
    # it must not even pay for the match.
    result, _, _, agent, reader, _ = await _run(
        _message_context("free nitro here", is_bot=True)
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []
    assert reader.calls == []


async def test_member_with_moderation_powers_is_exempt():
    # Matches /warn's refusal to warn moderators — checked before any spend.
    result, _, _, agent, reader, _ = await _run(
        _message_context("free nitro here", manage_messages=True)
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []
    assert reader.calls == []


async def test_matching_is_word_boundary_not_substring():
    # The rejected alternative, asserted: a substring test fires "class" inside
    # "classic" and "ban" inside "banter", and every false hit costs a lookup, an
    # agent call, and an admin's attention.
    config = {**MANIFEST.example_config, "keywords": "class, ban"}
    result, _, _, agent, _, _ = await _run(
        _message_context("that classic banter about subclassing"), config=config
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []


async def test_a_whole_word_hit_is_case_and_punctuation_insensitive():
    config = {**MANIFEST.example_config, "keywords": "class, ban"}
    result, _, _, agent, _, _ = await _run(
        _message_context("is this a **CLASS**, or what?"), config=config
    )
    assert result.outcome == "ok", result.error
    assert len(agent.prompts) == 1


async def test_a_multi_word_term_must_match_as_a_contiguous_phrase():
    result, _, _, agent, _, _ = await _run(
        _message_context("this is free, and here is some nitro for your car")
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []


async def test_a_multi_word_term_matches_across_punctuation():
    result, _, _, agent, _, _ = await _run(_message_context("FREE NITRO!!! claim now"))
    assert result.outcome == "ok", result.error
    assert len(agent.prompts) == 1


async def test_empty_entries_in_the_term_list_match_nothing():
    # A trailing comma (or a list that is only separators) must not become a term
    # that matches every message — that would agent-call the whole guild.
    config = {**MANIFEST.example_config, "keywords": " , ,, "}
    result, _, _, agent, _, _ = await _run(
        _message_context("just talking about nothing in particular"), config=config
    )
    assert result.outcome == "ok", result.error
    assert agent.prompts == []


# -- the facts handed to the agent ---------------------------------------------


async def test_the_prompt_carries_the_instruction_facts_and_contract():
    result, _, _, agent, reader, _ = await _run(
        _message_context(
            "get your free nitro here", account_days_ago=3.0, joined_days_ago=2.0
        ),
        rows=[
            {
                "action_type": "warn",
                "reason": "spam",
                "source": "manual",
                "moderator_username": "mod",
                "duration_seconds": None,
                "channel_id": None,
                "trigger_message_id": None,
                "created_at": "2026-05-04T18:30:00+00:00",
            }
        ],
    )
    assert result.outcome == "ok", result.error
    prompt = agent.prompts[0]
    # The guild's own instruction leads.
    assert "Flag scam and phishing bait" in prompt
    # The matched term, the two ages, the prior history, and the permitted set.
    assert "free nitro" in prompt
    assert "account 3d old" in prompt
    assert "here 2d" in prompt
    assert "prior mod actions newest first: warn (1+)" in prompt
    assert "Permitted: none, warn, timeout." in prompt
    # The exact output contract and the proportionality guidance.
    assert "ACTION: <one of none | warn | timeout | kick | ban>" in prompt
    assert "PROPORTIONALITY" in prompt
    # The content is fenced and named untrusted.
    assert "UNTRUSTED" in prompt
    assert "<<<MESSAGE" in prompt
    # Exactly one lookup, at the documented depth.
    assert reader.calls == [(_AUTHOR, 5)]


async def test_an_uncached_join_date_reads_as_unknown_not_as_brand_new():
    result, _, _, agent, _, _ = await _run(
        _message_context("free nitro", joined_days_ago=None)
    )
    assert result.outcome == "ok", result.error
    assert "here unknown" in agent.prompts[0]


# -- parsing + re-validation ---------------------------------------------------


async def test_a_clean_verdict_takes_no_action_but_is_still_reported():
    # The documented choice: "none" stays silent in the channel and lands one
    # mod-log line, because the no-action lines are how an admin tunes the terms.
    result, emitter, actor, _, _, recorder = await _run(
        _message_context("free nitro is a common scam, be careful"),
        reply="ACTION: none\nREASON: warning others about the scam",
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert recorder.calls == []
    content = _mod_log(emitter)
    assert "**Action:** none" in content
    assert "warning others about the scam" in content
    # Silent in the channel the message came from.
    assert all(channel == _MOD_LOG_CHANNEL for channel, _ in emitter.messages)


async def test_prose_naming_a_forbidden_action_is_not_that_action():
    # The anchored-parsing rail: "ban" appears inside "banter", and a substring
    # test would ban a member over a joke about bans.
    result, _, actor, _, _, _ = await _run(
        _message_context("free nitro"),
        reply=(
            "ACTION: none\n"
            "REASON: this is banter about a ban in another game, no violation"
        ),
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []


async def test_an_unparseable_reply_fails_closed():
    result, emitter, actor, _, _, _ = await _run(
        _message_context("free nitro"),
        reply="I think you should probably ban this person, honestly.",
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert "**Action:** none" in _mod_log(emitter)


async def test_an_unknown_action_value_fails_closed():
    result, _, actor, _, _, _ = await _run(
        _message_context("free nitro"),
        reply="ACTION: banish\nREASON: made-up verb",
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []


async def test_an_action_outside_allowed_actions_is_downgraded_to_a_report():
    # The example config permits none, warn and timeout — a kick verdict must
    # become a mod-log line and nothing else. Re-validating AFTER parsing is the
    # rail: the prompt asked for a permitted action, but a prompt is not a rail.
    result, emitter, actor, _, _, recorder = await _run(
        _message_context("free nitro"),
        reply="ACTION: kick\nREASON: scam link from a throwaway",
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert recorder.calls == []
    content = _mod_log(emitter)
    assert "**Action:** none — agent said kick, which is not permitted here" in content
    assert "scam link from a throwaway" in content


async def test_a_term_list_that_permits_nothing_degrades_to_report_only():
    config = {**MANIFEST.example_config, "allowed_actions": "  , ,"}
    result, emitter, actor, agent, _, _ = await _run(
        _message_context("free nitro"),
        reply="ACTION: timeout\nREASON: scam",
        config=config,
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert "**Action:** none" in _mod_log(emitter)
    # The agent is told the truth about what it may choose.
    assert "Permitted: none." in agent.prompts[0]


# -- applying the one action ---------------------------------------------------


async def test_a_permitted_timeout_is_applied_once_and_reported_once():
    result, emitter, actor, _, _, _ = await _run(
        _message_context("free nitro, click here"),
        reply="ACTION: timeout\nREASON: invite-link spam",
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == [("timeout", _AUTHOR, 600)]
    content = _mod_log(emitter)
    assert "**Keyword Watch**" in content
    assert "free nitro" in content
    assert "**Action:** timeout" in content
    assert "invite-link spam" in content
    assert "message " + _MESSAGE in content
    # The whole cost story for a matched fire.
    assert result.usage["lookups"] == 1
    assert result.usage["agent_calls"] == 1
    assert result.usage["mod_actions"] == 1
    assert result.usage["messages_sent"] == 1


async def test_a_permitted_warn_reports_the_authoritative_count():
    result, emitter, actor, _, _, recorder = await _run(
        _message_context("free nitro"),
        reply="ACTION: warn\nREASON: read the rules please",
        warn_count=3,
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    # The recorder owns the guild binding and the username resolution; the script
    # passes only the id, the reason, and the trigger channel.
    assert recorder.calls == [(_AUTHOR, "read the rules please", _HOME_CHANNEL)]
    assert "**Action:** warn — warn #3" in _mod_log(emitter)
    # warn_user's own public notice lands in the trigger channel: two messages,
    # one moderation action.
    assert any(channel == _HOME_CHANNEL for channel, _ in emitter.messages)


async def test_closed_dms_are_reported_not_treated_as_a_failure():
    result, emitter, _, _, _, _ = await _run(
        _message_context("free nitro"),
        reply="ACTION: warn\nREASON: scam bait",
        dm_delivers=False,
    )
    assert result.outcome == "ok", result.error
    assert "(DMs closed)" in _mod_log(emitter)


async def test_a_ban_is_withheld_from_an_established_member():
    # Proportionality as a RAIL, not just prompt guidance: even with ban
    # permitted, a two-year-old account that has been here a year is reported.
    config = {**MANIFEST.example_config, "allowed_actions": "none, warn, ban"}
    result, emitter, actor, _, _, _ = await _run(
        _message_context("free nitro", account_days_ago=900.0, joined_days_ago=400.0),
        reply="ACTION: ban\nREASON: scam link",
        config=config,
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert "ban withheld from an established member" in _mod_log(emitter)


async def test_a_ban_applies_to_a_young_just_arrived_account():
    config = {**MANIFEST.example_config, "allowed_actions": "none, warn, ban"}
    result, emitter, actor, _, _, _ = await _run(
        _message_context(
            "free nitro",
            account_days_ago=2.0,
            joined_days_ago=1.0,
            is_first_message=True,
        ),
        reply="ACTION: ban\nREASON: scam link on a day-old account",
        config=config,
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == [("ban", _AUTHOR, "scam link on a day-old account")]
    assert "**Action:** ban" in _mod_log(emitter)


async def test_an_unknown_account_age_never_qualifies_for_a_ban():
    # "We don't know when they joined" is not "they just joined": an uncached
    # join date must count against the ban, never for it.
    config = {**MANIFEST.example_config, "allowed_actions": "none, ban"}
    result, emitter, actor, _, _, _ = await _run(
        _message_context("free nitro", account_days_ago=2.0, joined_days_ago=None),
        reply="ACTION: ban\nREASON: scam link",
        config=config,
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert "ban withheld" in _mod_log(emitter)


async def test_a_permitted_kick_is_applied():
    config = {**MANIFEST.example_config, "allowed_actions": "none, kick"}
    result, _, actor, _, _, _ = await _run(
        _message_context("free nitro"),
        reply="ACTION: kick\nREASON: scam link",
        config=config,
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == [("kick", _AUTHOR, None)]


async def test_the_report_links_to_the_message_when_it_lives_in_a_thread():
    result, emitter, _, _, _, _ = await _run(
        _message_context("free nitro", thread_id="444333222111000999"),
        reply="ACTION: timeout\nREASON: scam link",
    )
    assert result.outcome == "ok", result.error
    assert (
        f"https://discord.com/channels/G1/444333222111000999/{_MESSAGE}"
        in _mod_log(emitter)
    )
