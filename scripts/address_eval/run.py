"""Score Jev's open-or-search call on scripts/address_eval/cases.yaml.

    uv run python scripts/address_eval/run.py [holdout.yaml]

Prints each case's probabilities and marks every case ``address.decide``
gets wrong. Typed text that isn't an address never reaches Jev."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from smarter_dev.web.web_search import address

HERE = Path(__file__).parent


async def main() -> None:
    load_dotenv(".env")
    name = sys.argv[1] if len(sys.argv) > 1 else "cases.yaml"
    cases = yaml.safe_load((HERE / name).read_text())
    labelled = [(text, "open") for text in cases["open"]] + [
        (text, "search") for text in cases["search"]
    ]
    client = address.build_client()
    gate = asyncio.Semaphore(8)

    async def score(text: str, want: str):
        found = address.parse(text)
        if found is None:
            return text, want, None, "not an address"
        if found.explicit:
            return text, want, 1.0, "explicit"
        async with gate:
            probabilities, _ = await address.ask_jev(found, client)
        opens = address.decide(probabilities)
        note = " ".join(f"{name}={p:.2f}" for name, p in probabilities.items())
        return text, want, probabilities["open"] if opens else min(probabilities["open"], address.OPEN_FROM - 0.001), note

    rows = await asyncio.gather(*(score(text, want) for text, want in labelled))
    await client.aclose()
    misses = 0
    for text, want, probability, note in rows:
        got = "open" if probability is not None and probability >= address.OPEN_FROM else "search"
        mark = "  " if got == want else "✗ "
        misses += got != want
        shown = "  -  " if probability is None else f"{probability:.3f}"
        print(f"{mark}{want:6} {shown}  {text}  {note}")
    print(f"\n{len(rows) - misses}/{len(rows)} right")


if __name__ == "__main__":
    asyncio.run(main())
