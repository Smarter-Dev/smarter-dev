"""Skrift hook registrations for sudo Discord projection.

Imported once at app boot so the ``@action`` decorators take effect. The
sole job here is to keep the Discord role projection in sync with auth
events: whenever a user logs in / signs up via OAuth (anything that
fires ``after_user_update`` or ``after_user_created_db``), we kick a
``converge`` against their site user. The converge function is
idempotent and only touches the managed role set, so the cost of running
it on every login is negligible.

Why hook here and not just on the billing webhooks alone? A user can
buy sudo before linking their Discord account; the next time they sign
in with Discord, this hook is what actually projects their roles into
the guild.
"""

from __future__ import annotations

import asyncio
import logging

from skrift.lib.hooks import action

logger = logging.getLogger(__name__)


def _spawn_converge(user_id) -> None:
    """Run converge in its own task with its own DB session.

    The hook is fired from inside the auth controller, which may or may
    not have a healthy session in scope. To avoid bleeding into that
    session's transaction or relying on its lifecycle, we open a fresh
    session here.
    """
    async def _run() -> None:
        from smarter_dev.shared.database import get_skrift_db_session_context
        from smarter_dev.web.billing.converge import converge

        try:
            async with get_skrift_db_session_context() as session:
                await converge(session, user_id)
        except Exception:
            logger.exception("post-login converge failed for user %s", user_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_run())


@action("after_user_update", priority=10)
async def _converge_on_login(user, *args, **kwargs) -> None:
    """Re-project Discord roles whenever a user logs in.

    Fires on every OAuth login (including a freshly-linked provider) and
    on passkey logins. The check inside ``converge`` for a linked Discord
    OAuth account is the natural no-op for users who never linked one.
    """
    user_id = getattr(user, "id", None)
    if user_id is None:
        return
    _spawn_converge(user_id)


@action("after_user_created_db", priority=10)
async def _converge_on_signup(user, *args, **kwargs) -> None:
    """Re-project Discord roles right after signup completes."""
    user_id = getattr(user, "id", None)
    if user_id is None:
        return
    _spawn_converge(user_id)
