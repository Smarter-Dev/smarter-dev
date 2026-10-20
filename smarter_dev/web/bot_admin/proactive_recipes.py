"""Proactive-agent recipes on the Skrift admin panel (#267).

Under ``/admin/bot/guilds/{guild_id}/proactive-recipes``: a guild's recipes
with the preview the agent is shown for each, create, edit, switch on/off,
delete, and the guild's preview length. Only admins write recipes; the agent
reads them through the bot API (``api_native/proactive_recipes.py``) and
cannot change them.

An edit that changes the name or the instructions raises the recipe's
revision; switching it on or off does not. The agent compares the revision
of each recipe it has opened with the current one, so an edit reaches it as
a notice at its next wake. The form's "Show preview" button renders the
preview of unsaved text without saving it.
"""

from __future__ import annotations

import logging
from uuid import UUID

from litestar import Controller
from litestar import Request
from litestar import get
from litestar import post
from litestar.response import Redirect
from litestar.response import Response
from litestar.response import Template as TemplateResponse
from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from skrift.flash import flash_error
from skrift.flash import flash_success
from skrift.flash import get_flash_messages
from skrift.forms.core import verify_csrf
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.bot_admin.campaigns import fetch_guild_or_error
from smarter_dev.web.crud import ConflictError
from smarter_dev.web.crud import create_proactive_recipe
from smarter_dev.web.crud import get_proactive_recipe
from smarter_dev.web.crud import get_proactive_recipe_preview_chars
from smarter_dev.web.crud import list_proactive_recipes
from smarter_dev.web.crud import set_proactive_recipe_preview_chars
from smarter_dev.web.crud import update_proactive_recipe
from smarter_dev.web.models import PROACTIVE_RECIPE_INSTRUCTIONS_MAX
from smarter_dev.web.models import PROACTIVE_RECIPE_NAME_MAX
from smarter_dev.web.models import PROACTIVE_RECIPE_PREVIEW_DEFAULT
from smarter_dev.web.models import PROACTIVE_RECIPE_PREVIEW_MAX
from smarter_dev.web.models import PROACTIVE_RECIPE_PREVIEW_MIN
from smarter_dev.web.proactive_recipes import RecipeForm
from smarter_dev.web.proactive_recipes import parse_preview_chars
from smarter_dev.web.proactive_recipes import read_recipe_form
from smarter_dev.web.proactive_recipes import recipe_form_errors
from smarter_dev.web.proactive_recipes import recipe_name_key
from smarter_dev.web.proactive_recipes import recipe_preview

logger = logging.getLogger(__name__)

_ACTIVE_PAGE = "proactive_recipes"
_GUARDS = [auth_guard, Permission("administrator")]
_EXPIRED = "Your session expired. Please try again."


def _list_path(guild_id: str) -> str:
    return f"/admin/bot/guilds/{guild_id}/proactive-recipes"


