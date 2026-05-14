"""Resources index page at /resources.

Lightweight hub that lists the collections we publish under /resources/*.
Currently a single entry (agentic coding) plus a "more coming" placeholder,
so the route doubles as a stable parent for breadcrumbs and future SEO.
"""

from __future__ import annotations

from litestar import Request, get
from litestar.response import Template

from skrift.auth.session_keys import SESSION_USER_ID


@get("/resources")
async def resources_index(request: Request) -> Template:
    is_authenticated = bool(
        request.session and request.session.get(SESSION_USER_ID)
    )
    return Template(
        "resources.html",
        context={
            "is_authenticated": is_authenticated,
            "seo_meta": {
                "description": (
                    "Writing, courses, and tutorials from around the "
                    "web for engineers working with modern dev tooling "
                    "and AI-assisted software development."
                ),
                "canonical_url": "https://smarter.dev/resources",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Resources: Smarter Dev",
                "description": (
                    "Writing, courses, and tutorials from around the "
                    "web for engineers working with modern dev tooling "
                    "and AI-assisted software development."
                ),
                "url": "https://smarter.dev/resources",
                "site_name": "Smarter Dev",
                "type": "website",
                "image": "",
            },
        },
    )
