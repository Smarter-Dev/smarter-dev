"""Thin email wrapper using Resend."""

from __future__ import annotations

import logging

from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)

_api_key_set = False


def _ensure_api_key() -> bool:
    """Configure the Resend API key from settings. Returns True if a key is available."""
    global _api_key_set
    if _api_key_set:
        return True

    settings = get_settings()
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY is not set — emails will be skipped")
        return False

    import resend

    resend.api_key = settings.resend_api_key
    _api_key_set = True
    return True


async def send_email(to: str, subject: str, html: str) -> None:
    """Send a transactional email via Resend.

    No-ops gracefully when the API key is not configured so local dev
    works without credentials.
    """
    if not _ensure_api_key():
        logger.info("Skipping email to %s (no API key)", to)
        return

    import resend

    try:
        resend.Emails.send(
            {
                "from": "Smarter Dev <noreply@smarter.dev>",
                "to": [to],
                "subject": subject,
                "html": html,
            }
        )
        logger.info("Confirmation email sent to %s", to)
    except Exception:
        logger.exception("Failed to send email to %s", to)
