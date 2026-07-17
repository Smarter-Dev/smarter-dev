"""Native Litestar controllers replacing the legacy FastAPI ``/api`` mount.

Built incrementally during phase 04 of the legacy sunset
(docs/v2/legacy-sunset/04-api-rewrite.md). Every controller declares its
final ``/api/...`` path but is NOT registered in ``app.yaml`` yet — the legacy
FastAPI mount keeps owning ``/api`` until the atomic switchover commit. Until
then these controllers exist only for isolated parity tests.
"""
