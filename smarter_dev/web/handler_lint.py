"""Static safety lint for candidate handler scripts — the host-side opaque-blob ban.

The judge can only reason about code it can read. An opaque or encoded blob —
base64/hex payloads, a string to be decoded and run, anything whose purpose
can't be determined by reading it — defeats a static judge entirely. So this
runs *between* author and judge as defense-in-depth: it rejects encoded blobs,
dynamic-execution escape hatches, and over-length scripts before the judge even
sees the candidate. The author prompt and the judge are the other two layers.

Pure and dependency-light: the static checks need nothing but ``re`` (so they're
trivially testable); ``compiles`` additionally parses the script with Monty.
"""

from __future__ import annotations

import re

MAX_SCRIPT_BYTES = 8 * 1024

# Dynamic execution / import-machinery escape hatches. A handler is plain,
# readable logic over the provided external functions — none of this belongs.
_BANNED_TOKENS = (
    "exec",
    "eval",
    "compile",
    "__import__",
    "importlib",
    "marshal",
    "pickle",
    "codecs",
    "base64",
    "binascii",
    "fromhex",
    "b64decode",
    "b64encode",
    "globals",
    "builtins",
    "getattr",
    "setattr",
)

# A string literal's inner content; good enough to flag encoded blobs.
_STRING_LITERAL = re.compile(
    r"'''(?P<a>.*?)'''|\"\"\"(?P<b>.*?)\"\"\"|'(?P<c>(?:\\.|[^'\\])*)'|\"(?P<d>(?:\\.|[^\"\\])*)\"",
    re.DOTALL,
)
_OPAQUE_MIN_LEN = 120
_BASE64ISH = re.compile(r"^[A-Za-z0-9+/=_-]+$")
_HEXISH = re.compile(r"^[0-9a-fA-F]+$")

# delete_thread(...) whose first argument opens with a string quote — a
# hardcoded destructive target. `f"..."` is not matched (the `f` precedes the
# quote), so dynamic references are left for the judge.
_DELETE_THREAD_LITERAL = re.compile(r"\bdelete_thread\s*\(\s*['\"]")

# add_role/remove_role(user_id, ROLE_ID): the SECOND argument (the grant target)
# must be a plain string literal so the role a handler can grant is reviewable —
# the inverse of the delete_thread rule (which rejects a LITERAL first arg). The
# user_id (first arg) is expected to be dynamic (context/payload), so only the
# role id is constrained. The first arg is matched non-greedily up to the comma,
# then the second arg must open with a bare quote — a variable, subscript, or
# f-string role id (the `f` precedes the quote) fails the match and is rejected.
# Pragmatic/regex-based like the rest of this module: fragile for deeply nested
# first args (a call with its own comma), which the judge still backstops.
_ROLE_ID_LITERAL = re.compile(
    r"\b(?:add_role|remove_role)\s*\(\s*[^,()]+,\s*(?P<second>['\"]?)"
)


def _string_literals(script: str) -> list[str]:
    out: list[str] = []
    for match in _STRING_LITERAL.finditer(script):
        out.append(next(g for g in match.groups() if g is not None))
    return out


def check_static(script: str) -> str | None:
    """Static-only checks. Return a one-line reason to reject, or None if clean."""
    if not script.strip():
        return "script is empty"
    if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        return f"script exceeds the {MAX_SCRIPT_BYTES}-byte limit"

    for token in _BANNED_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", script):
            return f"script uses banned construct '{token}'"

    # delete_thread is irreversible: its target must come from trigger context or
    # a list_threads result, never a hardcoded id literal (an unreviewable
    # destructive target). A quoted first argument is exactly that literal; a
    # variable, subscript, or f-string reference passes through to the judge.
    if _DELETE_THREAD_LITERAL.search(script):
        return "script calls delete_thread() with a hardcoded thread id literal"

    # A role grant's target must be a reviewable string-literal role id (the
    # allowlist is stated against literals). A variable/subscript/f-string role
    # id is rejected here; the runtime allowlist still gates whatever passes.
    for match in _ROLE_ID_LITERAL.finditer(script):
        if not match.group("second"):
            return (
                "script calls add_role/remove_role with a non-literal role id "
                "(the role id must be a string literal)"
            )

    for literal in _string_literals(script):
        compact = literal.strip()
        if len(compact) >= _OPAQUE_MIN_LEN and " " not in compact and "\n" not in compact:
            if _BASE64ISH.match(compact) or _HEXISH.match(compact):
                return "script contains an opaque/encoded blob (base64/hex string)"

    # Defined-but-never-called function = a silent no-op handler (a common LLM
    # slip: wrapping all logic in `async def main()` and forgetting to call it).
    for name in re.findall(r"(?:async\s+)?def\s+(\w+)\s*\(", script):
        if len(re.findall(rf"\b{re.escape(name)}\s*\(", script)) <= 1:
            return f"script defines {name}() but never calls it (handler would do nothing)"
    return None


def compiles(script: str) -> str | None:
    """Parse the script with Monty. Return a compile error message, or None."""
    import pydantic_monty as monty

    try:
        monty.Monty(script, inputs=["context"], type_check=False)
    except monty.MontyError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def lint_script(script: str) -> str | None:
    """Full lint: static checks then a Monty parse. Reason to reject, or None."""
    reason = check_static(script)
    if reason is not None:
        return reason
    return compiles(script)
