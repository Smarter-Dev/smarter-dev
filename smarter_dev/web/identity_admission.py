"""Process-level admission gate, including direct NodePort/ClusterIP requests.

Mounted operator control is mandatory for the deployment wrapper. This is not a
retroactive gate on old images: they must be drained by the reviewed transition.
"""

from pathlib import Path

PHASE = Path("/var/run/site-identity-rollout/phase")


class IdentityAdmissionGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = "/" + "/".join(filter(None, scope.get("path", "").split("/")))
        guarded = scope["type"] == "http" and path.startswith(
            (
                "/account/delete",
                "/api/site/privacy/delete-account",
                "/api/site/identities",
            )
        )
        if guarded:
            try:
                released = PHASE.read_text().strip() == "complete"
            except OSError:
                released = False
            if not released:
                body = b'{"status_code":503,"detail":"Identity transition in progress"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"cache-control", b"private, no-store"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
