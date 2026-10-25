"""No handler may punish members: moderation has one decision-maker.

Admin handlers used to be how a guild added a moderator: a script judged a
message and banned, timed out or warned on its own (scam-banner, Newcomer
Watch, Keyword Watch). The moderation coordinator
(``smarter_dev.bot.moderation_coordinator``) took those decisions over, and
the handler runtime no longer has the functions that punished members
(:data:`REMOVED_FUNCTIONS`). A script that still names one would only fail
when it ran, so it is refused when it is saved:

* authored with ``/adminhandler`` (``handler_authoring``);
* created or rewritten through the bot API (``api_native.admin_handlers``).

A script Python cannot parse is refused outright: the runtime (Monty) accepts
syntax Python 3.13 rejects, so any other reading of such a script could miss a
removed function. Python's parser NFKC-normalises identifiers as Monty does,
so ``ｂan_user`` is read as ``ban_user``. Only identifiers are normalised,
never the whole text: a fullwidth quote inside a string would otherwise become
a real quote and could swallow the code after it.

Posting, reacting, thread upkeep, deleting bots' messages and changing
ordinary roles stay available (the role rule refuses staff roles separately,
``staff_protection.role_refusal``); a handler hands a member to the moderators
with ``report_for_review``.
"""

from __future__ import annotations

import ast
import hashlib
import unicodedata

#: The script functions the runtime no longer has: every sanction, and the
#: destructive webhook and thread operations that went with them.
REMOVED_FUNCTIONS = frozenset(
    {
        "ban_user",
        "kick_user",
        "timeout_user",
        "remove_timeout",
        "hold_member",
        "release_hold",
        "warn_user",
        "delete_webhook",
        "lock_thread",
        "delete_thread",
    }
)

REFUSAL = (
    "handlers can no longer use {calls}: bans, kicks, timeouts, holds, "
    "warnings and removing members' messages are decided by the server's "
    "moderation system. A handler may still post, react, close and reopen "
    "threads, delete bots' messages, change non-staff roles, and hand a member "
    "to the moderators with report_for_review"
)

UNPARSEABLE_REFUSAL = "the script is not valid Python ({error})"

_BOM = "﻿"


def script_sha256(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def _parse(script: str) -> ast.Module:
    """The syntax tree of ``script`` as the runtime reads it.

    A leading byte-order mark is dropped (Monty ignores one; Python's
    ``compile`` of a str rejects it). Raises SyntaxError or ValueError for a
    script Python cannot parse.
    """
    return compile(
        script.removeprefix(_BOM),
        "<handler>",
        "exec",
        flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
    )


def removed_calls(script: str | None) -> frozenset[str]:
    """The removed functions ``script`` uses, NFKC-normalised.

    Any use counts, not only a direct call: ``f = ban_user; await f(id)`` or
    passing ``kick_user`` along names the same function. Read from the syntax
    tree, so a name in a comment or a string does not count. Raises
    SyntaxError or ValueError for a script Python cannot parse.
    """
    if not script:
        return frozenset()
    return frozenset(
        normalised
        for node in ast.walk(_parse(script))
        if isinstance(node, ast.Name)
        and (normalised := unicodedata.normalize("NFKC", node.id))
        in REMOVED_FUNCTIONS
    )


def moderation_refusal(script: str | None) -> str | None:
    """Why ``script`` may not be saved, or None when it may."""
    try:
        calls = removed_calls(script)
    except (SyntaxError, ValueError) as error:
        return UNPARSEABLE_REFUSAL.format(error=_parse_error(error))
    if not calls:
        return None
    return REFUSAL.format(calls=", ".join(sorted(calls)))


def _parse_error(error: SyntaxError | ValueError) -> str:
    if isinstance(error, SyntaxError) and error.lineno is not None:
        return f"line {error.lineno}: {error.msg}"
    return str(error)
