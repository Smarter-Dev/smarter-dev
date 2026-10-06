"""Public Terms of Service at /terms.

The wording lives in ``smarter_dev/shared/terms_of_service.md``. No sign-in
and no database: the terms have to load for anyone, including the Discord
developer portal's link checker.
"""

from __future__ import annotations

from litestar import get
from litestar.response import Template

from smarter_dev.shared.terms_of_service import LAST_UPDATED
from smarter_dev.shared.terms_of_service import TERMS_PATH
from smarter_dev.shared.terms_of_service import TERMS_TITLE
from smarter_dev.shared.terms_of_service import terms_markdown
from smarter_dev.shared.terms_of_service import terms_url

_DESCRIPTION = (
    "The terms for using the Smarter Dev Discord server, its bot and smarter.dev."
)


@get(TERMS_PATH)
async def terms_of_service() -> Template:
    url = terms_url()
    return Template(
        "terms.html",
        context={
            "terms_title": TERMS_TITLE,
            "terms_markdown": terms_markdown(),
            "last_updated": LAST_UPDATED,
            "seo_meta": {
                "description": _DESCRIPTION,
                "canonical_url": url,
                "robots": "index,follow",
            },
            "og_meta": {
                "title": f"{TERMS_TITLE} · Smarter Dev",
                "description": _DESCRIPTION,
                "url": url,
                "site_name": "Smarter Dev",
                "type": "website",
                "image": "",
            },
        },
    )
