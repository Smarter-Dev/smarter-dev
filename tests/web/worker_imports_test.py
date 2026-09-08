"""A process that only submits still has to know every worker job.

``@handler`` registers a job type and its payload model when the job module is
imported. ``skrift workers run`` imports the controllers plus every module under
``workers.imports`` before consuming; the ASGI app and a standalone script do
not, so ``submit()`` in those processes raises ``KeyError`` for any payload
whose job module nothing happened to import. Prod lost every handler fire this
way on 2026-09-07 when the fire payloads moved out of the job modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRE_JOB_MODULES = (
    "smarter_dev.web.handlers_jobs",
    "smarter_dev.web.admin_handlers_jobs",
)
SUBMIT_ONLY_ENTRY_POINTS = (
    REPO_ROOT / "main.py",
    REPO_ROOT / "scripts" / "handler_sweep.py",
)


def _workers_imports(config_name: str) -> list[str]:
    config = yaml.safe_load((REPO_ROOT / config_name).read_text())
    return config["workers"]["imports"]


def test_both_configs_list_the_fire_job_modules():
    for config_name in ("app.yaml", "app.development.yaml"):
        listed = _workers_imports(config_name)
        for module in FIRE_JOB_MODULES:
            assert module in listed, f"{config_name} does not list {module}"


def test_importing_the_listed_modules_registers_both_fire_payloads():
    """Run in a fresh interpreter: the registry is process-global, so an
    in-process check would pass on the strength of whatever another test
    imported first."""
    script = textwrap.dedent(
        """
        from skrift.workers.registry import registry

        from smarter_dev.web.handler_fire_payloads import AdminHandlerFirePayload
        from smarter_dev.web.handler_fire_payloads import HandlerFirePayload
        from smarter_dev.web.worker_imports import import_worker_job_modules

        imported = import_worker_job_modules()
        assert "smarter_dev.web.handlers_jobs" in imported, imported
        assert "smarter_dev.web.admin_handlers_jobs" in imported, imported
        standard = registry.job_type_for_payload(HandlerFirePayload(handler_id="h"))
        admin = registry.job_type_for_payload(
            AdminHandlerFirePayload(admin_handler_id="a")
        )
        print(standard, admin)
        """
    )
    env = {
        **os.environ,
        "SKRIFT_ENV": "development",
        "SECRET_KEY": "test-secret-key",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "handlers.fire admin_handlers.fire"


def test_every_submit_only_entry_point_imports_the_job_modules():
    for entry_point in SUBMIT_ONLY_ENTRY_POINTS:
        source = entry_point.read_text()
        assert "import_worker_job_modules()" in source, entry_point
