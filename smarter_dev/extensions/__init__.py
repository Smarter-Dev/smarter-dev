"""First-party extension system.

An *extension* is a code-reviewed bundle of admin-handler templates that ships
in the repo (``catalog/<slug>/``). Installing one materialises real
``admin_handlers`` rows for a guild with the admin-supplied config baked into
the scripts as string literals. See ``docs/v2/extensions/design.md``.

This package holds the pure, DB-free half of the system:

- :mod:`smarter_dev.extensions.schema` — the pydantic manifest models.
- :mod:`smarter_dev.extensions.rendering` — placeholder substitution + the
  render-time validation that every install/edit/update re-runs.
- :mod:`smarter_dev.extensions.registry` — catalog discovery + a cached
  singleton registry, fail-fast at import.

The DB-facing install service lives in
:mod:`smarter_dev.web.extension_installs`.
"""

from __future__ import annotations
