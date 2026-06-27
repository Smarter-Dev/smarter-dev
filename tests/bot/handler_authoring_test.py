"""Tests for the creation pipeline orchestration (author -> lint -> judge).

Author and judge are injected, so these run with no model calls.
"""

from __future__ import annotations

from smarter_dev.bot.agents.handler_authoring import (
    JudgeVerdict,
    describe_trigger,
    run_creation_pipeline,
    script_uses_agent,
)

GOOD_SCRIPT = 'if "huzzah" in context["message_content"].lower():\n    await add_reaction(context["message_id"], "🎉")\n'


def _author_returning(text):
    async def author(**kwargs):
        return text

    return author


def _judge_approving():
    async def judge(script, trigger_context):
        return JudgeVerdict(approved=True, reason="reacts once on a keyword, within caps")

    return judge


def _judge_rejecting(reason):
    async def judge(script, trigger_context):
        return JudgeVerdict(approved=False, reason=reason)

    return judge


async def test_happy_path_approves():
    result = await run_creation_pipeline(
        description="react with tada on huzzah",
        trigger_type="message",
        settings={},
        author=_author_returning(GOOD_SCRIPT),
        judge=_judge_approving(),
    )
    assert result.ok
    assert "add_reaction" in result.script


async def test_author_error_is_relayed():
    result = await run_creation_pipeline(
        description="say hi 100 times",
        trigger_type="message",
        settings={},
        author=_author_returning("ERROR: cannot exceed the 3-message cap"),
        judge=_judge_approving(),
    )
    assert not result.ok
    assert "3-message cap" in result.error


async def test_judge_reject_is_relayed():
    result = await run_creation_pipeline(
        description="whatever",
        trigger_type="message",
        settings={},
        author=_author_returning(GOOD_SCRIPT),
        judge=_judge_rejecting("sends in a loop, over the 3-message cap"),
    )
    assert not result.ok
    assert "loop" in result.error


async def test_lint_blocks_opaque_blob_before_judge():
    judged = []

    async def judge(script, trigger_context):
        judged.append(script)
        return JudgeVerdict(approved=True, reason="ok")

    blob = "QUJDREVG" * 30
    result = await run_creation_pipeline(
        description="sneaky",
        trigger_type="message",
        settings={},
        author=_author_returning(f'data = "{blob}"\nawait send_message(data)\n'),
        judge=judge,
    )
    assert not result.ok
    assert "opaque" in result.error
    assert judged == []  # judge never ran — lint stopped it first


async def test_code_fences_are_stripped():
    fenced = "```python\n" + GOOD_SCRIPT + "```"
    result = await run_creation_pipeline(
        description="x",
        trigger_type="message",
        settings={},
        author=_author_returning(fenced),
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
        return JudgeVerdict(approved=True, reason="ok")

    await run_creation_pipeline(
        description="post fib",
        trigger_type="schedule",
        settings={"interval_seconds": 300},
        author=_author_returning('await send_message("0,1,1,2")\n'),
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


def _admin_author_returning(plan):
    async def author(*, request, channel_lister):
        return plan

    return author


async def test_admin_pipeline_happy():
    plan = AdminHandlerPlan(
        feasible=True, trigger_type="message", channel_ids=[], settings={},
        script=ADMIN_SCRIPT,
    )
    result = await run_admin_creation_pipeline(
        request="watch for scams and ban them",
        author=_admin_author_returning(plan),
        judge=_judge_approving(),
    )
    assert result.ok
    assert result.trigger_type == "message"
    assert result.channel_ids == []
    assert "ban_user" in result.script


async def test_admin_pipeline_not_feasible():
    plan = AdminHandlerPlan(feasible=False, error="needs 100 bans/sec, over the cap")
    result = await run_admin_creation_pipeline(
        request="impossible", author=_admin_author_returning(plan), judge=_judge_approving()
    )
    assert not result.ok
    assert "over the cap" in result.error


async def test_admin_pipeline_lint_blocks_blob_before_judge():
    judged = []

    async def judge(script, ctx):
        judged.append(script)
        return JudgeVerdict(approved=True, reason="ok")

    blob = "QUJDREVG" * 30
    plan = AdminHandlerPlan(feasible=True, trigger_type="message", script=f'x = "{blob}"\n')
    result = await run_admin_creation_pipeline(
        request="sneaky", author=_admin_author_returning(plan), judge=judge
    )
    assert not result.ok
    assert "opaque" in result.error
    assert judged == []


async def test_admin_pipeline_judge_reject():
    plan = AdminHandlerPlan(feasible=True, trigger_type="message", script=ADMIN_SCRIPT)
    result = await run_admin_creation_pipeline(
        request="x", author=_admin_author_returning(plan),
        judge=_judge_rejecting("bans every author with no condition"),
    )
    assert not result.ok
    assert "no condition" in result.error
