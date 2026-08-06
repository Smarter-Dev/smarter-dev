"""The chat agent must never write its own replies into the event log.

The short-term event log exists so the bot can own what its ACCOUNT did —
moderation, announcements, DMs. Its own replies and its ``> -#`` status lines are
already in the conversation history, so recording them would feed the agent its
own words back as "things I did this hour" and, worse, make it narrate its own
talking. Reading the log is fine and expected; writing to it from the chat path
is the mistake, so this guard is on the recorder import specifically rather than
on the shared event-log module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

RECORDER_MODULES = frozenset(
    {
        "smarter_dev.bot.guild_event_recorder",
        "smarter_dev.web.guild_event_recorder",
    }
)

CHAT_PATH_MODULES = (
    Path("smarter_dev/bot/services/chat_engine.py"),
    Path("smarter_dev/bot/agents/chat_tools.py"),
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _imported_names(source_path: Path) -> set[str]:
    """Every module name and imported symbol the file pulls in, flattened."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("module_path", CHAT_PATH_MODULES, ids=lambda p: p.name)
def test_chat_path_never_imports_the_event_recorder(module_path: Path):
    imported = _imported_names(REPOSITORY_ROOT / module_path)

    assert not (imported & RECORDER_MODULES)
    assert "record_guild_event" not in imported
