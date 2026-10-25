"""Attachment file-type tiers, shared with the moderation coordinator.

Three-tier filtering approach:
1. Ignored extensions - Completely allowed, no action
2. Warn extensions - Send warning but don't delete
3. All others (blocked) - Delete + warn (or just warn if the author is protected
   staff: see smarter_dev.shared.staff_protection)

Only the pure classification lives here. The listener that used to act on it
(``check_attachment_filter``) was removed after the #240 cutover; the
moderation coordinator applies these tiers and acts through the moderation
gateway.
"""

from __future__ import annotations

import os
from collections.abc import Sequence


def attachment_extension(filename: str) -> str:
    """The extension the tiers compare, with its dot (``.exe``); ``""`` if none."""
    return os.path.splitext(filename.lower())[1].lower()


def classify_attachments(
    filenames: Sequence[str],
    *,
    ignored_extensions: Sequence[str] | None,
    warn_extensions: Sequence[str] | None,
) -> tuple[list[dict], list[dict]]:
    """Sort files into the warn and blocked tiers; ignored ones are dropped.

    Pure: the filter's own decision, shared with the moderation coordinator.
    Each entry is ``{"filename", "extension"}``, the extension shown as
    ``(no extension)`` when there is none.
    """
    warned: list[dict] = []
    blocked: list[dict] = []
    for filename in filenames:
        extension = attachment_extension(filename)
        if extension in (ignored_extensions or []):
            continue
        entry = {
            "filename": filename,
            "extension": extension if extension else "(no extension)",
        }
        if extension in (warn_extensions or []):
            warned.append(entry)
        else:
            # Everything that is neither ignored nor warned is blocked.
            blocked.append(entry)
    return warned, blocked
