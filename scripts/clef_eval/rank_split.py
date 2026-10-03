import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import httpx
from run import call
async def main():
    items = json.load(open(Path(__file__).with_name("ranking.json")))
    async with httpx.AsyncClient() as client:
        for m in ("clef-flash", "clef"):
            await call(client, m, {"state": "x", "questions": {"q": {"type": "noul", "instructions": "Is this x?"}}})
            for it in items:
                qs = list(it["body"]["questions"].items()); half = (len(qs) + 1) // 2
                parts = [dict(qs[:half]), dict(qs[half:])]
                s = time.perf_counter()
                rs = await asyncio.gather(*(call(client, m, {"state": it["body"]["state"], "questions": p}) for p in parts))
                ms = (time.perf_counter() - s) * 1000
                errs = [r for r in rs if "error" in r]
                it[m] = errs[0] if errs else {"ms": ms, "answers": {**rs[0]["answers"], **rs[1]["answers"]},
                        "usage": {"input_tokens": sum(r["usage"]["input_tokens"] for r in rs)}, "calls": 2,
                        "slowest_half_ms": max(r["ms"] for r in rs)}
            print(m, "done", flush=True)
    json.dump(items, open(Path(__file__).with_name("ranking.json"), "w"))
asyncio.run(main())
