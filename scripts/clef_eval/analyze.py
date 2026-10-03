import json, statistics
from pathlib import Path
OUT = Path(__file__).parent
PRICE = {"jev": 0.042, "clef-flash": 0.09, "clef": 0.24}
MODELS = list(PRICE)

def pct(xs, p):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(p * len(xs)))]

def lat(rows):
    ms = [r["ms"] for r in rows if "error" not in r]
    return f"p50 {pct(ms,.5):.0f} ms, p90 {pct(ms,.9):.0f} ms, max {max(ms):.0f} ms"

def spearman(a, b):
    def ranks(x):
        order = sorted(range(len(x)), key=lambda i: x[i]); r = [0.0] * len(x); i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]: j += 1
            for k in range(i, j + 1): r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    if len(set(ra)) < 2 or len(set(rb)) < 2: return None
    return statistics.correlation(ra, rb)

if (OUT / "address.json").exists():
    items = json.loads((OUT / "address.json").read_text())
    print(f"## Address check: {len(items)} inputs that reach the model")
    for m in MODELS:
        rows = [it[m] for it in items]
        errs = [r for r in rows if "error" in r]
        ok = [(it, it[m]) for it in items if "error" not in it[m]]
        def p(r, q): return r["answers"][q]["noul"]
        def opens(r, t=0.5): return p(r, "open") >= t and p(r, "file") < 0.5 and p(r, "code") < 0.5
        right = sum(opens(r) == (it["want"] == "open") for it, r in ok)
        false_open = [it["text"] for it, r in ok if opens(r) and it["want"] == "search"]
        missed = [it["text"] for it, r in ok if not opens(r) and it["want"] == "open"]
        per_set = {s: f"{sum(opens(r) == (it['want']=='open') for it, r in ok if it['set']==s)}/{sum(1 for it,_ in ok if it['set']==s)}" for s in ("cases.yaml", "holdout.yaml")}
        best = max(((sum(opens(r, t) == (it["want"] == "open") for it, r in ok), t) for t in [x / 100 for x in range(5, 96)]))
        toks = [r["usage"]["input_tokens"] for _, r in ok]
        print(f"\n### {m}\n- errors: {len(errs)} {errs[:1]}\n- right at Jev's thresholds: {right}/{len(ok)} ({per_set})"
              f"\n- best open threshold: {best[1]:.2f} -> {best[0]}/{len(ok)}"
              f"\n- opened, should search ({len(false_open)}): {false_open[:12]}\n- searched, should open ({len(missed)}): {missed[:12]}"
              f"\n- latency: {lat(rows)}\n- input tokens/check: {statistics.mean(toks):.0f} -> ${statistics.mean(toks)*PRICE[m]/1e6:.7f}/check")

if (OUT / "ranking.json").exists():
    items = json.loads((OUT / "ranking.json").read_text())
    rep = {c["id"]: c for c in json.load(open("scripts/search_query_eval/reports/judge-typesafe_jev-1.13.0-v7b-20260930-192632.json"))["results"]}
    def view(r, n):
        a = r["answers"]; sc = []; rel = []; loc = []
        for i in range(1, n + 1):
            s = a[f"result_{i}_relevance"]["score"]; l = a[f"result_{i}_local"]["noul"] >= 0.5
            s = max(0.0, s - 1.0) if l else s
            sc.append(s); rel.append(s >= 0.7); loc.append(l)
        return {"scores": sc, "relevant": rel, "best": int(a["best"]["choice"].strip("RESULT []"))}
    print(f"\n## Ranking: {len(items)} requests x {items[0]['n']} results")
    base = {it["id"]: view(it["jev"], it["n"]) for it in items if "error" not in it["jev"]}
    # Noise floor: today's Jev against Jev's own run on Sep 30 (same results; that prompt had no local question).
    old = {i: {"scores": [x["relevance_score"] for x in rep[i]["results"]], "best": rep[i]["best"]} for i in base}
    rho = [spearman(base[i]["scores"], old[i]["scores"]) for i in base]; rho = [x for x in rho if x is not None]
    print(f"- noise floor, Jev today vs Jev Sep 30: score rank correlation {statistics.mean(rho):.2f}, same top pick {sum(base[i]['best']==old[i]['best'] for i in base)}/{len(base)}")
    for m in MODELS:
        rows = [it[m] for it in items]; errs = [r for r in rows if "error" in r]
        ok = [it for it in items if "error" not in it[m] and it["id"] in base]
        toks = [it[m]["usage"]["input_tokens"] for it in items if "error" not in it[m]]
        line = f"\n### {m}\n- errors: {len(errs)} {errs[:1]}\n- latency: {lat(rows)}\n- input tokens/call: {statistics.mean(toks):.0f} -> ${statistics.mean(toks)*PRICE[m]/1e6:.5f}/call"
        if m != "jev":
            v = {it["id"]: view(it[m], it["n"]) for it in ok}
            rho = [spearman(base[i]["scores"], v[i]["scores"]) for i in v]; rho = [x for x in rho if x is not None]
            same_best = sum(base[i]["best"] == v[i]["best"] for i in v)
            agree = [a == b for i in v for a, b in zip(base[i]["relevant"], v[i]["relevant"])]
            jev_top3 = sum(v[i]["best"] in sorted(range(1, len(base[i]['scores'])+1), key=lambda k: -base[i]['scores'][k-1])[:3] for i in v)
            nrel = [sum(v[i]["relevant"]) for i in v]; jrel = [sum(base[i]["relevant"]) for i in v]
            line += (f"\n- vs Jev: score rank correlation {statistics.mean(rho):.2f}; same top pick {same_best}/{len(v)}; its pick in Jev's top 3 {jev_top3}/{len(v)}"
                     f"\n- relevant/not agrees with Jev on {sum(agree)}/{len(agree)} results ({100*sum(agree)/len(agree):.0f}%); relevant per request {statistics.mean(nrel):.1f} vs Jev {statistics.mean(jrel):.1f}")
        print(line)
