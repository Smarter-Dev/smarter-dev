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
