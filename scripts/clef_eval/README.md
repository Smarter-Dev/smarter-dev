# Jev vs Clef

Cloudflare's Clef and Clef-flash (Workers AI) against Jev on the two Jev jobs
in production, Oct 1 2026: the search link's address check (`address_eval`
cases + holdout, 169 inputs) and result ranking (the 25 requests of the Sep 30
judge report, 25 results each). Every model gets the request production sends
Jev; calls run one at a time.

- `run.py address|ranking` writes `address.json` / `ranking.json`.
- `rank_split.py` reruns Clef ranking as two calls (Clef takes at most 64
  questions; ranking asks 76).
- `lat2.py` is a second 40-call latency sample; `analyze.py` prints the tables.

Needs `TYPESAFE_API_KEY`, `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`
(Workers AI Read) in `.env`; run from the repo root.

Outcome: stayed on Jev. Address check: Jev 166/169, Clef-flash 158/169 (9
opens the labels call searches), Clef 165/169; medians 170 / 310 / 527 ms.
Ranking: Jev 272 ms median, Clef-flash 1.3 s, Clef 2.7 s, at 3x and 8x
Jev's cost per search.
