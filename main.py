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
import yaml
from pathlib import Path as _Path

_config_path = _Path("app.yaml")
if _config_path.exists():
    with open(_config_path) as _f:
        _raw = yaml.safe_load(_f)
    _schema = (_raw or {}).get("db", {}).get("schema")
    if _schema:
        from skrift.db.base import Base
        Base.metadata.schema = _schema

from skrift.asgi import app  # noqa: F401

if __name__ == "__main__":
    import subprocess
    import sys

    sys.exit(subprocess.call([
        sys.executable, "-m", "hypercorn", "main:app",
        "--bind", "0.0.0.0:8000", "--reload",
    ]))
