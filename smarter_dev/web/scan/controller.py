"""Scan subdomain controllers for scan.smarter.dev."""

import logging

from litestar import Controller, Request, get
from litestar.response import Template
from skrift.lib.notifications import subscribe_source, _ensure_nid
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.scan.crud import ResearchSessionOperations

logger = logging.getLogger(__name__)
ops = ResearchSessionOperations()


class ScanController(Controller):
    """Landing and result pages for the Scan research service."""

    path = "/"

    @get("/")
    async def landing(self) -> Template:
        """Scan landing page with search input and topic grid."""
        return Template("scan/landing.html")

    @get("/r/{result_id:str}")
    async def result(
        self, request: Request, result_id: str, db_session: AsyncSession
    ) -> Template:
        """Research result detail page with live updates for running sessions."""
        session_data = await ops.get_session(db_session, result_id)

        if session_data and session_data.status == "running":
            # Subscribe the visitor's Skrift session to the research source
            # so they receive timeseries notifications via /notifications/stream
            nid = _ensure_nid(request)
            if nid:
                await subscribe_source(f"session:{nid}", f"research:{result_id}")

        return Template(
            "scan/result.html",
            context={
                "result_id": result_id,
                "session": session_data,
            },
        )
