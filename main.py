"""Smarter Dev web application entry point.

Uses Skrift CMS as the ASGI framework. Controllers, page types, and
middleware are configured in app.yaml (or app.development.yaml when
SKRIFT_ENV=development).

The bot API at /api is served by the native Litestar controllers in
smarter_dev.web.api_native, registered in the app yamls.
"""

from smarter_dev.shared.observability import configure_observability
from smarter_dev.web.exception_handlers import install_exception_handlers

configure_observability("smarter-dev-web")
install_exception_handlers()

from skrift.asgi import app as skrift_app  # noqa: E402


class PrivateStorageGuard:
    """Keep named private stores out of Skrift's public storage middleware."""

    def __init__(self, wrapped):
        self.wrapped = wrapped

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        private_path = path.startswith("/storage/chat_attachments/") or path.startswith(
            "/storage/default/chat-attachments/"
        )
        if scope.get("type") == "http" and private_path:
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": b"Not found"})
            return
        await self.wrapped(scope, receive, send)


app = PrivateStorageGuard(skrift_app)

if __name__ == "__main__":
    import subprocess
    import sys

    sys.exit(
        subprocess.call(
            [
                sys.executable,
                "-m",
                "hypercorn",
                "main:app",
                "--bind",
                "0.0.0.0:8000",
                "--reload",
            ]
        )
    )
