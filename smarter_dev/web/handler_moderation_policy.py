"""No new handler may punish members: moderation has one decision-maker.

Admin handlers used to be how a guild added a moderator: a script judged a
message and banned, timed out or warned on its own (scam-banner, Newcomer
Watch, Keyword Watch). Every such script is a parallel punisher beside the
moderation coordinator (``smarter_dev.bot.moderation_coordinator``), which is
taking those decisions over. So a script that uses a sanction is refused when
it:

* is authored with ``/adminhandler`` (``handler_authoring``);
* is created or rewritten through the bot API (``api_native.admin_handlers``).

A script already live that sanctions is **frozen**: saving it unchanged still
works (an edit of the handler's other fields), and so does removing every
sanction from it, but any other change is refused. Keeping only the sanction
call sites would not be enough: the condition around a ``ban_user`` call is
the punisher, and rewriting it ("ban anyone who says hello") keeps the call.
Nor can it be renamed.

A script Python cannot parse is refused outright: the runtime (Monty) accepts
syntax Python 3.13 rejects, so any other reading of such a script could miss a
sanction. Python's parser NFKC-normalises identifiers as Monty does, so
``ｂan_user`` is read as ``ban_user``. Only identifiers are normalised, never
the whole text: a fullwidth quote inside a string would otherwise become a
real quote and could swallow the code after it.

Removing messages, roles and threads stay available for upkeep (the role rule
refuses staff roles separately, ``staff_protection.role_refusal``). The
catalog's moderation templates are retired the same way
(``ExtensionManifest.moved_to_moderation``): their installs cannot be
reconfigured, only disabled or uninstalled (``extension_installs``).
"""

from __future__ import annotations

import ast
import hashlib
import unicodedata

#: The script functions that sanction a member.
SANCTION_FUNCTIONS = frozenset(
    {
        "ban_user",
        "kick_user",
        "timeout_user",
        "remove_timeout",
        "hold_member",
        "release_hold",
        "warn_user",
    }
)

REFUSAL = (
    "handlers can no longer punish members ({calls}): bans, kicks, timeouts, "
    "holds and warnings are decided by the server's moderation system, which "
    "already covers scams, spam, spam rings, blocked files and edited pings. "
    "A handler may still post, react, manage threads and change non-staff roles"
)

FROZEN_REFUSAL = (
    "this handler punishes members ({calls}), so it can no longer be changed: "
    "bans, kicks, timeouts, holds and warnings are moving to the server's "
    "moderation system. Save it unchanged, or remove every sanction from it"
)

FROZEN_RENAME_REFUSAL = (
    "this handler punishes members ({calls}), so it can no longer be renamed: "
    "bans, kicks, timeouts, holds and warnings are moving to the server's "
    "moderation system. Keep its name, or remove every sanction from it"
)

UNPARSEABLE_REFUSAL = "the script is not valid Python ({error})"

_BOM = "\ufeff"


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


def sanction_calls(script: str | None) -> frozenset[str]:
    """The sanction functions ``script`` uses.

    Any use counts, not only a direct call: ``f = ban_user; await f(id)`` or
    passing ``kick_user`` along reaches the same external function, and a
    sanction function as a value has no other purpose. Read from the syntax
    tree, so a name in a comment or a string does not count. Raises
    SyntaxError or ValueError for a script Python cannot parse.
    """
    if not script:
        return frozenset()
    return frozenset(
        node.id
        for node in ast.walk(_parse(script))
        if isinstance(node, ast.Name)
        and unicodedata.normalize("NFKC", node.id) in SANCTION_FUNCTIONS
    )


def moderation_refusal(
    script: str | None,
    *,
    existing_script: str | None = None,
    handler_name: str | None = None,
) -> str | None:
    """Why ``script`` may not be saved, or None when it may.

    ``existing_script`` is the live script a rewrite replaces (None for a new
    handler). ``handler_name`` is accepted for the callers' sake and no longer
    changes the answer: the scam-banner reference-copy exception went with the
    #240 cutover.
    """
    try:
        calls = sanction_calls(script)
    except (SyntaxError, ValueError) as error:
        return UNPARSEABLE_REFUSAL.format(error=_parse_error(error))
    if not calls:
        return None
    if not _uses_sanctions(existing_script):
        # Nothing frozen to keep: a new punisher, whatever the name says.
        return REFUSAL.format(calls=", ".join(sorted(calls)))
    if script == existing_script:
        return None
    return FROZEN_REFUSAL.format(calls=", ".join(sorted(calls)))


def rename_refusal(script: str | None, *, name: str, new_name: str) -> str | None:
    """Why a handler saving ``script`` may not be renamed, or None.

    A handler that keeps a sanction keeps its name too, so a frozen punisher
    cannot take up another handler's name. Call after
    :func:`moderation_refusal` passed.
    """
    if new_name == name or not _uses_sanctions(script):
        return None
    return FROZEN_RENAME_REFUSAL.format(calls=", ".join(sorted(sanction_calls(script))))


def _uses_sanctions(script: str | None) -> bool:
    """Whether ``script`` uses a sanction; an unparseable one counts as not."""
    try:
        return bool(sanction_calls(script))
    except (SyntaxError, ValueError):
        return False


def _parse_error(error: SyntaxError | ValueError) -> str:
    if isinstance(error, SyntaxError) and error.lineno is not None:
        return f"line {error.lineno}: {error.msg}"
    return str(error)
