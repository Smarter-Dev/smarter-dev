"""Tests for the creation pipeline orchestration (author -> lint -> judge).

Author and judge are injected, so these run with no model calls.
"""

from __future__ import annotations

from smarter_dev.bot.agents.handler_authoring import (
    HandlerPlan,
    JudgeVerdict,
    describe_trigger,
    run_creation_pipeline,
    script_uses_agent,
)

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
    plan = _plan(action="edit", target_handler_id="99999999-9999-9999-9999-999999999999")
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
        settings={"interval_seconds": 300},
        existing_handlers=[],
        author=_author_returning(
            _plan(
                trigger_type="schedule",
                settings={"interval_seconds": 300},
                script='await send_message("0,1,1,2")\n',
            )
        ),
        judge=judge,
    )
    assert "every 5 minutes" in seen["ctx"]


def test_describe_trigger_cadence_phrasing():
    assert "EVERY user message" in describe_trigger("message", {})
    assert "EVERY user reaction" in describe_trigger("reaction", {})
    assert describe_trigger("schedule", {"interval_seconds": 3600}) == (
        "Recurring forever: fires every 1 hour."
    )
    assert "daily at 08:00" in describe_trigger("schedule", {"daily_time": "08:00"})
    assert "One-shot" in describe_trigger("timer", {"delay_seconds": 120})


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

from smarter_dev.bot.agents.handler_authoring import (
    AdminHandlerPlan,
    run_admin_creation_pipeline,
)

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


# -- checklist verdict + dual-judge merge --------------------------------------

from smarter_dev.bot.agents.handler_authoring import (
    checklist_failures,
    strictest_verdict,
)


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
