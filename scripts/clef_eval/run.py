"""Jev vs Clef-flash vs Clef: same request bodies, sequential, timed."""
import asyncio, json, sys, time
from pathlib import Path
import httpx, httpx2, yaml
from dotenv import dotenv_values, load_dotenv

load_dotenv(".env"); env = dotenv_values(".env")
ACCT, CF = env["CLOUDFLARE_ACCOUNT_ID"].strip(), env["CLOUDFLARE_API_TOKEN"].strip()
TS = env["TYPESAFE_API_KEY"].strip()
OUT = Path(__file__).parent
TARGETS = {
    "jev": ("https://api.typesafe.ai/v1/systemone", TS, lambda b: {**b, "model": "jev-1.13.0"}, lambda j: j),
    "clef-flash": (f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/ai/run/@cf/cloudflare/clef-flash", CF,
                   lambda b: {k: v for k, v in b.items() if k != "model"}, lambda j: j["result"]),
    "clef": (f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/ai/run/@cf/cloudflare/clef", CF,
             lambda b: {k: v for k, v in b.items() if k != "model"}, lambda j: j["result"]),
}

async def call(client, name, body):
    url, key, shape, unwrap = TARGETS[name]
    for attempt in range(3):
        s = time.perf_counter()
        r = await client.post(url, headers={"Authorization": f"Bearer {key}", "Accept-Encoding": "gzip, deflate"}, json=shape(body), timeout=90)
        ms = (time.perf_counter() - s) * 1000
        if r.status_code == 200:
            return {"ms": ms, **unwrap(r.json())}
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            await asyncio.sleep(2 + attempt * 3); continue
        return {"ms": ms, "error": r.status_code, "body": r.text[:300]}

async def capture_rank_bodies(cases):
    """Production rank() builds each body; capture it from the wire."""
    from smarter_dev.web.web_search import ranking
    bodies = []
    orig = httpx2.AsyncClient.send
    async def send(self, request, *a, **k):
        bodies.append(json.loads(request.content)); return await orig(self, request, *a, **k)
    httpx2.AsyncClient.send = send
    for c in cases:
        await ranking.rank(c["request"], c["results"])
    httpx2.AsyncClient.send = orig
    return bodies

async def main():
    which = sys.argv[1]
    async with httpx.AsyncClient() as client:
        if which == "address":
            from smarter_dev.web.web_search import address
            template = json.loads((OUT / "address_template.json").read_text())
            labelled = []
            for f in ("cases.yaml", "holdout.yaml"):
                c = yaml.safe_load(Path("scripts/address_eval", f).read_text())
                labelled += [(t, "open", f) for t in c["open"]] + [(t, "search", f) for t in c["search"]]
            items = []
            for text, want, f in labelled:
                found = address.parse(text)
                if found is None or found.explicit: continue
                items.append({"text": text, "want": want, "set": f, "body": {**template, "state": address.material(found)}})
            for name in TARGETS:
                await call(client, name, items[0]["body"])  # warm connection
                for it in items:
                    it[name] = await call(client, name, it["body"])
                print(name, "done", flush=True)
            (OUT / "address.json").write_text(json.dumps(items))
        else:
            rep = json.load(open("scripts/search_query_eval/reports/judge-typesafe_jev-1.13.0-v7b-20260930-192632.json"))
            cases = [{"id": c["id"], "request": c["request"],
                      "results": [{"domain": x["domain"], "snippet": x["snippet"]} for x in c["results"]]} for c in rep["results"]]
            bodies = await capture_rank_bodies(cases)
            items = [{"id": c["id"], "n": len(c["results"]), "body": b} for c, b in zip(cases, bodies)]
            for name in TARGETS:
                await call(client, name, items[0]["body"])
                for it in items:
                    it[name] = await call(client, name, it["body"])
                print(name, "done", flush=True)
            (OUT / "ranking.json").write_text(json.dumps(items))

if __name__ == "__main__":
    asyncio.run(main())
