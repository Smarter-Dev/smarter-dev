"""Scan subdomain controllers for scan.smarter.dev."""

from litestar import Controller, get
from litestar.response import Template


class ScanController(Controller):
    """Landing and result pages for the Scan research service."""

    path = "/"

    @get("/")
    async def landing(self) -> Template:
        """Scan landing page with search input and topic grid."""
        return Template("scan/landing.html")

    @get("/r/{result_id:str}")
    async def result(self, result_id: str) -> Template:
        """Research result detail page."""
        return Template("scan/result.html", context={"result_id": result_id})
