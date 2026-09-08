"""Register the handler fire jobs in a process that only submits them.

A ``@handler`` registers its job type and payload model when its module is
imported. ``skrift workers run`` imports the controllers plus every module
listed under ``workers.imports`` in app.yaml before it starts consuming, but
the ASGI app and a standalone script do not, so ``submit()`` there knows a
payload only if something else happened to import its job module. The two
submit-only entry points, ``main.py`` and ``scripts/handler_sweep.py``, call
``register_handler_fire_jobs`` at process start.

Only the two fire jobs are imported here, not the whole ``workers.imports``
list: the chat, blogging and resources job modules pull in their agent graphs,
about 100 MiB the web pod's 384Mi limit cannot absorb (the 2026-09-08 deploy
that tried was OOMKilled on startup). The fire jobs cost nothing extra, since
dispatch already imports everything they depend on.
"""

from __future__ import annotations

from importlib import import_module

HANDLER_FIRE_JOB_MODULES = (
    "smarter_dev.web.handlers_jobs",
    "smarter_dev.web.admin_handlers_jobs",
)


def register_handler_fire_jobs() -> tuple[str, ...]:
    """Import both fire job modules; return their paths."""
    for module_path in HANDLER_FIRE_JOB_MODULES:
        # A fixed pair of first-party modules, named above, not user input.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        import_module(module_path)
    return HANDLER_FIRE_JOB_MODULES
