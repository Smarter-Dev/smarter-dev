"""No handler may punish members (#239, #242): removed functions refuse a save."""

from __future__ import annotations

import pytest

from smarter_dev.web.handler_moderation_policy import REMOVED_FUNCTIONS
from smarter_dev.web.handler_moderation_policy import moderation_refusal
from smarter_dev.web.handler_moderation_policy import removed_calls

LIVE = 'if "nitro" in context["content"]:\n    await ban_user(context["author_id"], "scam")\n'


@pytest.mark.parametrize(
    ("script", "found"),
    [
        ('await ban_user(context["author_id"], "scam")\n', {"ban_user"}),
        # Aliased, indexed or passed along: the same external function.
        ('f = ban_user\nawait f(context["author_id"])\n', {"ban_user"}),
        ('await [timeout_user][0](context["author_id"], 60)\n', {"timeout_user"}),
        (
            "async def apply(action):\n    await action('1')\n\nawait apply(kick_user)\n",
            {"kick_user"},
        ),
        # The destructive non-sanction functions went with them.
        ('await delete_thread(context["thread_id"])\n', {"delete_thread"}),
        ('await lock_thread(context["thread_id"])\n', {"lock_thread"}),
        ('await delete_webhook("https://discord.com/api/webhooks/1/x")\n', {"delete_webhook"}),
        # A mention in a string or a comment is not a use.
        ('await send_message("no ban_user here")  # warn_user\n', set()),
        ('await add_role(context["author_id"], "R1")\n', set()),
        ('await report_for_review(context["member_id"], "rejoined")\n', set()),
        # Other spellings of the same identifier, as the runtime reads them.
        ('await ｂan_user(context["author_id"], "scam")\n', {"ban_user"}),
        ('await 𝐛𝐚𝐧_user(context["author_id"], "scam")\n', {"ban_user"}),
        ('﻿await ban_user(context["author_id"], "scam")\n', {"ban_user"}),
        # Fullwidth quotes stay inside their strings: normalising the whole
        # text would turn them into a triple quote around the real call.
        ('a = "＂"; await ban_user(context["author_id"]); b = "＂"\n', {"ban_user"}),
    ],
)
def test_removed_function_uses_are_found(script, found):
    assert removed_calls(script) == found


def test_every_removed_function_is_refused_by_name():
    for name in sorted(REMOVED_FUNCTIONS):
        reason = moderation_refusal(f'await {name}("1")\n')
        assert reason is not None and f"no longer use {name}:" in reason


@pytest.mark.parametrize(
    "script",
    [
        # #255's reproduction: Monty accepts the unparenthesised except, so
        # the script runs; Python 3.13 cannot read it, so it is refused.
        'try:\n    await ｂan_user(context["author_id"], "scam")\n'
        "except ValueError, TypeError:\n    pass\n",
        "ban_user(\n",
        # Not even a removed function in sight: still refused.
        "except ValueError, TypeError:\n    pass\n",
    ],
)
def test_a_script_python_cannot_parse_is_refused(script):
    reason = moderation_refusal(script)
    assert reason is not None and "not valid Python" in reason


def test_a_script_using_a_sanction_is_refused_with_a_plain_message():
    reason = moderation_refusal(LIVE)
    assert reason == (
        "handlers can no longer use ban_user: bans, kicks, timeouts, holds, "
        "warnings and removing members' messages are decided by the server's "
        "moderation system. A handler may still post, react, close and reopen "
        "threads, delete bots' messages, change non-staff roles, and hand a "
        "member to the moderators with report_for_review"
    )


def test_several_removed_functions_are_listed_sorted():
    script = 'await warn_user("1", "x")\nawait ban_user("1")\n'
    assert "use ban_user, warn_user:" in moderation_refusal(script)


def test_a_script_without_one_may_be_saved():
    assert moderation_refusal('await send_message("hi", "C1")\n') is None
    assert moderation_refusal(None) is None
    assert moderation_refusal("") is None
