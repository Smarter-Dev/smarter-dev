"""No new handler may punish members (#239)."""

from __future__ import annotations

import pytest

from smarter_dev.web.handler_moderation_policy import moderation_refusal
from smarter_dev.web.handler_moderation_policy import sanction_calls


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
        # A mention in a string or a comment is not a use.
        ('await send_message("no ban_user here")  # warn_user\n', set()),
        ('await add_role(context["author_id"], "R1")\n', set()),
        # A script that does not parse is read strictly.
        ("ban_user(\n", {"ban_user"}),
    ],
)
def test_sanction_uses_are_found(script, found):
    assert sanction_calls(script) == found


def test_a_rewrite_keeps_existing_sanctions_but_adds_none():
    live = 'await ban_user(context["author_id"], "scam")\n'
    assert (
        moderation_refusal(live.replace("scam", "confirmed scam"), existing_script=live)
        is None
    )
    added = live + 'await warn_user(context["author_id"], "x")\n'
    assert "(warn_user)" in moderation_refusal(added, existing_script=live)
    assert "(ban_user)" in moderation_refusal(live)
