"""Every guild-event producer about a member records the member's id, so the
chat agent's event filter can honour the blocked-users list (#79)."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "smarter_dev"
CONSTRUCTORS = {"mod_action_event", "bot_message_event", "GuildEvent"}
# Builds the event from a whole ModerationAction context, which always
# carries target_user_id (build_mod_action_context).
CONTEXT_SITES = {("bot/mod_action_dispatch.py", "context")}
DEFINING_MODULE = "shared/guild_event_log.py"


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _sites():
    for path in PACKAGE.rglob("*.py"):
        relative = path.relative_to(PACKAGE).as_posix()
        if relative == DEFINING_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) in CONSTRUCTORS:
                yield relative, node


def _violation(relative: str, call: ast.Call) -> str | None:
    kind = _name(call.func)
    keywords = {kw.arg: kw.value for kw in call.keywords}
    if kind == "mod_action_event":
        context = call.args[0] if call.args else keywords.get("context")
        if isinstance(context, ast.Dict):
            keys = {k.value for k in context.keys if isinstance(k, ast.Constant)}
            if "target_username" in keys and "target_user_id" not in keys:
                return "target_username without target_user_id"
            return None
        if (relative, _name(context)) in CONTEXT_SITES:
            return None
        return "context is not a literal dict; add it to CONTEXT_SITES after checking"
    about_member = "target_username" in keywords or (
        _name(keywords.get("kind")) == "BOT_DM_KIND"
    )
    if about_member and "target_user_id" not in keywords:
        return "about a member without target_user_id"
    return None


def test_every_member_event_carries_the_member_id():
    sites = list(_sites())
    assert len(sites) >= 8  # the walk actually found the producers
    problems = [
        f"{relative}:{call.lineno} {reason}"
        for relative, call in sites
        if (reason := _violation(relative, call))
    ]
    assert problems == []
