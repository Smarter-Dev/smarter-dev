"""Tests for the static safety lint (opaque-blob ban)."""

from __future__ import annotations

from smarter_dev.web.handler_lint import (
    MAX_SCRIPT_BYTES,
    check_static,
    compiles,
    lint_script,
)


def test_clean_script_passes_static():
    assert check_static('await send_message("hi")\n') is None


def test_rejects_base64_blob():
    blob = "QUJDREVG" * 30  # long, base64-ish, no spaces
    reason = check_static(f'data = "{blob}"\n')
    assert reason and "opaque" in reason


def test_rejects_hex_blob():
    blob = "deadbeef" * 30
    reason = check_static(f'data = "{blob}"\n')
    assert reason and "opaque" in reason


def test_rejects_dynamic_execution():
    assert "exec" in (check_static('exec("x")\n') or "")
    assert "eval" in (check_static('eval("1")\n') or "")
    assert "__import__" in (check_static('__import__("os")\n') or "")


def test_rejects_over_length():
    big = 'x = "' + ("a " * MAX_SCRIPT_BYTES) + '"\n'
    reason = check_static(big)
    assert reason and "byte limit" in reason


def test_normal_long_prose_string_is_not_opaque():
    # A long message with spaces reads plainly — not a blob.
    msg = "good morning everyone " * 20
    assert check_static(f'await send_message("{msg}")\n') is None


def test_compiles_detects_syntax_error():
    assert compiles("def (:\n") is not None
    assert compiles('await send_message("hi")\n') is None


def test_lint_script_combines_checks():
    assert lint_script('await send_message("hi")\n') is None
    assert lint_script('eval("1")\n') is not None


def test_rejects_defined_but_never_called_function():
    # The classic no-op: everything in main(), never invoked.
    noop = "async def main():\n    await send_message('hi')\n"
    reason = check_static(noop)
    assert reason and "never calls it" in reason


def test_allows_function_that_is_called():
    ok = "async def run():\n    await send_message('hi')\nawait run()\n"
    assert check_static(ok) is None


def test_allows_plain_toplevel_script():
    assert check_static('await send_message("hi")\n') is None


def test_rejects_hardcoded_delete_thread_target():
    # A delete target must come from trigger context or a list_threads result —
    # a hardcoded id literal is an unreviewable destructive action.
    reason = check_static('await delete_thread("123456789012345678")\n')
    assert reason and "delete_thread" in reason


def test_allows_delete_thread_from_context():
    ok = 'await delete_thread(context["thread_id"])\n'
    assert check_static(ok) is None


def test_allows_delete_thread_over_list_threads_result():
    script = (
        "for t in await list_threads(context['parent_channel_id']):\n"
        "    if t['archived']:\n"
        "        await delete_thread(t['thread_id'])\n"
    )
    assert check_static(script) is None


def test_add_role_with_literal_role_id_ok():
    ok = 'await add_role(context["member_id"], "888160821673349140")\n'
    assert check_static(ok) is None


def test_remove_role_with_literal_role_id_ok():
    ok = "await remove_role(context['payload']['user_id'], '644325811301777426')\n"
    assert check_static(ok) is None


def test_add_role_with_variable_role_id_rejected():
    reason = check_static('await add_role(context["member_id"], role_id)\n')
    assert reason and "role id" in reason


def test_remove_role_with_fstring_role_id_rejected():
    reason = check_static('await remove_role(context["member_id"], f"{role}")\n')
    assert reason and "role id" in reason


def test_add_role_with_subscript_role_id_rejected():
    reason = check_static('await add_role(context["member_id"], roles["holding"])\n')
    assert reason and "role id" in reason


# -- text prefix commands (prohibited under Discord's message-content-intent policy)


def _prefix_command_reason(snippet: str) -> str | None:
    return check_static(f"{snippet}\n    await send_message('hi')\n")


def test_rejects_startswith_command_word():
    reason = _prefix_command_reason('if text.startswith("!ping"):')
    assert reason and "leading command word" in reason


def test_rejects_equality_against_command_word():
    assert _prefix_command_reason("if word == '!sus':")
    assert _prefix_command_reason('if text.split()[0] == "?help":')
    assert _prefix_command_reason('if "!ping" == word:')


def test_rejects_inverted_equality_against_command_word():
    # The first line of the deleted bump_commands.monty: a guard that returns
    # unless the message IS the command word.
    assert _prefix_command_reason('if text != "!bumpers" and text != "!bumps":')
    assert _prefix_command_reason('if "!bumps"!=text:')


def test_rejects_membership_in_command_word_collection():
    assert _prefix_command_reason('if text in ("!sus", "!list_sus"):')
    assert _prefix_command_reason('if text in ["!sus"]:')
    assert _prefix_command_reason('if word in (\n    "!a",\n    "?b",\n):')


def test_allows_comparisons_that_are_not_command_words():
    assert _prefix_command_reason('if reply != "ok":') is None
    assert _prefix_command_reason('if "ok"!=reply:') is None
    assert _prefix_command_reason('if word == "hello":') is None
    assert _prefix_command_reason('if text in ("yes", "no"):') is None


def test_allows_keyword_anywhere_in_message():
    # Matching a word anywhere in the body is a keyword watch, not a command.
    assert _prefix_command_reason('if "?help" in text:') is None
    assert _prefix_command_reason('if "raid" in context["message_content"]:') is None


def test_allows_exclamation_that_is_not_a_command_word():
    assert _prefix_command_reason('await send_message("!! raid alert")') is None
    assert _prefix_command_reason('if "!" in text:') is None


def test_lint_script_rejects_a_prefix_command_before_compile():
    script = (
        'text = context["message_content"].strip().lower()\n'
        'if text != "!bumpers" and text != "!bumps":\n'
        "    pass\n"
    )
    reason = lint_script(script)
    assert reason and "prefix commands are prohibited" in reason
