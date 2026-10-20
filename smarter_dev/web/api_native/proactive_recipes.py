"""Bot API for the proactive agent's recipes (#267).

Two read-only routes; only admins write recipes, on the admin site:

- ``GET /api/guilds/{guild_id}/proactive-recipes`` — the guild's enabled
  recipes as the agent's prompt lists them: name, preview, revision. 200 with
  an empty list when there are none.
- ``GET /api/guilds/{guild_id}/proactive-recipes/by-name?name=…`` — one
  enabled recipe whole, matched on its name casefolded with whitespace
  collapsed. 404 for an unknown name and for a recipe that is switched off
  alike: a switched-off recipe is hidden from the agent, not just closed.

A worker from before recipes never calls either route, so this ships ahead
of it; a worker with recipes treats a 404 from an older application as "no
recipes".
"""

from __future__ import annotations

from litestar import Controller
from litestar import get
from litestar.status_codes import HTTP_200_OK
from skrift.auth.guards import APIKeyOnly
from skrift.auth.guards import Permission
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import nested_not_found_error
from smarter_dev.web.api_native.schemas import ProactiveRecipeListRead
from smarter_dev.web.api_native.schemas import ProactiveRecipePreviewRead
from smarter_dev.web.api_native.schemas import ProactiveRecipeRead
from smarter_dev.web.crud import get_proactive_recipe_by_name_key
from smarter_dev.web.crud import get_proactive_recipe_preview_chars
from smarter_dev.web.crud import list_proactive_recipes
from smarter_dev.web.proactive_recipes import recipe_name_key
from smarter_dev.web.proactive_recipes import recipe_preview

BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE — Skrift's auth_guard inspects
# route_handler.guards for the APIKeyOnly marker (see model_overrides.py).
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


class ProactiveRecipesController(Controller):
    """A guild's enabled recipes: previews, and one opened whole."""

    path = "/api/guilds/{guild_id:str}/proactive-recipes"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get(status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def list_recipes(
        self, db_session: AsyncSession, guild_id: str
    ) -> ProactiveRecipeListRead:
        """Every enabled recipe's name and preview."""
        preview_chars = await get_proactive_recipe_preview_chars(db_session, guild_id)
        recipes = await list_proactive_recipes(db_session, guild_id, enabled_only=True)
        return ProactiveRecipeListRead(
            preview_chars=preview_chars,
            recipes=[
                ProactiveRecipePreviewRead(
                    id=recipe.id,
                    name=recipe.name,
                    preview=recipe_preview(recipe.instructions, preview_chars),
                    revision=recipe.revision,
                    updated_at=recipe.updated_at,
                )
                for recipe in recipes
            ],
        )

    @get("/by-name", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def open_recipe(
        self, db_session: AsyncSession, guild_id: str, name: str
    ) -> ProactiveRecipeRead:
        """One enabled recipe whole; 404 when unknown or switched off."""
        recipe = await get_proactive_recipe_by_name_key(
            db_session, guild_id, recipe_name_key(name)
        )
        if recipe is None or not recipe.enabled:
            raise nested_not_found_error("Recipe not found")
        return ProactiveRecipeRead.model_validate(recipe)
