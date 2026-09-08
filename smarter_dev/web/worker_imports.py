"""Register every worker job in a process that only submits.

A ``@handler`` registers its job type and payload model when its module is
imported. ``skrift workers run`` imports the controllers plus every module
listed under ``workers.imports`` in app.yaml before it starts consuming, but
the ASGI app and a standalone script do not, so ``submit()`` there knows a
payload only if something else happened to import its job module. Calling
``import_worker_job_modules`` at the process entry point makes the app.yaml
list the one owner of which job modules exist, for producers and consumers
alike.
"""

from __future__ import annotations

from importlib import import_module

from skrift.config import get_settings


def import_worker_job_modules() -> list[str]:
    """Import every module under ``workers.imports``; return their paths."""
    module_paths = [spec.split(":", 1)[0] for spec in get_settings().workers.imports]
    for module_path in module_paths:
        # The paths come from app.yaml, the same list `skrift workers run` imports.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        import_module(module_path)
    return module_paths
