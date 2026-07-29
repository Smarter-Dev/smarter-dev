"""Render TeX expressions through one long-lived local MathJax worker."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_LATEX_SOURCE_CHARS = 1_800
MAX_RENDERED_BYTES = 8 * 1024 * 1024
RENDER_TIMEOUT_SECONDS = 5.0


class LatexRenderError(RuntimeError):
    """Raised when the local renderer cannot produce a usable PNG."""


@dataclass(frozen=True)
class RenderedLatex:
    """An in-memory Discord attachment produced from one fenced equation."""

    data: bytes
    filename: str = "latex.png"
    mime_type: str = "image/png"


class LatexRenderer:
    """Serialises requests through a lazily started MathJax Node process."""

    def __init__(
        self,
        worker_path: Path | None = None,
        *,
        timeout_seconds: float = RENDER_TIMEOUT_SECONDS,
    ) -> None:
        repository_root = Path(__file__).resolve().parents[3]
        self.worker_path = worker_path or (
            repository_root / "latex_renderer" / "worker.mjs"
        )
        self.timeout_seconds = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0

    async def render(self, source: str) -> RenderedLatex:
        """Render ``source`` as PNG, restarting the worker after protocol errors."""
        source = source.strip()
        if not source:
            raise LatexRenderError("LaTeX source must not be empty")
        if len(source) > MAX_LATEX_SOURCE_CHARS:
            raise LatexRenderError(
                f"LaTeX source exceeds {MAX_LATEX_SOURCE_CHARS} characters"
            )

        async with self._lock:
            process = await self._ensure_process()
            self._next_id += 1
            request_id = self._next_id
            request = json.dumps(
                {"id": request_id, "source": source},
                ensure_ascii=False,
            ).encode("utf-8")

            try:
                assert process.stdin is not None
                assert process.stdout is not None
                process.stdin.write(request + b"\n")
                await process.stdin.drain()
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=self.timeout_seconds,
                )
                response: dict[str, Any] = json.loads(line)
            except TimeoutError as exc:
                await self._stop_process()
                raise LatexRenderError("LaTeX rendering timed out") from exc
            except (BrokenPipeError, EOFError, json.JSONDecodeError) as exc:
                await self._stop_process()
                raise LatexRenderError("LaTeX renderer stopped unexpectedly") from exc

            if response.get("id") != request_id:
                await self._stop_process()
                raise LatexRenderError("LaTeX renderer returned an invalid response")
            if response.get("error"):
                raise LatexRenderError(str(response["error"]))

            encoded = response.get("png")
            if not isinstance(encoded, str):
                await self._stop_process()
                raise LatexRenderError("LaTeX renderer returned no image")
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                await self._stop_process()
                raise LatexRenderError("LaTeX renderer returned invalid image data") from exc
            if not data or len(data) > MAX_RENDERED_BYTES:
                raise LatexRenderError("Rendered LaTeX image has an invalid size")
            return RenderedLatex(data=data)

    async def close(self) -> None:
        """Stop the worker if it was started."""
        async with self._lock:
            await self._stop_process()

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        process = self._process
        if process is not None and process.returncode is None:
            return process
        try:
            process = await asyncio.create_subprocess_exec(
                "node",
                str(self.worker_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(self.worker_path.parent),
            )
        except OSError as exc:
            raise LatexRenderError("LaTeX renderer could not start") from exc
        self._process = process
        return process

    async def _stop_process(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            process.kill()
            await process.wait()
