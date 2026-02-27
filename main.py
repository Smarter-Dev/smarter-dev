"""Smarter Dev web application entry point.

Uses Skrift CMS as the ASGI framework. Controllers, page types, and
middleware are configured in app.yaml (or app.development.yaml when
SKRIFT_ENV=development).

The FastAPI API is mounted at /api via the ASGI handler registered
in smarter_dev.web.controllers.
"""

# Set the database schema BEFORE importing the ASGI app so that the
# startup setup-check query qualifies table names (e.g. skrift.settings)
# and succeeds through pgbouncer where search_path cannot be changed.
from skrift.db.base import Base
from skrift.config import get_settings as _get_settings

_cfg = _get_settings()
if _cfg.db.db_schema:
    Base.metadata.schema = _cfg.db.db_schema

from skrift.asgi import app  # noqa: F401

if __name__ == "__main__":
    import subprocess
    import sys

    sys.exit(subprocess.call([
        sys.executable, "-m", "hypercorn", "main:app",
        "--bind", "0.0.0.0:8000", "--reload",
    ]))
