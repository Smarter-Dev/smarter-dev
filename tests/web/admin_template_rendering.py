"""Render a real admin template with only its layout stubbed out.

Admin pages extend ``admin/base.html``, which pulls in the whole site chrome —
navigation, the authenticated user, flash plumbing — none of which a template
assertion cares about. Tests that want to prove a page renders the rows it was
handed load the real template file with that one layout replaced by an empty
shell, so the assertions read the page's own markup.

One owner for the templates root, the layout stub, autoescaping and the globals
the admin templates call, so a template that starts calling a new global is
fixed here once instead of in every test module that renders admin pages. Tests
that assert on a template's raw source (a sidebar link, say) name it the same
slash-separated way through :func:`admin_template_source`, so no test module
holds a path into the templates tree.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import ChoiceLoader
from jinja2 import DictLoader
from jinja2 import Environment
from jinja2 import FileSystemLoader

_TEMPLATES_ROOT = Path(__file__).resolve().parents[2] / "templates"

_STUBBED_LAYOUT = {
    "admin/base.html": "{% block title %}{% endblock %}{% block admin_content %}{% endblock %}"
}


def admin_template_source(template_name: str) -> str:
    """The raw, unrendered source of ``template_name``."""
    return (_TEMPLATES_ROOT / template_name).read_text()


def render_admin_template(template_name: str, **context) -> str:
    """The rendered HTML of ``template_name`` given ``context``."""
    environment = Environment(  # nosemgrep: python.flask.security.xss.audit.direct-use-of-jinja2.direct-use-of-jinja2
        loader=ChoiceLoader(
            [DictLoader(_STUBBED_LAYOUT), FileSystemLoader(_TEMPLATES_ROOT)]
        ),
        autoescape=True,
    )
    environment.globals.update(
        site_name=lambda: "Smarter Dev",
        csp_nonce=lambda: "test-nonce",
    )
    return environment.get_template(template_name).render(**context)
