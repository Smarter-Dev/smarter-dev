"""ASGI middleware for extracting partial regions from full HTML responses.

When a request includes the X-Sk-Partial header, the middleware captures the
rendered HTML response, extracts elements marked with data-sk-partial attributes,
and returns a JSON envelope containing only the requested partials.

Controllers need zero modification — they always render full pages.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send


PARTIAL_HEADER = "x-sk-partial"
VARY_HEADER = "X-Sk-Partial"


class _PartialExtractor(HTMLParser):
    """Extracts content from elements with data-sk-partial attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.partials: dict[str, str] = {}
        self.title: str = ""
        self.meta: dict[str, str] = {}

        # Tracking state
        self._current_partial: str | None = None
        self._partial_depth: int = 0
        self._partial_content: list[str] = []
        self._in_title: bool = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)

        # Check for <meta name="sk-partials"> or <meta name="sk-page-type">
        if tag == "meta":
            name = attr_dict.get("name", "")
            content = attr_dict.get("content", "")
            if name == "sk-partials":
                self.meta["partial_names"] = content or ""
            elif name == "sk-page-type":
                self.meta["page_type"] = content or ""

        # Check for <title>
        if tag == "title":
            self._in_title = True
            self._title_parts = []

        # Check for data-sk-partial attribute
        partial_name = attr_dict.get("data-sk-partial")
        if partial_name and self._current_partial is None:
            self._current_partial = partial_name
            self._partial_depth = 1
            self._partial_content = []
            return

        # Track nesting depth inside a partial
        if self._current_partial is not None:
            self._partial_depth += 1
            # Reconstruct the tag inside the partial
            self._partial_content.append(self._reconstruct_tag(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        if self._in_title and tag == "title":
            self._in_title = False
            self.title = "".join(self._title_parts)
            return

        if self._current_partial is not None:
            self._partial_depth -= 1
            if self._partial_depth == 0:
                # We've closed the partial's root element
                self.partials[self._current_partial] = "".join(self._partial_content)
                self._current_partial = None
                self._partial_content = []
            else:
                self._partial_content.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._current_partial is not None:
            self._partial_content.append(data)

    def handle_entityref(self, name: str) -> None:
        text = f"&{name};"
        if self._current_partial is not None:
            self._partial_content.append(text)

    def handle_charref(self, name: str) -> None:
        text = f"&#{name};"
        if self._current_partial is not None:
            self._partial_content.append(text)

    def handle_comment(self, data: str) -> None:
        if self._current_partial is not None:
            self._partial_content.append(f"<!--{data}-->")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)

        if tag == "meta":
            name = attr_dict.get("name", "")
            content = attr_dict.get("content", "")
            if name == "sk-partials":
                self.meta["partial_names"] = content or ""
            elif name == "sk-page-type":
                self.meta["page_type"] = content or ""

        if self._current_partial is not None:
            self._partial_content.append(self._reconstruct_tag(tag, attrs, self_closing=True))

    @staticmethod
    def _reconstruct_tag(
        tag: str, attrs: list[tuple[str, str | None]], self_closing: bool = False
    ) -> str:
        parts = [f"<{tag}"]
        for name, value in attrs:
            if value is None:
                parts.append(f" {name}")
            else:
                escaped = value.replace("&", "&amp;").replace('"', "&quot;")
                parts.append(f' {name}="{escaped}"')
        if self_closing:
            parts.append(" />")
        else:
            parts.append(">")
        return "".join(parts)


def extract_partials(html: str) -> dict[str, Any]:
    """Parse HTML and extract partial regions.

    Returns a dict with keys: partials, title, meta.
    """
    extractor = _PartialExtractor()
    extractor.feed(html)
    return {
        "partials": extractor.partials,
        "title": extractor.title,
        "meta": extractor.meta,
    }


class PartialMiddleware:
    """ASGI middleware that intercepts responses for partial requests.

    When the incoming request has an X-Sk-Partial header, the full HTML
    response is captured, partials are extracted, and a JSON envelope
    is returned instead.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Check for the partial header
        headers = dict(
            (k.decode("latin-1").lower(), v.decode("latin-1"))
            for k, v in scope.get("headers", [])
        )

        if PARTIAL_HEADER not in headers:
            # Not a partial request — pass through, but add Vary header
            await self._passthrough_with_vary(scope, receive, send)
            return

        # Partial request — capture the response body
        response_started = False
        response_status = 200
        response_headers: list[tuple[bytes, bytes]] = []
        body_parts: list[bytes] = []

        async def capture_send(message: Message) -> None:
            nonlocal response_started, response_status, response_headers

            if message["type"] == "http.response.start":
                response_started = True
                response_status = message.get("status", 200)
                response_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    body_parts.append(body)

        await self.app(scope, receive, capture_send)

        # Check if the response is HTML
        content_type = ""
        for name, value in response_headers:
            if name.decode("latin-1").lower() == "content-type":
                content_type = value.decode("latin-1")
                break

        if "text/html" not in content_type or response_status >= 400:
            # Not HTML or error — send original response
            await self._send_captured(send, response_status, response_headers, body_parts)
            return

        # Extract partials from the HTML
        html = b"".join(body_parts).decode("utf-8", errors="replace")
        result = extract_partials(html)

        # Build JSON response
        json_body = json.dumps(result, ensure_ascii=False).encode("utf-8")

        # Build new headers (replace content-type, add vary)
        new_headers: list[tuple[bytes, bytes]] = []
        for name, value in response_headers:
            header_name = name.decode("latin-1").lower()
            if header_name in ("content-type", "content-length"):
                continue
            if header_name == "vary":
                existing = value.decode("latin-1")
                if VARY_HEADER.lower() not in existing.lower():
                    value = f"{existing}, {VARY_HEADER}".encode("latin-1")
                new_headers.append((name, value))
                continue
            new_headers.append((name, value))

        new_headers.append((b"content-type", b"application/json; charset=utf-8"))
        new_headers.append((b"content-length", str(len(json_body)).encode("latin-1")))

        # Add Vary header if not already present
        has_vary = any(h[0].decode("latin-1").lower() == "vary" for h in new_headers)
        if not has_vary:
            new_headers.append((b"vary", VARY_HEADER.encode("latin-1")))

        await send({
            "type": "http.response.start",
            "status": response_status,
            "headers": new_headers,
        })
        await send({
            "type": "http.response.body",
            "body": json_body,
        })

    async def _passthrough_with_vary(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Pass through the response but add Vary: X-Sk-Partial header."""
        vary_added = False

        async def add_vary_send(message: Message) -> None:
            nonlocal vary_added
            if message["type"] == "http.response.start" and not vary_added:
                vary_added = True
                headers = list(message.get("headers", []))
                has_vary = False
                for i, (name, value) in enumerate(headers):
                    if name.decode("latin-1").lower() == "vary":
                        has_vary = True
                        existing = value.decode("latin-1")
                        if VARY_HEADER.lower() not in existing.lower():
                            headers[i] = (name, f"{existing}, {VARY_HEADER}".encode("latin-1"))
                        break
                if not has_vary:
                    headers.append((b"vary", VARY_HEADER.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, add_vary_send)

    @staticmethod
    async def _send_captured(
        send: Send,
        status: int,
        headers: list[tuple[bytes, bytes]],
        body_parts: list[bytes],
    ) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        })
        await send({
            "type": "http.response.body",
            "body": b"".join(body_parts),
        })


def create_partial_middleware(app: ASGIApp) -> PartialMiddleware:
    """Factory function for Skrift middleware config."""
    return PartialMiddleware(app=app)
