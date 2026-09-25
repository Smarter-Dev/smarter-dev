"""Peak memory of one (or N concurrent) real web_read attachment reads (#25).

Run as a script in a fresh process by ``test_media_read_memory``; prints JSON.

argv: fixture path, attachment filename, [concurrency]. The real ``web_read``
and ``fetch_bytes`` run over a fake 64 KB-chunked stream (Content-Length sent).
The media reader is the real pydantic-ai OpenAI Responses model, answered by an
httpx MockTransport, so the SDK's own request serialization is included. Audio
uses the same OpenAI model; Gemini's SDK is not exercised. Text summaries are a
no-op that keeps the text. Growth is VmHWM from before the download.

``combined_mib`` is what the container sees: a 2 ms sampler adds this
process's RSS growth to the RSS of every child it has (the PDF parser), at the
same instant (the PDF parser or image child), counted from the RSS before the read (so it can exceed the VmHWM
figure when the high-water mark was already above RSS). Sampling can miss a
peak between samples, so it is a lower bound; ``child_peak_mib`` is the children's own sampled peak. (Not
``RUSAGE_CHILDREN``: a forked child's ru_maxrss starts at this process's size.)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from smarter_dev.bot.agents import chat_tools
from smarter_dev.bot.agents import media_reader
from smarter_dev.bot.utils import web_fetch

RESPONSE = {
    "id": "r",
    "object": "response",
    "created_at": 1,
    "status": "completed",
    "model": "gpt-6-luna",
    "output": [
        {
            "type": "message",
            "id": "m",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "described", "annotations": []}],
        }
    ],
    "usage": {
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    },
    "parallel_tool_calls": True,
    "tool_choice": "auto",
    "tools": [],
}


def hwm_kib() -> int:
    with open("/proc/self/status") as status:
        return next(int(line.split()[1]) for line in status if line.startswith("VmHWM:"))


def rss_kib(pid: int | str = "self") -> int:
    try:
        with open(f"/proc/{pid}/status") as status:
            return next(int(line.split()[1]) for line in status if line.startswith("VmRSS:"))
    except (OSError, StopIteration):  # exited between listing and reading
        return 0


def children(pid: int) -> list[int]:
    found: list[int] = []
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return found
    for task in tasks:
        try:
            with open(f"/proc/{pid}/task/{task}/children") as listing:
                found += [int(child) for child in listing.read().split()]
        except OSError:
            continue
    return found + [grand for child in found for grand in children(child)]


def is_bounded_child(pid: int) -> bool:
    # Only the parser and image children are memory a read adds. Libraries also shell
    # out (platform's `uname -p` and `file -b`), and a child caught between
    # fork and exec reports this process's shared pages as its own RSS.
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as cmdline:
            args = cmdline.read()
        return b"smarter_dev.shared.pdf_text" in args or b"smarter_dev.shared.image_child" in args
    except OSError:
        return False


class Sampler(threading.Thread):
    """Peak of (this process's RSS growth + its children's RSS), every 2 ms."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.base = rss_kib()
        self.peak = 0
        self.child_peak = 0
        self.running = True

    def run(self) -> None:
        me = os.getpid()
        while self.running:
            child = sum(rss_kib(c) for c in children(me) if is_bounded_child(c))
            self.child_peak = max(self.child_peak, child)
            self.peak = max(self.peak, rss_kib() - self.base + child)
            time.sleep(0.002)


def main(path: str, name: str, concurrency: int) -> dict:
    size = os.path.getsize(path)
    sent: list[int] = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/octet-stream", "content-length": str(size)}

        async def aiter_bytes(self):
            with open(path, "rb") as file:
                while chunk := file.read(65536):
                    yield chunk

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *exc):
            return False

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return Stream()

    async def answer(request: httpx.Request) -> httpx.Response:
        sent.append(len(await request.aread()))
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=RESPONSE)

    def model() -> OpenAIResponsesModel:
        client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        return OpenAIResponsesModel(
            "gpt-6-luna", provider=OpenAIProvider(api_key="x", http_client=client)
        )

    held: list[str] = []

    async def summarize(url, instruction, content, title=""):
        held.append(content)
        return {"url": url, "summary": f"text {len(content)} chars"}

    async def no_status(*args, **kwargs):
        pass

    url = f"https://cdn.discordapp.com/attachments/1/2/{name}?ex=1&is=2&hm=3"
    ctx = SimpleNamespace(deps=SimpleNamespace(channel_id=1, bot=None))
    audio = Agent(model(), output_type=str)

    async def run() -> dict:
        media_reader._media_reader_agent = None
        with (
            patch.object(web_fetch, "httpx", SimpleNamespace(AsyncClient=Client)),
            patch.object(media_reader, "_build_model", model),
            patch.object(media_reader, "get_audio_reader_agent", lambda: audio),
            patch.object(chat_tools, "_summarize_text", summarize),
            patch.object(chat_tools, "_post_status", no_status),
        ):
            media_reader.get_media_reader_agent()
            base = hwm_kib()
            sampler = Sampler()
            sampler.start()
            outs = await asyncio.gather(
                *(chat_tools.web_read(ctx, url, "read") for _ in range(concurrency))
            )
            sampler.running = False
            sampler.join()
            peak = hwm_kib()
        first = outs[0]
        return {
            "name": name,
            "n": concurrency,
            "input_mb": round(size / 1e6, 1),
            "growth_mib": round((peak - base) / 1024, 1),
            "combined_mib": round(max(sampler.peak, peak - base) / 1024, 1),
            "child_peak_mib": round(sampler.child_peak / 1024, 1),
            "request_mb": round(max(sent, default=0) / 1e6, 1),
            "error": first.get("error", ""),
            "result": (first.get("summary") or first.get("detail") or "")[:80],
        }

    return asyncio.run(run())


if __name__ == "__main__":
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    print(json.dumps(main(sys.argv[1], sys.argv[2], count)))
