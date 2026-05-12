"""Resources index page at /resources.

Lightweight hub that lists the curated guides we publish under /resources/*.
Currently a single entry (vibe coding) plus a "more coming" placeholder, so
the route doubles as a stable parent for breadcrumbs and future SEO.
"""

from __future__ import annotations

from litestar import get
from litestar.response import Template


@get("/resources")
async def resources_index() -> Template:
    return Template(
        "resources.html",
        context={
            "seo_meta": {
                "description": (
                    "Curated guides from Smarter Dev on modern dev tooling, "
                    "agentic coding, and AI-assisted software development."
                ),
                "canonical_url": "https://smarter.dev/resources",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Resources — Smarter Dev",
                "description": (
                    "Curated guides from Smarter Dev on modern dev tooling "
                    "and AI-assisted software development."
                ),
                "url": "https://smarter.dev/resources",
                "site_name": "Smarter Dev",
                "type": "website",
                "image": "",
            },
        },
    )
