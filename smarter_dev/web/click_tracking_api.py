"""Generic click-tracking API.

Public POST endpoint that increments a server-side counter for a stable link key.
Designed to be called via `navigator.sendBeacon` from the site-wide click-tracker
(themes/smarterdev/static/js/click-tracker.js). Any anchor with
`data-track-key="..."` will fire a beacon at this endpoint just before the
browser navigates.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from urllib.parse import urlparse

from litestar import Controller, Request, post
from litestar.exceptions import HTTPException
from litestar.status_codes import (
    HTTP_204_NO_CONTENT,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_429_TOO_MANY_REQUESTS,
)
from msgspec import Struct
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from smarter_dev.web.models import TrackedLinkCounter

logger = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^[a-z0-9:_\-]{3,200}$")

# Per-IP rate limit: 30 clicks / 60s. Clicks are higher-volume than form
# submissions; this still blocks scripted abuse but stays out of the way for
# legitimate users.
_RATE_LIMIT = 30
_RATE_WINDOW = 60
_request_log: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    timestamps = _request_log[ip]
    _request_log[ip] = timestamps = [t for t in timestamps if now - t < _RATE_WINDOW]
    if len(timestamps) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Slow down.",
        )
    timestamps.append(now)


class TrackClickBody(Struct):
    key: str
    url: str


class ClickTrackingApiController(Controller):
    path = "/v2/api/track-click"

    @post("", status_code=HTTP_204_NO_CONTENT)
    async def track_click(
        self, data: TrackClickBody, request: Request, db_session: AsyncSession
    ) -> None:
        _check_rate_limit(request.client.host if request.client else "unknown")

        if not _KEY_RE.match(data.key):
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid key format.",
            )

        parsed = urlparse(data.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid URL.",
            )

        stmt = pg_insert(TrackedLinkCounter).values(
            key=data.key,
            url=data.url,
            count=1,
            last_clicked_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[TrackedLinkCounter.key],
            set_={
                "count": TrackedLinkCounter.count + 1,
                "url": stmt.excluded.url,
                "last_clicked_at": func.now(),
            },
        )
        await db_session.execute(stmt)
        await db_session.commit()
