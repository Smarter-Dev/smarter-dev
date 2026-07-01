"""Generate example diagrams with the fixed brand palette and write an HTML report.

Runs the real image model (``image_generator.generate_image``) over a few
technical prompts and produces a single self-contained HTML file (PNGs embedded
as base64) so the palette + aesthetic can be eyeballed. The report shows the
palette swatches and the exact style preamble that gets prepended to every
prompt.

Usage:
    python scripts/image_palette_samples.py
Needs GEMINI_API_KEY or GOOGLE_API_KEY in the environment.
"""

from __future__ import annotations

import asyncio
import base64
import html
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import dotenv

    dotenv.load_dotenv()
except Exception:  # noqa: BLE001
    pass

from smarter_dev.bot.agents.image_generator import (
    DEFAULT_MODEL,
    MODEL_ENV_VAR,
    PALETTE,
    STYLE_PREAMBLE,
    generate_image,
)

OUT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "reports", "image_samples"
)

SAMPLES: list[tuple[str, str]] = [
    ("Binary search tree",
     "A binary search tree with the values 8, 3, 10, 1, 6, 14, 4, 7, 13 "
     "inserted in that order. Draw each node as a labeled box with its value, "
     "and straight arrows from each parent down to its left and right child."),
    ("TCP three-way handshake",
     "A sequence diagram of the TCP three-way handshake between a Client and a "
     "Server. Two labeled vertical lifelines; horizontal arrows for SYN "
     "(client to server), SYN-ACK (server to client), then ACK (client to "
     "server), each arrow labeled."),
    ("Big-O complexity growth",
     "A line chart comparing algorithmic complexity growth for O(1), O(log n), "
     "O(n), O(n log n), and O(n^2). Label the x-axis 'input size n' and the "
     "y-axis 'operations', draw each curve in a different accent color, and "
     "include a small legend."),
    ("Hash table with chaining",
     "A hash table with 8 numbered buckets drawn as a vertical array of boxes. "
     "Several buckets point rightward to linked lists of key/value node boxes "
     "to illustrate separate-chaining collision handling."),
    ("Web request architecture",
     "A system architecture diagram of a web request path: Browser, CDN, Load "
     "Balancer, API Server, and Database, each a labeled box connected left to "
     "right by arrows, with a Cache box beside the API Server."),
]


async def _gen(title: str, prompt: str) -> dict:
    print(f"  generating: {title} ...", flush=True)
    try:
        data, mime = await generate_image(prompt)
        print(f"    ok — {len(data)} bytes ({mime})", flush=True)
        return {"title": title, "prompt": prompt, "data": data, "mime": mime, "error": None}
    except Exception as e:  # noqa: BLE001
        print(f"    ERROR — {type(e).__name__}: {e}", flush=True)
        return {"title": title, "prompt": prompt, "data": None, "mime": None, "error": f"{type(e).__name__}: {e}"}


def _swatches() -> str:
    cells = []
    for name, hexv in PALETTE.items():
        cells.append(
            f'<div class="sw"><div class="chip" style="background:{hexv}"></div>'
            f'<div class="meta"><span class="nm">{html.escape(name)}</span>'
            f'<span class="hx">{hexv}</span></div></div>'
        )
    return "\n".join(cells)


def _cards(results: list[dict]) -> str:
    out = []
    for r in results:
        if r["error"]:
            body = f'<div class="err">generation failed: {html.escape(r["error"])}</div>'
        else:
            b64 = base64.b64encode(r["data"]).decode("ascii")
            body = f'<img alt="{html.escape(r["title"])}" src="data:{r["mime"]};base64,{b64}"/>'
        out.append(
            '<section class="card">'
            f'<h3>{html.escape(r["title"])}</h3>'
            f'{body}'
            f'<details><summary>prompt</summary><pre>{html.escape(r["prompt"])}</pre></details>'
            '</section>'
        )
    return "\n".join(out)


def build_html(results: list[dict], model: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Image palette samples</title>
<style>
  :root {{
    --bg:{PALETTE['background']}; --card:{PALETTE['card']}; --text:{PALETTE['text']};
    --muted:{PALETTE['muted_text']}; --cyan:{PALETTE['cyan']};
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
    font-family:'Courier New',monospace; padding:32px; line-height:1.5; }}
  h1 {{ color:var(--cyan); letter-spacing:.12em; text-transform:uppercase;
    font-size:1.2rem; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:.8rem; margin-bottom:24px; }}
  h2 {{ color:var(--cyan); letter-spacing:.1em; text-transform:uppercase;
    font-size:.8rem; border-bottom:1px solid rgba(0,212,255,.15);
    padding-bottom:6px; margin:32px 0 16px; }}
  .palette {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
    gap:10px; }}
  .sw {{ display:flex; align-items:center; gap:10px; background:var(--card);
    border:1px solid rgba(0,212,255,.10); padding:8px; }}
  .chip {{ width:34px; height:34px; flex:0 0 34px; box-shadow:0 0 12px rgba(0,212,255,.10); }}
  .meta {{ display:flex; flex-direction:column; }}
  .nm {{ font-size:.78rem; }} .hx {{ color:var(--muted); font-size:.7rem; }}
  pre.preamble {{ background:var(--card); border:1px solid rgba(0,212,255,.10);
    padding:14px; white-space:pre-wrap; color:var(--muted); font-size:.74rem; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr));
    gap:20px; }}
  .card {{ background:var(--card); border:1px solid rgba(0,212,255,.12);
    padding:14px; box-shadow:inset 0 0 20px rgba(0,212,255,.04); }}
  .card h3 {{ color:var(--text); letter-spacing:.06em; font-size:.85rem;
    margin:0 0 12px; }}
  .card img {{ width:100%; height:auto; display:block; border:1px solid rgba(0,212,255,.08); }}
  details {{ margin-top:10px; }} summary {{ color:var(--cyan); cursor:pointer; font-size:.72rem; }}
  details pre {{ white-space:pre-wrap; color:var(--muted); font-size:.72rem; }}
  .err {{ color:{PALETTE['red']}; font-size:.8rem; }}
</style></head>
<body>
  <h1>Image palette samples</h1>
  <div class="sub">{now} &middot; model <code>{html.escape(model)}</code> &middot;
    fixed neon palette baked into every generation</div>

  <h2>Palette</h2>
  <div class="palette">{_swatches()}</div>

  <h2>Style preamble (prepended to every prompt)</h2>
  <pre class="preamble">{html.escape(STYLE_PREAMBLE)}</pre>

  <h2>Samples</h2>
  <div class="grid">{_cards(results)}</div>
</body></html>
"""


async def main() -> int:
    if not any(os.getenv(k) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY")):
        print("No GEMINI_API_KEY / GOOGLE_API_KEY — cannot run.")
        return 1
    model = os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL)
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Generating {len(SAMPLES)} sample diagrams with {model}...\n")

    # Sequential — image models tend to have tighter rate limits than text.
    results = []
    for title, prompt in SAMPLES:
        results.append(await _gen(title, prompt))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.abspath(os.path.join(OUT_DIR, f"palette_samples_{stamp}.html"))
    with open(path, "w") as f:
        f.write(build_html(results, model))
    ok = sum(1 for r in results if not r["error"])
    print(f"\n{ok}/{len(results)} generated. Report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
