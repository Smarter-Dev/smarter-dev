"""Public privacy notice at /privacy.

The wording lives in ``smarter_dev/shared/privacy_notice.md`` so the
``/privacy`` bot command and the channel post quote the same text. No sign-in
and no database: the notice has to load for anyone, including someone whose
account has just been deleted.
"""

from __future__ import annotations

from litestar import get
from litestar.response import Template

from smarter_dev.shared.privacy_notice import LAST_UPDATED
from smarter_dev.shared.privacy_notice import NOTICE_TITLE
from smarter_dev.shared.privacy_notice import PRIVACY_PATH
from smarter_dev.shared.privacy_notice import notice_markdown

_DESCRIPTION = (
    "What Smarter Dev stores from Discord and smarter.dev, why, for how long, "
    "and how to have it deleted."
)


@get(PRIVACY_PATH)
async def privacy_notice() -> Template:
    url = f"https://smarter.dev{PRIVACY_PATH}"
    return Template(
        "privacy.html",
        context={
            "notice_title": NOTICE_TITLE,
            "notice_markdown": notice_markdown(),
            "last_updated": LAST_UPDATED,
            "seo_meta": {
                "description": _DESCRIPTION,
                "canonical_url": url,
                "robots": "index,follow",
            },
            "og_meta": {
                "title": f"{NOTICE_TITLE} · Smarter Dev",
                "description": _DESCRIPTION,
                "url": url,
                "site_name": "Smarter Dev",
                "type": "website",
                "image": "",
            },
        },
    )
