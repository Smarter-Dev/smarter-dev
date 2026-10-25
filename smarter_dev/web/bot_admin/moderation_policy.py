"""One moderation settings page and API per guild, and its incidents (#241).

``/admin/bot/guilds/{guild_id}/moderation`` edits the guild's moderation
policy (:mod:`smarter_dev.shared.moderation_policy`): every rule's mode,
scope and settings, the protected roles, the sanctions, the risk windows,
the provisional hold, the classifier, where reports go and the staff-ping
triage. A save writes the next revision on top of the one the form was
loaded from, or is refused: two admins cannot undo each other unseen. The
bot picks the new revision up on its next message; the page shows which
revision each bot process has loaded, and since when.

``.../moderation/policy.json`` is the same thing as an API: ``GET`` the
current revision, ``PUT`` ``{"base_revision": n, "document": {...}}``.

``.../moderation/incidents`` lists incidents with what was requested,
confirmed, not applied and unknown, each linked to its one report.

The form is parsed against the current document: every setting the document
has is read from the form (:func:`document_from_form`), so a checkbox left
unticked is off and an emptied list is empty. Nothing is filled in.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID

from litestar import Controller
from litestar import Request
from litestar import get
from litestar import post
from litestar import put
from litestar.response import Redirect
from litestar.response import Response
from litestar.response import Template as TemplateResponse
from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from skrift.flash import flash_error
from skrift.flash import flash_success
from skrift.flash import get_flash_messages
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.bot.moderation_coordinator.ownership import read_ownership
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.shared.staff_protection import STAFF_ROLE_IDS
from smarter_dev.web import moderation_incidents_view as incidents_view
from smarter_dev.web import moderation_policy_store as store
from smarter_dev.web.bot_admin.guild_configs import fetch_channels_of_types
from smarter_dev.web.bot_admin.guild_configs import fetch_guild_roles
from smarter_dev.web.bot_admin.guild_configs import split_id_list
from smarter_dev.web.bot_admin.squads import fetch_guild_or_error

logger = logging.getLogger(__name__)

_TEXT_CHANNEL_TYPES: frozenset[int] = frozenset({0, 5})
#: A process not seen for this long is shown as gone quiet.
STALE_AFTER = timedelta(minutes=2)
#: Lists picked from a fixed set: one checkbox per option, named
#: ``<path>.<option>``. Every other list is a textarea of entries.
CHOICE_LISTS: dict[str, tuple[str, ...]] = {
    "sanctions.allowed": mp.SANCTIONS,
    "provisional.signals": mp.SIGNALS,
    "triage.enabled_tools": mp.TRIAGE_TOOLS,
}
#: The document's fields the form does not carry.
NOT_ON_THE_FORM = frozenset({"schema"})

#: The permissions behind :data:`~smarter_dev.shared.staff_protection.
#: STAFF_PERMISSIONS`, as the page names them.
STAFF_PERMISSION_NAMES = (
    "Administrator",
    "Manage Server",
    "Manage Roles",
    "Manage Channels",
    "Manage Messages",
    "Timeout Members",
    "Kick Members",
    "Ban Members",
    "Manage Nicknames",
    "Manage Webhooks",
    "View Audit Log",
)

RULE_TITLES = {
    mp.SPAM_ENGINE: "Spam engine",
    mp.ATTACHMENT_FILTER: "Attachment filter",
    mp.SPAM_RING: "Spam ring",
    mp.SCAM_GATE: "Scam check",
    mp.EDIT_PING: "Edit-ping",
    mp.BUMP_CHANNEL: "Bump channel",
    mp.MASS_MENTION: "@everyone / @here",
}
RULE_HELP = {
    mp.SPAM_ENGINE: "Message rate, channel spread, duplicates, mass mentions, "
    "nitro bait and scam links: a warning, then a mute.",
    mp.ATTACHMENT_FILTER: "File types: allowed, warned about, or removed.",
    mp.SPAM_RING: "A new member's event ad and the new members cheering it on: "
    "each participant timed out, their messages removed, one report with "
    "the transcript.",
    mp.SCAM_GATE: "Links, risky words and attachments from high-risk authors, "
    "a burst across channels or four or more images: checked by the classifier.",
    mp.EDIT_PING: "An @everyone/@here edited into a posted message: removed.",
    mp.BUMP_CHANNEL: "Members' messages in the Disboard bump channel: removed. "
    "These channels are the rule; none listed is no bump channel.",
    mp.MASS_MENTION: "@everyone/@here from a member, outside code: removed.",
}
SIGNAL_TITLES = {
    mp.SIGNAL_SCAM_LINK: "Scam link",
    mp.SIGNAL_NITRO_BAIT: "Mass mention with nitro bait",
    mp.SIGNAL_MENTION_LINK: "Mass mention with a link",
    mp.SIGNAL_BURST: "Same post in several channels within 10 s",
    mp.SIGNAL_IMAGES_HIGH_RISK: "Four or more images from a high-risk author",
}


class FormError(ValueError):
    """A field the form sent that is not a value of its setting's kind."""


