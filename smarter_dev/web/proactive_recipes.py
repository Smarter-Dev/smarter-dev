"""Recipes for the proactive agent (#267): the rules both sides share.

An admin writes a recipe on the admin site; the proactive agent sees each
enabled recipe's name and preview in its prompt and opens one by name to read
it whole. The preview is computed here, once, so the admin page shows exactly
what the agent is shown: the recipe's first paragraph (the text before its
first blank line), its lines joined by spaces, cut to the guild's preview
length with an ellipsis when it was cut.

Only admins write recipes. They are not member data, so neither the privacy
purge nor the retention sweep rewrites them; the purge's check reports a
recipe that names the person, for an admin to edit by hand (see
``chat_bot_purge.INFO_TABLES`` and ``docs/data-retention.md``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from smarter_dev.web.models import PROACTIVE_RECIPE_INSTRUCTIONS_MAX
from smarter_dev.web.models import PROACTIVE_RECIPE_NAME_MAX
from smarter_dev.web.models import PROACTIVE_RECIPE_PREVIEW_MAX
from smarter_dev.web.models import PROACTIVE_RECIPE_PREVIEW_MIN

# ASCII whitespace only, so the edit form's script (``recipePreview`` in
# ``templates/admin/bot/proactive_recipes/form.html``) cuts exactly where this
# does: Python's and JavaScript's ideas of Unicode whitespace differ. A test
# runs both on the same texts.
_SPACE = " \t\n\r\f\v"
_SPACE_RUN = re.compile(r"[ \t\n\r\f\v]+")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
ELLIPSIS = "…"


def recipe_name_key(name: str) -> str:
    """The form names are unique in and opened by: casefolded, spaces collapsed."""
    return " ".join(name.split()).casefold()


def recipe_preview(instructions: str, preview_chars: int) -> str:
    """What the agent sees of a recipe before it opens it.

    The first paragraph (the text before the first blank line), its
    whitespace runs collapsed to single spaces, cut to ``preview_chars``
    characters (code points) with the ellipsis counted in.
    """
    text = instructions.replace("\r\n", "\n").strip(_SPACE)
    first = _BLANK_LINE.split(text, maxsplit=1)[0]
    paragraph = _SPACE_RUN.sub(" ", first).strip(" ")
    if len(paragraph) <= preview_chars:
        return paragraph
    return paragraph[: max(0, preview_chars - 1)].rstrip(" ") + ELLIPSIS


@dataclass(frozen=True)
class RecipeForm:
    name: str
    instructions: str
    enabled: bool


def read_recipe_form(form) -> RecipeForm:
    """The submitted recipe fields, trimmed; line endings normalised."""
    return RecipeForm(
        name=" ".join(str(form.get("name") or "").split()),
        instructions=str(form.get("instructions") or "").replace("\r\n", "\n").strip(),
        enabled=form.get("enabled") == "on",
    )


def recipe_form_errors(data: RecipeForm) -> list[str]:
    errors = []
    if not data.name:
        errors.append("Give the recipe a name.")
    elif len(data.name) > PROACTIVE_RECIPE_NAME_MAX:
        errors.append(
            f"The name is {len(data.name)} characters; the limit is "
            f"{PROACTIVE_RECIPE_NAME_MAX}."
        )
    if not data.instructions:
        errors.append("Write the recipe's instructions.")
    elif len(data.instructions) > PROACTIVE_RECIPE_INSTRUCTIONS_MAX:
        errors.append(
            f"The instructions are {len(data.instructions):,} characters; the "
            f"limit is {PROACTIVE_RECIPE_INSTRUCTIONS_MAX:,}."
        )
    return errors


def parse_preview_chars(raw) -> int | None:
    """A submitted preview length, or None when it is not a whole number in range."""
    try:
        value = int(str(raw or "").strip())
    except ValueError:
        return None
    if not PROACTIVE_RECIPE_PREVIEW_MIN <= value <= PROACTIVE_RECIPE_PREVIEW_MAX:
        return None
    return value