class ProactiveRecipesAdminController(Controller):
    """A guild's proactive-agent recipes under ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    @get("/guilds/{guild_id:str}/proactive-recipes", guards=_GUARDS)
    async def recipes_list(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Every recipe, on or off, with the preview the agent is shown."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        preview_chars = await get_proactive_recipe_preview_chars(db_session, guild_id)
        recipes = await list_proactive_recipes(db_session, guild_id)
        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/proactive_recipes/list.html",
            context={
                "guild": guild,
                "recipes": [
                    (recipe, recipe_preview(recipe.instructions, preview_chars))
                    for recipe in recipes
                ],
                "preview_chars": preview_chars,
                "preview_default": PROACTIVE_RECIPE_PREVIEW_DEFAULT,
                "preview_min": PROACTIVE_RECIPE_PREVIEW_MIN,
                "preview_max": PROACTIVE_RECIPE_PREVIEW_MAX,
                "flash_messages": get_flash_messages(request),
                "active_page": _ACTIVE_PAGE,
                "guild_id": guild_id,
                **ctx,
            },
        )

    @post("/guilds/{guild_id:str}/proactive-recipes/settings", guards=_GUARDS)
    async def recipes_settings(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Set the guild's preview length."""
        _guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        back = _list_path(guild_id)
        if not await verify_csrf(request):
            flash_error(request, _EXPIRED)
            return Redirect(path=back)
        value = parse_preview_chars((await request.form()).get("preview_chars"))
        if value is None:
            flash_error(
                request,
                "The preview length must be a whole number from "
                f"{PROACTIVE_RECIPE_PREVIEW_MIN} to {PROACTIVE_RECIPE_PREVIEW_MAX}.",
            )
            return Redirect(path=back)
        await set_proactive_recipe_preview_chars(db_session, guild_id, value)
        await db_session.commit()
        flash_success(request, f"Previews are now cut at {value} characters.")
        return Redirect(path=back)

    @get("/guilds/{guild_id:str}/proactive-recipes/create", guards=_GUARDS)
    async def recipe_create_form(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        return await _render_form(
            request,
            db_session,
            guild,
            guild_id,
            recipe=None,
            data=RecipeForm(name="", instructions="", enabled=True),
            errors=[],
        )

    @post("/guilds/{guild_id:str}/proactive-recipes/create", guards=_GUARDS)
    async def recipe_create(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        if not await verify_csrf(request):
            flash_error(request, _EXPIRED)
            return Redirect(path=f"{_list_path(guild_id)}/create")
        form = await request.form()
        data = read_recipe_form(form)
        errors = recipe_form_errors(data)
        if errors or form.get("action") == "preview":
            return await _render_form(
                request,
                db_session,
                guild,
                guild_id,
                recipe=None,
                data=data,
                errors=errors,
                status_code=400 if errors else 200,
            )
        try:
            await create_proactive_recipe(
                db_session,
                guild_id=guild_id,
                name=data.name,
                name_key=recipe_name_key(data.name),
                instructions=data.instructions,
                enabled=data.enabled,
            )
            await db_session.commit()
        except (ConflictError, IntegrityError):
            await db_session.rollback()
            return await _render_form(
                request,
                db_session,
                guild,
                guild_id,
                recipe=None,
                data=data,
                errors=[f"A recipe named “{data.name}” already exists."],
                status_code=400,
            )
        logger.info(
            "Admin created proactive recipe %r in guild %s", data.name, guild_id
        )
        flash_success(request, f"Recipe “{data.name}” created.")
        return Redirect(path=_list_path(guild_id))

    @get(
        "/guilds/{guild_id:str}/proactive-recipes/{recipe_id:uuid}/edit",
        guards=_GUARDS,
    )
    async def recipe_edit_form(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        recipe_id: UUID,
    ) -> Response:
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        recipe = await get_proactive_recipe(db_session, guild_id, recipe_id)
        if recipe is None:
            flash_error(request, "That recipe no longer exists.")
            return Redirect(path=_list_path(guild_id))
        return await _render_form(
            request,
            db_session,
            guild,
            guild_id,
            recipe=recipe,
            data=RecipeForm(
                name=recipe.name,
                instructions=recipe.instructions,
                enabled=recipe.enabled,
            ),
            errors=[],
        )

    @post(
        "/guilds/{guild_id:str}/proactive-recipes/{recipe_id:uuid}/edit",
        guards=_GUARDS,
    )
    async def recipe_edit(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        recipe_id: UUID,
    ) -> Response:
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        recipe = await get_proactive_recipe(db_session, guild_id, recipe_id)
        if recipe is None:
            flash_error(request, "That recipe no longer exists.")
            return Redirect(path=_list_path(guild_id))
        if not await verify_csrf(request):
            flash_error(request, _EXPIRED)
            return Redirect(path=f"{_list_path(guild_id)}/{recipe_id}/edit")
        form = await request.form()
        data = read_recipe_form(form)
        errors = recipe_form_errors(data)
        if errors or form.get("action") == "preview":
            return await _render_form(
                request,
                db_session,
                guild,
                guild_id,
                recipe=recipe,
                data=data,
                errors=errors,
                status_code=400 if errors else 200,
            )
        try:
            changed = await update_proactive_recipe(
                db_session,
                recipe,
                name=data.name,
                name_key=recipe_name_key(data.name),
                instructions=data.instructions,
                enabled=data.enabled,
            )
            await db_session.commit()
        except (ConflictError, IntegrityError):
            await db_session.rollback()
            recipe = await get_proactive_recipe(db_session, guild_id, recipe_id)
            return await _render_form(
                request,
                db_session,
                guild,
                guild_id,
                recipe=recipe,
                data=data,
                errors=[f"A recipe named “{data.name}” already exists."],
                status_code=400,
            )
        logger.info(
            "Admin edited proactive recipe %s in guild %s (text changed: %s)",
            recipe_id,
            guild_id,
            changed,
        )
        flash_success(
            request,
            f"Recipe “{data.name}” saved. If the agent has it open, it is told "
            "at its next wake to open it again."
            if changed
            else f"Recipe “{data.name}” saved.",
        )
        return Redirect(path=_list_path(guild_id))

    @post(
        "/guilds/{guild_id:str}/proactive-recipes/{recipe_id:uuid}/toggle",
        guards=_GUARDS,
    )
    async def recipe_toggle(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        recipe_id: UUID,
    ) -> Redirect:
        """Switch a recipe on or off; off hides it from the agent."""
        back = _list_path(guild_id)
        if not await verify_csrf(request):
            flash_error(request, _EXPIRED)
            return Redirect(path=back)
        recipe = await get_proactive_recipe(db_session, guild_id, recipe_id)
        if recipe is None:
            flash_error(request, "That recipe no longer exists.")
            return Redirect(path=back)
        recipe.enabled = not recipe.enabled
        await db_session.commit()
        flash_success(
            request,
            f"Recipe “{recipe.name}” is on."
            if recipe.enabled
            else f"Recipe “{recipe.name}” is off; the agent can no longer see or open it.",
        )
        return Redirect(path=back)

    @post(
        "/guilds/{guild_id:str}/proactive-recipes/{recipe_id:uuid}/delete",
        guards=_GUARDS,
    )
    async def recipe_delete(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        recipe_id: UUID,
    ) -> Redirect:
        back = _list_path(guild_id)
        if not await verify_csrf(request):
            flash_error(request, _EXPIRED)
            return Redirect(path=back)
        recipe = await get_proactive_recipe(db_session, guild_id, recipe_id)
        if recipe is None:
            flash_error(request, "That recipe no longer exists.")
            return Redirect(path=back)
        name = recipe.name
        await db_session.delete(recipe)
        await db_session.commit()
        logger.info(
            "Admin deleted proactive recipe %s in guild %s", recipe_id, guild_id
        )
        flash_success(request, f"Recipe “{name}” deleted.")
        return Redirect(path=back)


async def _render_form(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    guild_id: str,
    *,
    recipe,
    data: RecipeForm,
    errors: list[str],
    status_code: int = 200,
) -> TemplateResponse:
    """The create or edit form, with the preview of the text as submitted."""
    preview_chars = await get_proactive_recipe_preview_chars(db_session, guild_id)
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/proactive_recipes/form.html",
        context={
            "guild": guild,
            "recipe": recipe,
            "form_data": data,
            "preview": recipe_preview(data.instructions, preview_chars)
            if data.instructions
            else "",
            "preview_chars": preview_chars,
            "name_max": PROACTIVE_RECIPE_NAME_MAX,
            "instructions_max": PROACTIVE_RECIPE_INSTRUCTIONS_MAX,
            "errors": errors,
            "flash_messages": get_flash_messages(request),
            "active_page": _ACTIVE_PAGE,
            "guild_id": guild_id,
            **ctx,
        },
        status_code=status_code,
    )
