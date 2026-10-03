import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import httpx
from run import call
async def main():
    items = json.load(open(Path(__file__).with_name("address.json")))[:40]
    async with httpx.AsyncClient() as c:
        for m in ("jev", "clef-flash", "clef"):
            await call(c, m, items[0]["body"])
            ms = sorted([(await call(c, m, it["body"]))["ms"] for it in items])
            print(f"{m:10} n=40 p50={ms[20]:.0f} p90={ms[36]:.0f} max={ms[-1]:.0f} ms, over 1 s: {sum(x>1000 for x in ms)}", flush=True)
asyncio.run(main())
