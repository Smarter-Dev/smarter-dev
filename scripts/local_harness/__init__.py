"""Local end-to-end smoke harness for the legacy-sunset migration.

Boots the full app (Skrift, with the native ``/api`` Litestar controllers and
the ``/admin`` bot pages) against throwaway podman postgres/redis, seeds
representative data into the single database's ``skrift`` schema, and asserts
every bot-consumed API endpoint and every admin page still works.

Entry point: ``uv run python scripts/local_harness/run.py``
"""