def leaf_paths(document: Mapping[str, Any], prefix: str = "") -> list[str]:
    """Every setting in the document, as the form names it."""
    paths: list[str] = []
    for key, value in document.items():
        path = f"{prefix}{key}"
        if path in NOT_ON_THE_FORM:
            continue
        if isinstance(value, dict):
            paths += leaf_paths(value, path + ".")
        else:
            paths.append(path)
    return paths


def form_field_names(document: Mapping[str, Any]) -> list[str]:
    """The names the form must send for :func:`document_from_form`."""
    names: list[str] = []
    for path in leaf_paths(document):
        if path in CHOICE_LISTS:
            names += [f"{path}.{option}" for option in CHOICE_LISTS[path]]
        else:
            names.append(path)
    return names


def _number(path: str, raw: str, current):
    raw = raw.strip()
    if not raw:
        raise FormError(f"{path}: a number is needed")
    try:
        return float(raw) if isinstance(current, float) else int(raw)
    except ValueError:
        raise FormError(f"{path}: {raw!r} is not a number") from None


def _entries(raw: str) -> list[str]:
    return split_id_list(raw)


def document_from_form(current: dict[str, Any], form: Mapping[str, str]) -> dict:
    """The document the form describes, every setting read from it.

    A missing checkbox is off and an empty textarea is an empty list: the
    form names every setting (:func:`form_field_names`), so nothing absent
    is left as it was. Raises :class:`FormError`; the result still goes
    through :func:`~smarter_dev.shared.moderation_policy.validate`."""
    result = json.loads(json.dumps(current))
    errors: list[str] = []
    for path in leaf_paths(current):
        *parents, leaf = path.split(".")
        node = result
        for part in parents:
            node = node[part]
        value = node[leaf]
        try:
            if path in CHOICE_LISTS:
                node[leaf] = [
                    option
                    for option in CHOICE_LISTS[path]
                    if form.get(f"{path}.{option}")
                ]
            elif isinstance(value, bool):
                node[leaf] = bool(form.get(path))
            elif isinstance(value, int | float):
                node[leaf] = _number(path, str(form.get(path, "")), value)
            elif isinstance(value, list):
                node[leaf] = _entries(str(form.get(path, "")))
            else:
                raw = str(form.get(path, ""))
                if leaf == "instructions":
                    node[leaf] = raw.replace("\r\n", "\n")
                else:
                    node[leaf] = raw.strip() or None
        except FormError as error:
            errors.append(str(error))
    if errors:
        raise FormError("; ".join(errors))
    return result


def _ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def load_rows(loads, current_revision: int | None, now: datetime) -> list[dict]:
    """Each process's row for the page: behind, gone quiet, or current."""
    rows = []
    for load in loads:
        seen = load.seen_at if load.seen_at.tzinfo else load.seen_at.replace(tzinfo=UTC)
        quiet = now - seen > STALE_AFTER
        behind = current_revision is not None and load.revision != current_revision
        rows.append(
            {
                "process": load.process,
                "revision": load.revision,
                "loaded": _ago(load.loaded_at, now),
                "seen": _ago(load.seen_at, now),
                "behind": behind,
                "quiet": quiet,
            }
        )
    return rows


