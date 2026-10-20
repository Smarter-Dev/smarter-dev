"""No new handler may punish members: moderation has one decision-maker.

Admin handlers used to be how a guild added a moderator: a script judged a
message and banned, timed out or warned on its own (scam-banner, Newcomer
Watch, Keyword Watch). Every such script is a parallel punisher beside the
moderation coordinator (``smarter_dev.bot.moderation_coordinator``), which is
taking those decisions over. So a script that is new, or that would call a
sanction it did not call before, is refused when it:

* is authored with ``/adminhandler`` (``handler_authoring``);
* is created or rewritten through the bot API (``api_native.admin_handlers``).

A script already live that sanctions keeps the sanctions it has (scam-banner
until the cutover retires it), so editing its wording still works; it cannot
add one. Removing messages, roles and threads stay available for upkeep (the
role rule refuses staff roles separately, ``staff_protection.role_refusal``).
The catalog's moderation templates are retired the same way
(``ExtensionManifest.moved_to_moderation``).
"""

from __future__ import annotations

import ast
import re

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

_NAME = re.compile(r"\b(" + "|".join(sorted(SANCTION_FUNCTIONS)) + r")\b")


def sanction_calls(script: str | None) -> frozenset[str]:
    """The sanction functions ``script`` uses.

    Any use counts, not only a direct call: ``f = ban_user; await f(id)`` or
    passing ``kick_user`` along reaches the same external function, and a
    sanction function as a value has no other purpose. Read from the syntax
    tree, so a name in a comment or a string does not count; a script that
    does not parse is read with a plain pattern instead (stricter: any
    mention counts).
    """
    if not script:
        return frozenset()
    try:
        tree = compile(
            script,
            "<handler>",
            "exec",
            flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
    except (SyntaxError, ValueError):
        return frozenset(_NAME.findall(script))
    return frozenset(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in SANCTION_FUNCTIONS
    )


def moderation_refusal(
    script: str | None, *, existing_script: str | None = None
) -> str | None:
    """Why ``script`` may not be saved, or None when it may.

    ``existing_script`` is the live script a rewrite replaces: the sanctions
    it already calls are kept, no new one may be added.
    """
    new = sanction_calls(script) - sanction_calls(existing_script)
    if not new:
        return None
    return REFUSAL.format(calls=", ".join(sorted(new)))
