"""Account page hooks.

Skrift 0.2.0 owns the ``/account`` route with a built-in account page that
extensions augment through the ``account_page_*`` filter hooks. We render the
Smarter Dev profile editor as a section on that page (the matching POST handler
still lives on :class:`AccountController` in ``account_controller.py``).
"""

from __future__ import annotations

from typing import Any

from skrift.hooks import ACCOUNT_PAGE_CONTEXT, ACCOUNT_PAGE_SECTIONS, filter

from smarter_dev.web.account_controller import _get_or_create_profile


@filter(ACCOUNT_PAGE_CONTEXT)
async def _add_profile_context(
    context: dict[str, Any], request, db_session, user, *args, **kwargs
) -> dict[str, Any]:
    """Expose the user's ``UserProfile`` to the account page template."""
    context["profile"] = await _get_or_create_profile(db_session, user.id)
    return context


@filter(ACCOUNT_PAGE_SECTIONS)
async def _add_profile_section(
    sections: list[dict[str, Any]], request, db_session, user, context, *args, **kwargs
) -> list[dict[str, Any]]:
    """Render the profile editor section on the core account page."""
    sections.append({"template": "account/profile_section.html"})
    return sections