class ModerationPolicyAdminController(Controller):
    """The moderation policy page, its API, and the incidents views."""

    path = "/admin/bot/guilds"
    guards = [auth_guard, Permission("administrator")]

    # -- the page -----------------------------------------------------------

    @get("/{guild_id:str}/moderation")
    async def moderation_policy(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id, "moderation_policy"
        )
        if error is not None:
            return error
        ctx = await get_admin_context(request, db_session)
        stored = await store.read_policy(db_session, guild_id)
        ownership = await read_ownership(db_session, guild_id)
        now = datetime.now(UTC)
        document = (stored.document if stored else mp.default_document()).as_json()
        return TemplateResponse(
            "admin/bot/moderation_policy/page.html",
            context={
                "guild": guild,
                "guild_id": guild_id,
                "active_page": "moderation_policy",
                "stored": stored,
                "updated": _ago(stored.updated_at, now) if stored else None,
                "doc": document,
                "ownership": ownership,
                "loads": load_rows(
                    await store.loads(db_session, guild_id),
                    stored.revision if stored else None,
                    now,
                )
                if stored
                else [],
                "revisions": await store.revisions(db_session, guild_id)
                if stored
                else [],
                "text_channels": await fetch_channels_of_types(
                    guild_id, _TEXT_CHANNEL_TYPES
                ),
                "guild_roles": await fetch_guild_roles(guild_id),
                "rules": mp.RULES,
                "rule_titles": RULE_TITLES,
                "rule_help": RULE_HELP,
                "modes": mp.MODES,
                "sanctions": mp.SANCTIONS,
                "signals": mp.SIGNALS,
                "signal_titles": SIGNAL_TITLES,
                "triage_tools": mp.TRIAGE_TOOLS,
                "staff_role_ids": sorted(STAFF_ROLE_IDS),
                "staff_permissions": STAFF_PERMISSION_NAMES,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post("/{guild_id:str}/moderation")
    async def save_moderation_policy(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        redirect = Redirect(path=f"/admin/bot/guilds/{guild_id}/moderation")
        form = await request.form()
        stored = await store.read_policy(db_session, guild_id)
        if stored is None:
            flash_error(
                request,
                "This server has no moderation policy yet. It is imported from "
                "the current settings by the import Job; nothing was saved.",
            )
            return redirect
        base = str(form.get("base_revision", "")).strip()
        try:
            document = document_from_form(stored.document.as_json(), form)
            revision = await store.save_policy(
                db_session,
                guild_id,
                document,
                base_revision=int(base) if base.isdigit() else -1,
                by=await _who(request, db_session),
                source=store.SOURCE_ADMIN,
            )
        except (FormError, mp.PolicyError) as error:
            flash_error(request, f"Not saved: {error}")
            return redirect
        except store.StaleRevision as error:
            flash_error(request, f"Not saved: {error}.")
            return redirect
        flash_success(
            request,
            f"Saved as revision {revision}. The bot uses it from its next message.",
        )
        return redirect

    # -- the API ------------------------------------------------------------

    @get("/{guild_id:str}/moderation/policy.json")
    async def policy_json(self, db_session: AsyncSession, guild_id: str) -> Response:
        stored = await store.read_policy(db_session, guild_id)
        if stored is None:
            return Response({"guild_id": guild_id, "revision": None}, status_code=404)
        now = datetime.now(UTC)
        loads = await store.loads(db_session, guild_id)
        rows = load_rows(loads, stored.revision, now)
        return Response(
            {
                "guild_id": guild_id,
                "revision": stored.revision,
                "updated_by": stored.updated_by,
                "updated_at": stored.updated_at.isoformat()
                if stored.updated_at
                else None,
                "document": stored.document.as_json(),
                "loads": [
                    {**row, "loaded_at": load.loaded_at.isoformat()}
                    for row, load in zip(rows, loads, strict=True)
                ],
            }
        )

    @put("/{guild_id:str}/moderation/policy.json")
    async def put_policy_json(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any unreadable body
            return Response({"error": "the body must be JSON"}, status_code=400)
        if not isinstance(body, dict) or "document" not in body:
            return Response(
                {"error": 'send {"base_revision": n, "document": {...}}'},
                status_code=400,
            )
        base = body.get("base_revision")
        if base is not None and not isinstance(base, int):
            return Response(
                {"error": "base_revision must be a number"}, status_code=400
            )
        try:
            revision = await store.save_policy(
                db_session,
                guild_id,
                body["document"],
                base_revision=base,
                by=await _who(request, db_session),
                source=store.SOURCE_API,
            )
        except mp.PolicyError as error:
            return Response({"errors": error.errors}, status_code=422)
        except store.StaleRevision as error:
            return Response(
                {"error": str(error), "revision": error.current}, status_code=409
            )
        return Response({"guild_id": guild_id, "revision": revision})

    # -- incidents ------------------------------------------------------------

    @get("/{guild_id:str}/moderation/incidents")
    async def incidents(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        user_id = request.query_params.get("user_id", "").strip() or None
        return TemplateResponse(
            "admin/bot/moderation_policy/incidents.html",
            context={
                "guild_id": guild_id,
                "active_page": "moderation_incidents",
                "filter_user": user_id or "",
                "incidents": await incidents_view.list_incidents(
                    db_session, guild_id, user_id=user_id
                ),
                **ctx,
            },
        )

    @get("/{guild_id:str}/moderation/incidents/{incident_id:str}")
    async def incident(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        incident_id: str,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        try:
            parsed = UUID(incident_id)
        except ValueError:
            parsed = None
        detail = (
            await incidents_view.incident_detail(db_session, guild_id, parsed)
            if parsed
            else None
        )
        return TemplateResponse(
            "admin/bot/moderation_policy/incident.html",
            context={
                "guild_id": guild_id,
                "active_page": "moderation_incidents",
                "detail": detail,
                "incident_id": incident_id,
                "outcome": incidents_view.outcome,
                "system_kinds": incidents_view.SYSTEM_ENTRY_KINDS,
                **ctx,
            },
            status_code=200 if detail else 404,
        )


async def _who(request: Request, db_session: AsyncSession) -> str:
    """Who saved, for the revision history: the signed-in admin.

    Read through the admin context (the session's user): the app has no
    auth middleware, so ``request.user`` is not set."""
    user = (await get_admin_context(request, db_session)).get("user")
    for attribute in ("name", "email", "id"):
        value = getattr(user, attribute, None)
        if value:
            return f"admin:{value}"[:100]
    return "admin"
