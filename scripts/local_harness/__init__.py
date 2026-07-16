"""Local end-to-end smoke harness for the legacy-sunset migration.

Boots the full app (Skrift + mounted legacy FastAPI + legacy Starlette admin)
against throwaway podman postgres/redis, seeds representative data, and
asserts every bot-consumed API endpoint and every admin page still works.

Entry point: ``uv run python scripts/local_harness/run.py``
"""
