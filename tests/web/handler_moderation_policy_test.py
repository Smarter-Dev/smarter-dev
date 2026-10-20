"""No new handler may punish members (#239)."""

from __future__ import annotations

from pathlib import Path

import pytest

from smarter_dev.web.handler_moderation_policy import SCAM_BANNER_REFERENCE_SHA256
from smarter_dev.web.handler_moderation_policy import moderation_refusal
from smarter_dev.web.handler_moderation_policy import sanction_calls
from smarter_dev.web.handler_moderation_policy import script_sha256

REFERENCE = (
    Path(__file__).resolve().parents[2] / "scripts/handler_scripts/scam-banner.monty"
).read_text()
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
        # A mention in a string or a comment is not a use.
        ('await send_message("no ban_user here")  # warn_user\n', set()),
        ('await add_role(context["author_id"], "R1")\n', set()),
        # Other spellings of the same identifier, as the runtime reads them.
        ('await ｂan_user(context["author_id"], "scam")\n', {"ban_user"}),
        ('await 𝐛𝐚𝐧_user(context["author_id"], "scam")\n', {"ban_user"}),
        ('\ufeffawait ban_user(context["author_id"], "scam")\n', {"ban_user"}),
        # Fullwidth quotes stay inside their strings: normalising the whole
        # text would turn them into a triple quote around the real call.
        ('a = "＂"; await ban_user(context["author_id"]); b = "＂"\n', {"ban_user"}),
    ],
)
def test_sanction_uses_are_found(script, found):
    assert sanction_calls(script) == found


@pytest.mark.parametrize(
    "script",
    [
        # #255's reproduction: Monty accepts the unparenthesised except, so
        # the script runs; Python 3.13 cannot read it, so it is refused.
        'try:\n    await ｂan_user(context["author_id"], "scam")\n'
        "except ValueError, TypeError:\n    pass\n",
        "ban_user(\n",
        # Not even a sanction in sight: still refused, nothing can vouch for it.
        "except ValueError, TypeError:\n    pass\n",
    ],
)
def test_a_script_python_cannot_parse_is_refused(script):
    reason = moderation_refusal(script)
    assert reason is not None and "not valid Python" in reason
    assert moderation_refusal(script, existing_script=script) == reason


def test_a_new_script_may_not_sanction():
    assert "(ban_user)" in moderation_refusal(LIVE)
    assert moderation_refusal('await send_message("hi", "C1")\n') is None


@pytest.mark.parametrize(
    "rewrite",
    [
        # #255's reproductions: each keeps or adds a ban call site.
        LIVE.replace('"nitro" in context["content"]', '"hello" in context["content"]'),
        LIVE + '    await ban_user(context["author_id"], "again")\n',
        LIVE.replace('"scam"', '"confirmed scam"'),
        'await kick_user(context["author_id"], "every message")\n',
    ],
)
def test_a_live_punisher_is_frozen(rewrite):
    reason = moderation_refusal(
        rewrite, existing_script=LIVE, handler_name="keyword-watch"
    )
    assert reason is not None and "can no longer be changed" in reason


def test_a_live_punisher_may_be_saved_unchanged_or_disarmed():
    assert moderation_refusal(LIVE, existing_script=LIVE) is None
    disarmed = (
        'if "nitro" in context["content"]:\n    await send_message("scam?", "MODS")\n'
    )
    assert moderation_refusal(disarmed, existing_script=LIVE) is None


def test_scam_banner_may_be_put_back_to_its_reference_copy_only():
    assert script_sha256(REFERENCE) == SCAM_BANNER_REFERENCE_SHA256
    assert (
        moderation_refusal(REFERENCE, existing_script=LIVE, handler_name="scam-banner")
        is None
    )
    # Another handler, a new handler, or one character off: refused.
    assert moderation_refusal(
        REFERENCE, existing_script=LIVE, handler_name="keyword-watch"
    )
    assert moderation_refusal(REFERENCE)
    assert moderation_refusal(
        REFERENCE + "\n", existing_script=LIVE, handler_name="scam-banner"
    )
