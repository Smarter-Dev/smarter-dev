"""Native Litestar controllers owning the ``/api`` bot API.

Built during phase 04 of the legacy sunset
(docs/v2/legacy-sunset/04-api-rewrite.md) as one-for-one ports of the retired
FastAPI routers, then registered in ``app.yaml`` / ``app.development.yaml`` at
the atomic switchover that deleted ``smarter_dev/web/api/``. Every controller
declares its full ``/api/...`` path and guards with Skrift API-key auth
(``Bearer sk_...`` only).
"""
