"""Tests for the creation pipeline orchestration (author -> lint -> judge).

Author and judge are injected, so these run with no model calls.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openai import OpenAIResponsesModel

from smarter_dev.bot.agents import handler_authoring
from smarter_dev.bot.agents.handler_authoring import AdminHandlerPlan
from smarter_dev.bot.agents.handler_authoring import HandlerPlan
from smarter_dev.bot.agents.handler_authoring import JudgeVerdict
from smarter_dev.bot.agents.handler_authoring import _HANDLER_THINKING
from smarter_dev.bot.agents.handler_authoring import _admin_judge_models
from smarter_dev.bot.agents.handler_authoring import _build_admin_author_prompt
from smarter_dev.bot.agents.handler_authoring import _build_configured_model
from smarter_dev.bot.agents.handler_authoring import _handler_model_settings
from smarter_dev.shared.config import Settings
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.bot.agents.handler_authoring import _build_author_prompt
from smarter_dev.bot.agents.handler_authoring import checklist_failures
from smarter_dev.bot.agents.handler_authoring import describe_trigger
from smarter_dev.bot.agents.handler_authoring import run_admin_creation_pipeline
from smarter_dev.bot.agents.handler_authoring import run_creation_pipeline
from smarter_dev.bot.agents.handler_authoring import script_uses_agent
from smarter_dev.bot.agents.handler_authoring import strictest_verdict

GOOD_SCRIPT = 'if "huzzah" in context["message_content"].lower():\n    await add_reaction(context["message_id"], "🎉")\n'

EXISTING = [
    {
        "handler_id": "11111111-1111-1111-1111-111111111111",
        "name": "greeter",
        "trigger_type": "message",
        "settings": {},
        "description": "greets newcomers",
        "script": 'await send_message("welcome")\n',
    },
    {
        "handler_id": "22222222-2222-2222-2222-222222222222",
        "name": "daily-digest",
        "trigger_type": "schedule",
        "settings": {"daily_time": "08:00"},
        "description": "posts a daily digest",
        "script": 'await send_message("digest")\n',
    },
]

NOW_UTC = datetime(2026, 7, 22, 18, 30, tzinfo=UTC)


def _plan(**over):
    fields = {
        "feasible": True,
        "action": "create",
        "name": "huzzah-reactor",
        "trigger_type": "message",
        "settings": {},
        "script": GOOD_SCRIPT,
    }
    fields.update(over)
    return HandlerPlan(**fields)


def _author_returning(plan):
    async def author(**kwargs):
        return plan

    return author


def _verdict(approved=True, reason="ok", **overrides):
    """A checklist verdict with every category passing unless overridden."""
    fields = {
        "sandbox_valid": True,
        "within_limits": True,
        "memory_bounded": True,
        "guards_effective": True,
        "agent_verdict_safe": True,
        "actions_appropriate": True,
        "transparent": True,
        "schedule_reasonable": True,
        "approved": approved,
        "reason": reason,
    }
    fields.update(overrides)
    return JudgeVerdict(**fields)


def _judge_approving():
    async def judge(script, trigger_context):
        return _verdict(reason="reacts once on a keyword, within caps")

    return judge


def _judge_rejecting(reason):
    async def judge(script, trigger_context):
        return _verdict(approved=False, reason=reason)

    return judge


async def test_create_happy_path():
    result = await run_creation_pipeline(
        request="react with tada on huzzah",
        trigger_type="message",
        settings={},
        existing_handlers=EXISTING,
        author=_author_returning(_plan()),
        judge=_judge_approving(),
    )
    assert result.ok
    assert result.action == "create"
    assert result.name == "huzzah-reactor"
    assert result.trigger_type == "message"
    assert "add_reaction" in result.script


async def test_author_sees_existing_handlers():
    seen = {}

    async def author(**kwargs):
        seen.update(kwargs)
        return _plan()

    await run_creation_pipeline(
        request="x",
        trigger_type="message",
        settings={},
        existing_handlers=EXISTING,
        author=author,
        judge=_judge_approving(),
    )
    assert seen["existing_handlers"] == EXISTING


async def test_edit_targets_existing_handler():
    plan = _plan(
        action="edit",
        target_handler_id="11111111-1111-1111-1111-111111111111",
        name="",
        script='await send_message("welcome, adventurer")\n',
    )
    result = await run_creation_pipeline(
        request="make the greeter more whimsical",
        trigger_type="message",
        settings={},
        existing_handlers=EXISTING,
        author=_author_returning(plan),
        judge=_judge_approving(),
    )
    assert result.ok
    assert result.action == "edit"
    assert result.target_handler_id == "11111111-1111-1111-1111-111111111111"
    # Trigger comes from the target, not the plan.
    assert result.trigger_type == "message"


async def test_edit_of_unknown_target_is_rejected():
    plan = _plan(
        action="edit", target_handler_id="99999999-9999-9999-9999-999999999999"
    )
    result = await run_creation_pipeline(
        request="x",
        trigger_type="message",
        settings={},
        existing_handlers=EXISTING,
        author=_author_returning(plan),
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "unknown handler" in result.error


async def test_edit_uses_target_trigger_and_plan_settings():
    plan = _plan(
        action="edit",
        target_handler_id="22222222-2222-2222-2222-222222222222",
        trigger_type="message",  # author restates wrongly; the target wins
        settings={"daily_time": "09:30"},
        script='await send_message("digest v2")\n',
    )
    result = await run_creation_pipeline(
        request="move the digest to 9:30",
        trigger_type="schedule",
        settings={},
        existing_handlers=EXISTING,
        author=_author_returning(plan),
        judge=_judge_approving(),
    )
    assert result.ok
    assert result.trigger_type == "schedule"
    assert result.settings == {"daily_time": "09:30"}


async def test_create_with_taken_name_is_rejected():
    result = await run_creation_pipeline(
        request="x",
        trigger_type="message",
        settings={},
        existing_handlers=EXISTING,
        author=_author_returning(_plan(name="Greeter")),  # case-insensitive clash
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "already" in result.error


async def test_create_without_name_is_rejected():
    result = await run_creation_pipeline(
        request="x",
        trigger_type="message",
        settings={},
        existing_handlers=[],
        author=_author_returning(_plan(name="  ")),
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "name" in result.error


async def test_author_infeasible_is_relayed():
    plan = HandlerPlan(feasible=False, error="cannot exceed the 3-message cap")
    result = await run_creation_pipeline(
        request="say hi 100 times",
        trigger_type="message",
        settings={},
        existing_handlers=[],
        author=_author_returning(plan),
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "3-message cap" in result.error


async def test_judge_reject_is_relayed():
    result = await run_creation_pipeline(
        request="whatever",
        trigger_type="message",
        settings={},
        existing_handlers=[],
        author=_author_returning(_plan()),
        judge=_judge_rejecting("sends in a loop, over the 3-message cap"),
    )
    assert not result.ok
    assert "loop" in result.error


async def test_lint_blocks_opaque_blob_before_judge():
    judged = []

    async def judge(script, trigger_context):
        judged.append(script)
        return _verdict(reason="ok")

    blob = "QUJDREVG" * 30
    result = await run_creation_pipeline(
        request="sneaky",
        trigger_type="message",
        settings={},
        existing_handlers=[],
        author=_author_returning(
            _plan(script=f'data = "{blob}"\nawait send_message(data)\n')
        ),
        judge=judge,
    )
    assert not result.ok
    assert "opaque" in result.error
    assert judged == []  # judge never ran — lint stopped it first


async def test_code_fences_are_stripped():
    result = await run_creation_pipeline(
        request="x",
        trigger_type="message",
        settings={},
        existing_handlers=[],
        author=_author_returning(_plan(script="```python\n" + GOOD_SCRIPT + "```")),
        judge=_judge_approving(),
    )
    assert result.ok
    assert not result.script.startswith("```")


def test_script_uses_agent_detection():
    assert script_uses_agent('await spawn_agent("x")\n')
    assert not script_uses_agent('await send_message("x")\n')


async def test_judge_receives_trigger_cadence():
    seen = {}

    async def judge(script, trigger_context):
        seen["ctx"] = trigger_context
        return _verdict(reason="ok")

    await run_creation_pipeline(
        request="post fib",
        trigger_type="schedule",
        settings={"interval_seconds": 300, "start_at": "2026-07-22T19:00:00Z"},
        existing_handlers=[],
        author=_author_returning(
            _plan(
                trigger_type="schedule",
                settings={
                    "interval_seconds": 300,
                    "start_at": "2026-07-22T19:00:00Z",
                },
                script='await send_message("0,1,1,2")\n',
            )
        ),
        judge=judge,
        now_utc=NOW_UTC,
    )
    assert "every 5 minutes" in seen["ctx"]
    assert NOW_UTC.isoformat() in seen["ctx"]
    assert "Candidate action: create" in seen["ctx"]
    assert '"start_at": "2026-07-22T19:00:00Z"' in seen["ctx"]
    assert "post fib" in seen["ctx"]


def test_author_prompts_receive_trusted_current_utc():
    standard = _build_author_prompt("post later", "schedule", {}, [], NOW_UTC)
    admin = _build_admin_author_prompt("post later", [], NOW_UTC)
    assert f"Trusted current UTC date and time: {NOW_UTC.isoformat()}" in standard
    assert f"Trusted current UTC date and time: {NOW_UTC.isoformat()}" in admin


async def test_schedule_reasonable_failure_blocks_installation():
    async def judge(script, trigger_context):
        return _verdict(
            approved=True,
            schedule_reasonable=False,
            reason="the requested start is implausibly far in the future",
        )

    result = await run_creation_pipeline(
        request="post soon",
        trigger_type="schedule",
        settings={},
        existing_handlers=[],
        author=_author_returning(
            _plan(
                trigger_type="schedule",
                settings={
                    "interval_seconds": 300,
                    "start_at": "2099-01-01T00:00:00Z",
                },
                script='await send_message("later")\n',
            )
        ),
        judge=judge,
        now_utc=NOW_UTC,
    )
    assert not result.ok
    assert "schedule_reasonable" in result.error


def test_describe_trigger_dm_message():
    line = describe_trigger("dm_message", {})
    assert "EVERY DM" in line
    assert "user-controlled" in line
    assert "untrusted" in line


def test_describe_trigger_message_edit():
    line = describe_trigger("message_edit", {})
    assert "EVERY message edit" in line
    assert "high frequency" in line
    assert "evasion" in line
    assert "old_content" in line


def test_describe_trigger_mod_action():
    line = describe_trigger("mod_action", {})
    # Not the generic fallback.
    assert line != "Trigger: mod_action."
    assert "moderation action" in line
    # Documents the loop rail (0 mod-action budget).
    assert "0 moderation-action budget" in line


def test_describe_trigger_cadence_phrasing():
    assert "EVERY user message" in describe_trigger("message", {})
    assert "EVERY user reaction" in describe_trigger("reaction", {})
    assert describe_trigger("schedule", {"interval_seconds": 3600}) == (
        "Recurring forever: fires every 1 hour."
    )
    assert "daily at 08:00" in describe_trigger("schedule", {"daily_time": "08:00"})
    assert "One-shot" in describe_trigger("timer", {"delay_seconds": 120})


def test_describe_trigger_timer_mentions_self_rearm():
    # The timer cadence line must surface schedule_timer self re-arm and the
    # timer-context branch, since ANY trigger can receive a self-armed re-fire.
    for settings in (
        {"delay_seconds": 120},
        {"fire_at": "2026-06-27T08:00:00+00:00"},
        {},
    ):
        copy = describe_trigger("timer", settings)
        assert "schedule_timer" in copy
        assert "timer context" in copy


def test_describe_trigger_member_and_thread_cadence():
    join = describe_trigger("member_join", {})
    assert "EVERY member join" in join and "raid" in join.lower()
    leave = describe_trigger("member_leave", {})
    assert "EVERY member leave" in leave and "ban wave" in leave.lower()
    rules = describe_trigger("member_rules_accepted", {})
    assert "idempotent" in rules
    role = describe_trigger("member_role_change", {})
    assert "actually change" in role and "low frequency" in role.lower()
    thread = describe_trigger("thread_create", {})
    assert "EVERY new thread/post" in thread


# -- admin creation pipeline ---------------------------------------------------

ADMIN_SCRIPT = (
    'if context.get("author_joined_at"):\n'
    '    report = await spawn_agent("scam?", has_tools=False)\n'
    '    if "scam" in report.lower():\n'
    '        await ban_user(context["author_id"], "scam")\n'
    '        await send_message("banned a scammer", "MODCHAT")\n'
)

ADMIN_EXISTING = [
    {
        "handler_id": "33333333-3333-3333-3333-333333333333",
        "name": "scam-banner",
        "trigger_type": "message",
        "settings": {},
        "channel_ids": [],
        "description": "bans scammers",
        "script": ADMIN_SCRIPT,
    },
]


def _admin_plan(**over):
    fields = {
        "feasible": True,
        "action": "create",
        "name": "raid-alarm",
        "trigger_type": "message",
        "channel_ids": [],
        "settings": {},
        "script": ADMIN_SCRIPT,
    }
    fields.update(over)
    return AdminHandlerPlan(**fields)


def _admin_author_returning(plan):
    async def author(*, request, existing_handlers, channel_lister):
        return plan

    return author


async def test_admin_pipeline_create():
    result = await run_admin_creation_pipeline(
        request="watch for scams and ban them",
        existing_handlers=ADMIN_EXISTING,
        author=_admin_author_returning(_admin_plan()),
        judge=_judge_approving(),
    )
    assert result.ok
    assert result.action == "create"
    assert result.name == "raid-alarm"
    assert result.trigger_type == "message"
    assert "ban_user" in result.script


async def test_admin_pipeline_edit():
    plan = _admin_plan(
        action="edit",
        target_handler_id="33333333-3333-3333-3333-333333333333",
        name="",
    )
    result = await run_admin_creation_pipeline(
        request="also check attachments",
        existing_handlers=ADMIN_EXISTING,
        author=_admin_author_returning(plan),
        judge=_judge_approving(),
    )
    assert result.ok
    assert result.action == "edit"
    assert result.target_handler_id == "33333333-3333-3333-3333-333333333333"


async def test_admin_pipeline_edit_unknown_target():
    plan = _admin_plan(action="edit", target_handler_id="not-a-real-id")
    result = await run_admin_creation_pipeline(
        request="x",
        existing_handlers=ADMIN_EXISTING,
        author=_admin_author_returning(plan),
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "unknown handler" in result.error


async def test_admin_pipeline_create_taken_name():
    result = await run_admin_creation_pipeline(
        request="x",
        existing_handlers=ADMIN_EXISTING,
        author=_admin_author_returning(_admin_plan(name="scam-banner")),
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "already" in result.error


async def test_admin_pipeline_not_feasible():
    plan = AdminHandlerPlan(feasible=False, error="needs 100 bans/sec, over the cap")
    result = await run_admin_creation_pipeline(
        request="impossible",
        existing_handlers=[],
        author=_admin_author_returning(plan),
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "over the cap" in result.error


async def test_admin_pipeline_lint_blocks_blob_before_judge():
    judged = []

    async def judge(script, ctx):
        judged.append(script)
        return _verdict(reason="ok")

    blob = "QUJDREVG" * 30
    result = await run_admin_creation_pipeline(
        request="sneaky",
        existing_handlers=[],
        author=_admin_author_returning(_admin_plan(script=f'x = "{blob}"\n')),
        judge=judge,
    )
    assert not result.ok
    assert "opaque" in result.error
    assert judged == []


async def test_admin_pipeline_judge_reject():
    result = await run_admin_creation_pipeline(
        request="x",
        existing_handlers=[],
        author=_admin_author_returning(_admin_plan()),
        judge=_judge_rejecting("bans every author with no condition"),
    )
    assert not result.ok
    assert "no condition" in result.error


async def test_admin_pipeline_accepts_member_trigger():
    # The admin vocabulary includes the five member/thread triggers; a
    # member_join create plan is accepted and its cadence reaches the judge.
    seen = {}

    async def judge(script, trigger_context):
        seen["ctx"] = trigger_context
        return _verdict(reason="ok")

    result = await run_admin_creation_pipeline(
        request="alert mods when someone joins",
        existing_handlers=[],
        author=_admin_author_returning(
            _admin_plan(
                trigger_type="member_join",
                name="join-alert",
                script='await send_message("welcome", "MODCHAT")\n',
            )
        ),
        judge=judge,
    )
    assert result.ok
    assert result.trigger_type == "member_join"
    assert "EVERY member join" in seen["ctx"]


async def test_admin_schedule_reviewer_receives_current_utc_and_start():
    seen = {}

    async def judge(script, trigger_context):
        seen["ctx"] = trigger_context
        return _verdict(reason="ok")

    result = await run_admin_creation_pipeline(
        request="post an hourly audit starting tonight",
        existing_handlers=[],
        author=_admin_author_returning(
            _admin_plan(
                trigger_type="schedule",
                name="hourly-audit",
                settings={
                    "interval_seconds": 3600,
                    "start_at": "2026-07-22T23:00:00+00:00",
                },
                script='await send_message("audit", "MODCHAT")\n',
            )
        ),
        judge=judge,
        now_utc=NOW_UTC,
    )
    assert result.ok
    assert NOW_UTC.isoformat() in seen["ctx"]
    assert "2026-07-22T23:00:00+00:00" in seen["ctx"]
    assert "post an hourly audit starting tonight" in seen["ctx"]


async def test_admin_pipeline_accepts_message_edit_trigger():
    # message_edit is in the admin vocabulary; a create plan on it is accepted
    # and its edit-frequency cadence reaches the judge.
    seen = {}

    async def judge(script, trigger_context):
        seen["ctx"] = trigger_context
        return _verdict(reason="ok")

    result = await run_admin_creation_pipeline(
        request="delete edits that add @everyone",
        existing_handlers=[],
        author=_admin_author_returning(
            _admin_plan(
                trigger_type="message_edit",
                name="edit-ping-catch",
                script=(
                    'if "@everyone" in context["message_content"]:\n'
                    '    await delete_message(context["message_id"])\n'
                ),
            )
        ),
        judge=judge,
    )
    assert result.ok
    assert result.trigger_type == "message_edit"
    assert "EVERY message edit" in seen["ctx"]


async def test_standard_pipeline_rejects_admin_only_trigger():
    # Even if the standard author mistakenly emits an admin-only trigger, the
    # standard pipeline rejects it — the vocabulary stays the four base types.
    result = await run_creation_pipeline(
        request="alert on join",
        trigger_type="message",
        settings={},
        existing_handlers=[],
        author=_author_returning(_plan(trigger_type="member_join")),
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "invalid trigger" in result.error


# -- admin fix round -----------------------------------------------------------

CLEAN_ADMIN_SCRIPT = (
    'if context["message_content"].startswith("!raid"):\n'
    '    await send_message("raid reported", "MODCHAT")\n'
)

LINT_BLOB_SCRIPT = 'x = "{}"\n'.format("QUJDREVG" * 30)

OVERLONG_ADMIN_SCRIPT = CLEAN_ADMIN_SCRIPT + "# padding past the byte cap\n" * 400


def _admin_author_drafting(*plans):
    """An author returning each plan in turn; a call past the last one fails."""
    requests = []

    async def author(*, request, existing_handlers, channel_lister):
        requests.append(request)
        return plans[len(requests) - 1]

    return author, requests


def _judge_returning(*verdicts):
    """A judge returning each verdict in turn, recording its trigger contexts."""
    contexts = []

    async def judge(script, trigger_context):
        contexts.append(trigger_context)
        return verdicts[len(contexts) - 1]

    return judge, contexts


def _progress_recorder():
    messages = []

    async def progress(text):
        messages.append(text)

    return progress, messages


async def test_admin_rejected_draft_goes_back_to_the_author_once():
    first = _admin_plan(script=ADMIN_SCRIPT)
    second = _admin_plan(script=CLEAN_ADMIN_SCRIPT)
    author, requests = _admin_author_drafting(first, second)
    judge, _ = _judge_returning(
        _verdict(approved=False, reason="bans every author with no condition"),
        _verdict(reason="guarded and within caps"),
    )
    progress, messages = _progress_recorder()

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=judge,
        progress=progress,
    )
    assert result.ok
    assert result.script == CLEAN_ADMIN_SCRIPT
    assert len(requests) == 2
    # The revision carries the first draft's script and the rejection detail.
    assert "ban_user" in requests[1]
    assert "no condition" in requests[1]
    assert "alert mods about raids" in requests[1]
    assert any("no condition" in m for m in messages)


async def test_admin_fix_round_relays_the_lint_reason():
    author, requests = _admin_author_drafting(
        _admin_plan(script=LINT_BLOB_SCRIPT),
        _admin_plan(script=CLEAN_ADMIN_SCRIPT),
    )
    progress, messages = _progress_recorder()

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=_judge_approving(),
        progress=progress,
    )
    assert result.ok
    assert len(requests) == 2
    assert "opaque" in requests[1]
    assert any("opaque" in m for m in messages)


async def test_admin_fix_round_relays_a_resolution_error():
    author, requests = _admin_author_drafting(
        _admin_plan(name="scam-banner"),
        _admin_plan(name="raid-alarm", script=CLEAN_ADMIN_SCRIPT),
    )

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=_judge_approving(),
    )
    assert result.ok
    assert result.name == "raid-alarm"
    assert len(requests) == 2
    assert "already exists" in requests[1]


async def test_admin_fix_round_relays_a_schedule_error():
    author, requests = _admin_author_drafting(
        _admin_plan(
            trigger_type="schedule",
            name="hourly-audit",
            settings={"interval_seconds": 30},
            script='await send_message("audit", "MODCHAT")\n',
        ),
        _admin_plan(
            trigger_type="schedule",
            name="hourly-audit",
            settings={"interval_seconds": 3600},
            script='await send_message("audit", "MODCHAT")\n',
        ),
    )

    result = await run_admin_creation_pipeline(
        request="post an audit every so often",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=_judge_approving(),
    )
    assert result.ok
    assert result.settings == {"interval_seconds": 3600}
    assert len(requests) == 2
    assert "interval_seconds must be at least 60" in requests[1]


async def test_admin_second_rejection_fails_with_the_second_verdict():
    author, requests = _admin_author_drafting(
        _admin_plan(), _admin_plan(script=CLEAN_ADMIN_SCRIPT)
    )
    judge, _ = _judge_returning(
        _verdict(approved=False, reason="bans every author with no condition"),
        _verdict(approved=False, reason="still reports on every single message"),
    )

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=judge,
    )
    assert not result.ok
    assert "still reports on every single message" in result.error
    assert "after a round of fixes" in result.error
    assert len(requests) == 2


async def test_admin_infeasible_first_draft_skips_the_fix_round():
    author, requests = _admin_author_drafting(
        AdminHandlerPlan(feasible=False, error="needs 100 bans/sec, over the cap")
    )

    result = await run_admin_creation_pipeline(
        request="impossible",
        existing_handlers=[],
        author=author,
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "over the cap" in result.error
    assert len(requests) == 1


async def test_admin_infeasible_revision_is_terminal():
    author, requests = _admin_author_drafting(
        _admin_plan(),
        AdminHandlerPlan(feasible=False, error="can't guard this without a user id"),
    )

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=_judge_rejecting("bans every author with no condition"),
    )
    assert not result.ok
    assert "can't guard this without a user id" in result.error
    assert "after a round of fixes" in result.error
    assert len(requests) == 2


async def test_admin_lint_failure_and_judge_rejection_get_separate_rounds():
    # The over-length draft spends the mechanical retry; the judge rejection on
    # the revision still has the untouched semantic round to spend.
    author, requests = _admin_author_drafting(
        _admin_plan(script=OVERLONG_ADMIN_SCRIPT),
        _admin_plan(script=ADMIN_SCRIPT),
        _admin_plan(script=CLEAN_ADMIN_SCRIPT),
    )
    judge, _ = _judge_returning(
        _verdict(approved=False, reason="bans every author with no condition"),
        _verdict(reason="guarded and within caps"),
    )
    progress, messages = _progress_recorder()

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=judge,
        progress=progress,
    )
    assert result.ok
    assert result.script == CLEAN_ADMIN_SCRIPT
    assert len(requests) == 3
    assert any("8192-byte limit" in text for text in messages)
    assert any("no condition" in text for text in messages)


async def test_admin_judge_rejection_then_lint_failure_get_separate_rounds():
    author, requests = _admin_author_drafting(
        _admin_plan(script=ADMIN_SCRIPT),
        _admin_plan(script=OVERLONG_ADMIN_SCRIPT),
        _admin_plan(script=CLEAN_ADMIN_SCRIPT),
    )
    judge, _ = _judge_returning(
        _verdict(approved=False, reason="bans every author with no condition"),
        _verdict(reason="guarded and within caps"),
    )

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=judge,
    )
    assert result.ok
    assert result.script == CLEAN_ADMIN_SCRIPT
    assert len(requests) == 3


async def test_admin_second_lint_failure_is_final():
    author, requests = _admin_author_drafting(
        _admin_plan(script=LINT_BLOB_SCRIPT),
        _admin_plan(script=OVERLONG_ADMIN_SCRIPT),
    )

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "8192-byte limit" in result.error
    assert "after a round of fixes" in result.error
    assert len(requests) == 2


async def test_admin_pipeline_ends_once_both_budgets_are_spent():
    author, requests = _admin_author_drafting(
        _admin_plan(script=LINT_BLOB_SCRIPT),
        _admin_plan(script=ADMIN_SCRIPT),
        _admin_plan(script=OVERLONG_ADMIN_SCRIPT),
    )
    judge, _ = _judge_returning(
        _verdict(approved=False, reason="bans every author with no condition")
    )

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=judge,
    )
    assert not result.ok
    assert "8192-byte limit" in result.error
    assert "after a round of fixes" in result.error
    assert len(requests) == 3


async def test_admin_resolution_error_spends_the_semantic_budget():
    author, requests = _admin_author_drafting(
        _admin_plan(name="scam-banner"),
        _admin_plan(script=ADMIN_SCRIPT),
    )
    judge, _ = _judge_returning(
        _verdict(approved=False, reason="bans every author with no condition")
    )

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=judge,
    )
    assert not result.ok
    assert "no condition" in result.error
    assert "after a round of fixes" in result.error
    assert len(requests) == 2


async def test_admin_schedule_error_spends_the_semantic_budget():
    author, requests = _admin_author_drafting(
        _admin_plan(
            trigger_type="schedule",
            name="hourly-audit",
            settings={"interval_seconds": 30},
            script='await send_message("audit", "MODCHAT")\n',
        ),
        _admin_plan(
            trigger_type="schedule",
            name="hourly-audit",
            settings={"interval_seconds": 3600},
            script='await send_message("audit", "MODCHAT")\n',
        ),
    )
    judge, _ = _judge_returning(
        _verdict(approved=False, reason="posts an audit nobody reads")
    )

    result = await run_admin_creation_pipeline(
        request="post an audit every so often",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=judge,
    )
    assert not result.ok
    assert "posts an audit nobody reads" in result.error
    assert "after a round of fixes" in result.error
    assert len(requests) == 2


async def test_admin_judge_reviews_against_the_original_request_on_both_rounds():
    author, requests = _admin_author_drafting(
        _admin_plan(), _admin_plan(script=CLEAN_ADMIN_SCRIPT)
    )
    judge, contexts = _judge_returning(
        _verdict(approved=False, reason="bans every author with no condition"),
        _verdict(reason="guarded and within caps"),
    )

    result = await run_admin_creation_pipeline(
        request="alert mods about raids",
        existing_handlers=ADMIN_EXISTING,
        author=author,
        judge=judge,
    )
    assert result.ok
    assert len(contexts) == 2
    for context in contexts:
        assert "alert mods about raids" in context
        assert "REVISION" not in context
        assert "no condition" not in context


# -- checklist verdict + dual-judge merge --------------------------------------


def test_checklist_failures_names_failing_categories():
    verdict = _verdict(memory_bounded=False, agent_verdict_safe=False)
    assert checklist_failures(verdict) == ["memory_bounded", "agent_verdict_safe"]
    assert checklist_failures(_verdict()) == []


async def test_checklist_failure_forces_rejection_despite_approval():
    async def judge(script, trigger_context):
        # Judge says approved overall but flags unbounded memory — the
        # checklist wins.
        return _verdict(approved=True, reason="looks fine", memory_bounded=False)

    result = await run_creation_pipeline(
        request="x",
        trigger_type="message",
        settings={},
        existing_handlers=[],
        author=_author_returning(_plan()),
        judge=judge,
    )
    assert not result.ok
    assert "memory_bounded" in result.error


async def test_admin_checklist_failure_forces_rejection():
    async def judge(script, trigger_context):
        return _verdict(approved=True, reason="solid", actions_appropriate=False)

    result = await run_admin_creation_pipeline(
        request="x",
        existing_handlers=[],
        author=_admin_author_returning(_admin_plan()),
        judge=judge,
    )
    assert not result.ok
    assert "actions_appropriate" in result.error


def test_strictest_verdict_picks_the_rejector():
    ok = _verdict(reason="fine")
    bad = _verdict(approved=False, reason="bans everyone")
    assert strictest_verdict([ok, bad]).reason == "bans everyone"
    assert strictest_verdict([bad, ok]).reason == "bans everyone"
    # A checklist failure counts as a rejection even with approved=True.
    sneaky = _verdict(approved=True, reason="lgtm", guards_effective=False)
    assert strictest_verdict([ok, sneaky]).reason == "lgtm"
    # All passing: first verdict wins.
    assert strictest_verdict([ok, _verdict(reason="also fine")]).reason == "fine"


# -- role-grant authoring rails (E2) -------------------------------------------

_ROLE_GRANT_SCRIPT = (
    'if context["trigger_type"] == "member_rules_accepted":\n'
    '    await add_role(context["member_id"], "888160821673349140", reason="onboard")\n'
)


async def test_pipeline_rejects_role_grant_with_nonliteral_role():
    """A non-literal role id is caught by lint before the judge ever runs."""
    judged = []

    async def judge(script, trigger_context):
        judged.append(script)
        return _verdict()

    plan = _admin_plan(
        trigger_type="member_rules_accepted",
        settings={"allowed_role_ids": ["888160821673349140"]},
        script='await add_role(context["member_id"], role_id)\n',
    )
    result = await run_admin_creation_pipeline(
        request="give the holding role on rules acceptance",
        existing_handlers=ADMIN_EXISTING,
        author=_admin_author_returning(plan),
        judge=judge,
    )
    assert not result.ok
    assert "lint" in result.error and "role id" in result.error
    assert judged == []  # lint stopped it before the judge


async def test_judge_rejects_unconditional_add_role_on_join():
    """A literal role id passes lint, so the judge is the gate for an
    unconditional grant on a raid-frequency trigger."""
    plan = _admin_plan(
        trigger_type="member_join",
        settings={"allowed_role_ids": ["888160821673349140"]},
        script='await add_role(context["member_id"], "888160821673349140")\n',
    )
    result = await run_admin_creation_pipeline(
        request="give everyone the holding role when they join",
        existing_handlers=ADMIN_EXISTING,
        author=_admin_author_returning(plan),
        judge=_judge_rejecting("unconditional role grant on member_join"),
    )
    assert not result.ok
    assert "unconditional role grant" in result.error


# -- schedule_timer authoring rails (E3) ---------------------------------------

_TIMER_NO_BRANCH_SCRIPT = (
    'await add_role(context["member_id"], "888160821673349140", reason="sus")\n'
    'await schedule_timer(86400, {"user_id": context["member_id"]})\n'
)

_TIMER_WITH_BRANCH_SCRIPT = (
    'if context["trigger_type"] == "timer":\n'
    '    await remove_role(context["payload"]["user_id"], "888160821673349140")\n'
    "else:\n"
    '    await add_role(context["member_id"], "888160821673349140", reason="sus")\n'
    '    await schedule_timer(86400, {"user_id": context["member_id"]})\n'
)


async def test_judge_rejects_schedule_timer_without_timer_branch():
    # A script that arms a timer but never handles the timer re-fire is a
    # guaranteed error on every re-fire; the judge (guards_effective) rejects it.
    plan = _admin_plan(
        trigger_type="message",
        settings={"allowed_role_ids": ["888160821673349140"]},
        script=_TIMER_NO_BRANCH_SCRIPT,
    )
    result = await run_admin_creation_pipeline(
        request="sus a member and remove the role a day later",
        existing_handlers=ADMIN_EXISTING,
        author=_admin_author_returning(plan),
        judge=_judge_rejecting(
            "arms schedule_timer but never handles the timer re-fire branch"
        ),
    )
    assert not result.ok
    assert "timer re-fire branch" in result.error


async def test_pipeline_accepts_schedule_timer_with_timer_branch():
    result = await run_admin_creation_pipeline(
        request="sus a member and remove the role a day later",
        existing_handlers=ADMIN_EXISTING,
        author=_admin_author_returning(
            _admin_plan(
                trigger_type="message",
                settings={"allowed_role_ids": ["888160821673349140"]},
                script=_TIMER_WITH_BRANCH_SCRIPT,
            )
        ),
        judge=_judge_approving(),
    )
    assert result.ok
    assert "schedule_timer" in result.script


async def test_pipeline_accepts_conditional_literal_role_grant():
    result = await run_admin_creation_pipeline(
        request="give the holding role on rules acceptance",
        existing_handlers=ADMIN_EXISTING,
        author=_admin_author_returning(
            _admin_plan(
                trigger_type="member_rules_accepted",
                settings={"allowed_role_ids": ["888160821673349140"]},
                script=_ROLE_GRANT_SCRIPT,
            )
        ),
        judge=_judge_approving(),
    )
    assert result.ok
    assert result.settings == {"allowed_role_ids": ["888160821673349140"]}
    assert "add_role" in result.script


# ---------------------------------------------------------------------------
# Admin author / judge model routing
# ---------------------------------------------------------------------------


def _settings_with(**overrides):
    """A stand-in for the global settings, defaulting every handler model field."""
    handler_model_fields = {
        name: field.default
        for name, field in Settings.model_fields.items()
        if name.startswith("handler_")
    }
    return SimpleNamespace(**{**handler_model_fields, **overrides})


def test_configured_model_builder_routes_catalog_ids_through_model_router(monkeypatch):
    # The admin author and second judge both default to Terra, which the catalog
    # serves via OpenAI — building it as a Google model would send a GPT id to
    # the Gemini API. Catalog wire ids must route through the shared router;
    # anything else is assumed to be a Gemini id.
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    terra = _build_configured_model("gpt-5.6-terra")
    assert isinstance(terra, OpenAIResponsesModel)

    luna = _build_configured_model("openai/gpt-5.6-luna")
    assert isinstance(luna, OpenAIChatModel)

    gemini = _build_configured_model("gemini-3-flash-preview")
    assert isinstance(gemini, GoogleModel)


def test_admin_model_defaults_are_the_stronger_pair():
    assert Settings.model_fields["handler_admin_author_model"].default == "gpt-5.6-terra"
    assert (
        Settings.model_fields["handler_admin_judge_model"].default == "gemini-3.7-flash"
    )
    assert (
        Settings.model_fields["handler_admin_second_judge_model"].default
        == "gpt-5.6-terra"
    )


def test_standard_tier_model_defaults_are_unchanged():
    assert (
        Settings.model_fields["handler_author_model"].default
        == "gemini-3-flash-preview"
    )
    assert (
        Settings.model_fields["handler_judge_model"].default == "gemini-3-flash-preview"
    )


def test_admin_judge_panel_defaults_to_gemini_then_terra(monkeypatch):
    monkeypatch.setattr(handler_authoring, "get_settings", _settings_with)
    assert _admin_judge_models() == ["gemini-3.7-flash", "gpt-5.6-terra"]


def test_admin_judge_panel_ignores_the_standard_tier_judge(monkeypatch):
    monkeypatch.setattr(
        handler_authoring,
        "get_settings",
        lambda: _settings_with(handler_judge_model="gemini-3-flash-preview"),
    )
    assert "gemini-3-flash-preview" not in _admin_judge_models()


def test_empty_second_judge_leaves_a_single_judge_panel(monkeypatch):
    monkeypatch.setattr(
        handler_authoring,
        "get_settings",
        lambda: _settings_with(handler_admin_second_judge_model=""),
    )
    assert _admin_judge_models() == ["gemini-3.7-flash"]


def test_duplicate_admin_judge_models_are_deduplicated(monkeypatch):
    monkeypatch.setattr(
        handler_authoring,
        "get_settings",
        lambda: _settings_with(
            handler_admin_judge_model="gpt-5.6-terra",
            handler_admin_second_judge_model="gpt-5.6-terra",
        ),
    )
    assert _admin_judge_models() == ["gpt-5.6-terra"]


def test_admin_author_agent_uses_the_admin_author_model(monkeypatch):
    # The cached agent is module-level; clear it so this test builds its own and
    # leaves no cache behind for the next one.
    monkeypatch.setattr(handler_authoring, "_admin_author_agent", None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(
        handler_authoring,
        "get_settings",
        lambda: _settings_with(handler_admin_author_model="gpt-5.6-terra"),
    )
    agent = handler_authoring._get_admin_author_agent()
    assert agent.model.model_name == "gpt-5.6-terra"


def test_handler_model_settings_uses_the_requested_reasoning_level():
    terra_high = _handler_model_settings("gpt-5.6-terra", ReasoningLevel.HIGH)
    assert terra_high["openai_reasoning_effort"] == "high"

    gemini_high = _handler_model_settings("gemini-3.7-flash", ReasoningLevel.HIGH)
    assert gemini_high["google_thinking_config"] == {"thinking_level": "HIGH"}

    terra_medium = _handler_model_settings("gpt-5.6-terra", ReasoningLevel.MEDIUM)
    assert terra_medium["openai_reasoning_effort"] == "medium"


def test_handler_model_settings_falls_back_for_non_catalog_ids():
    assert (
        _handler_model_settings("gemini-3-flash-preview", ReasoningLevel.HIGH)
        is _HANDLER_THINKING
    )
