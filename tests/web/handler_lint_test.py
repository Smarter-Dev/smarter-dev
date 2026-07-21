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
