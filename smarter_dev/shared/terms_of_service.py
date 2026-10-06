"""The public Terms of Service.

``terms_of_service.md`` beside this module is the one copy of the wording; the
site renders it at :data:`TERMS_PATH`, the URL the Discord developer portal
links to.
"""

from __future__ import annotations

from datetime import date
from functools import cache
from pathlib import Path

from smarter_dev.shared.config import get_settings

TERMS_PATH = "/terms"
TERMS_TITLE = "Terms of Service"
LAST_UPDATED = date(2026, 10, 6)

_TERMS_FILE = Path(__file__).with_name("terms_of_service.md")


@cache
def terms_markdown() -> str:
    """The full terms, as markdown."""
    return _TERMS_FILE.read_text(encoding="utf-8")


def terms_url() -> str:
    """The terms' public URL on this deployment."""
    return f"{get_settings().site_base_url.rstrip('/')}{TERMS_PATH}"
