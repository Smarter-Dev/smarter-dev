# Site resources API

`/api/site/resources` lets the site Worker fill in chat answer cards with the
same resource metadata Python's own answer renderer uses. Given the URLs an
answer cites, it returns the catalog rows `sdanswer` reads
(`_load_sources`, exact URL match on `resource_sources`) with the fields of
`_source_to_article_card`. It is read-only and holds no member data.

Every route requires the site service principal (`principal_type="service"`,
`service_name="site"`) with an explicit **site-resources** scope, and the
owner's `site-service` role. A browser never calls these routes.

**Rate limit.** The route does not use the bot API's per-key limiter
(`MultiTierRateLimitMiddleware`), whose bucket the site memory routes share.
It has its own rule in Skrift's global limiter (`rate_limit.rules` in
`app.yaml`): namespace `site-resources`, `POST` on this exact path, keyed by
client IP, **600 requests a minute per IP**. Every other route stays on the
IP-keyed default (60 a minute) in its own namespace, so card lookups never
spend the site memory routes' budget.

The rule is not keyed by API key (`key: api_key`): Skrift 0.2.1a3 keys that on
the raw, unverified bearer token, which puts the site key into Redis key names
(`…:ratelimit:site-resources:apikey:sk_…`) and gives every invented token a
fresh budget. The cost of IP keying is that a leaked key is not bounded across
addresses; a leaked key means a re-mint anyway, and this route only serves
public catalog data.

600 a minute is 600 lookup requests: 120 to 600 enrichment events a minute,
depending on how many batches each takes, well above today's site traffic. The
Worker makes one batched call (at most 5 batches of 100 URLs) per chat page
load, per earlier-history page, per version switch, per shared-answer view
(also capped at 20 a minute per visitor on the Worker) and per completed
answer. On a 429 the Worker shows its stored cards, so hitting the limit only
costs freshness.

| Route | Body / result |
| ----- | ------------- |
| `POST /api/site/resources/sources` | `{urls}` → 200 `{sources, omitted}`. |

`urls` is a list of 1–100 strings, each 1–500 characters; any other field,
a non-string entry or a list outside those limits is a 422 with the API's
usual validation body. Repeated URLs are looked up once.

```json
{
  "sources": [
    {"url": "…", "title": "…", "byline": "…", "blurb": "…",
     "learning_type": "…", "track_key": "…"}
  ],
  "omitted": ["…"]
}
```

`sources` holds one entry per requested URL found in the catalog, in the order
of the request. Each entry is exactly the article card Python's renderer
builds (`_source_to_article_card`) without its `type`. `byline` and `blurb` are
`""` when empty. URLs must match exactly (no normalisation); unknown URLs are
left out of both lists, so the Worker decides what to show for them.

If several catalog rows share a URL, the one with the lowest `track_key` is
used, here and in the renderer (`sdanswer.pick_sources`). `track_key` is
unique, so the pick does not depend on row order.
`scripts/parity/pick_sources.py <catalog.json>` prints that pick for every
duplicated URL in a catalog export, as a fixture for the site's own pick.

**Limits.** The card caps each field in one place, for the renderer and this
route alike: `title` 512 characters, `byline` 256, `blurb` 2,000,
`learning_type` 32, `track_key` 200. Longer values are cut to the cap. `url`
is never cut, so it stays a working link; it is bounded here because only
requested URLs of at most 500 characters can match. The whole encoded JSON
body is at most 1 MiB: sources are added in request order, and once the next
one would take the body past 1 MiB, it and every later found URL go in
`omitted`, in request order. `omitted` is always present and normally `[]`.
An omitted URL was **not answered here**; it does not mean the URL is missing
from the catalog. The Worker treats omitted URLs as unknown and falls back for
them.

## Enabling it

The site guard (`site_guards.py`) checks the key's own
`scoped_permission_list`, so the `site-service` role entry alone does not
authorize the existing site key. No re-mint is needed: add `site-resources` to
the existing site key's scopes in place, which keeps its secret (and the
Worker's `SKRIFT_SITE_KEY`) unchanged. Either use the Skrift
`/admin/api-keys/{id}/edit` form, or run the one-off Job in [Adding a scope to
the existing site key](admin-host.md#adding-a-scope-to-the-existing-site-key),
which calls `update_api_key` to append `site-resources` to the active site
key's `scoped_permissions` and never prints a key.

Until then the Worker's lookups receive 401 and it falls back to its bundled
catalog.

Deploy order: this API first, then the scope edit, then the Worker change that
calls it.

## Corpus search and read (#315)

The site's chat researches the catalog as the Resources researcher does,
through two more routes on the same controller and scope. Both call
`smarter_dev/web/resources_corpus.py`, which the researcher's
`search_resources`, `read_source` and `read_url` tools call too, so the
ranking and the Jina cache are one implementation.

| Route | Body / result |
| ----- | ------------- |
| `POST /api/site/resources/search` | `{query, limit?}` → 200 `{hits}`: up to `limit` (1–20, default 8) hits, sources then tools, each `{kind, title, url, byline, blurb, learning_type, directory, category}`. |
| `POST /api/site/resources/read` | `{url}` → 200 `{url, body, cached}`: the source's text, at most 10,000 characters. 404 for a URL that isn't a curated `resource_sources` row (nothing is fetched); 504 when the whole read passes 12 seconds; 502 when Jina fails, in plain words, never Jina's own error text. |

- **Search** ranks titles and blurbs: an English full-text match (scored
  0.4) or pg_trgm similarity over 0.15. Article text is not searched.
- **Read** returns a stored body as stored, if fetched in the last 30 days;
  otherwise it reads the page live through Jina Reader, stores it (at most
  10,000 characters) and returns it. Simultaneous reads of one cold URL share
  one fetch. The 12-second bound covers the whole read: the lookup, the fetch
  and the write-back. A fetch others are waiting on keeps going for them; the
  last waiter to give up cancels it.
- **Logs** name the route only. The query and URL travel in the request body;
  while these routes run, httpx's request lines (which carry the Jina URL)
  are held back, and the Jina failure line omits the host. The researcher's
  logging is unchanged.
- **Rate limits** (`rate_limit.rules`, keyed by IP for the reasons above):
  `site-corpus-search` 240 a minute; `site-corpus-read` 120 a minute and
  3,000 an hour. A chat reply makes at most 12 tool calls, at most 6 of them
  reads, and a member runs at most 3 replies at once, so this is about 20
  researched replies a minute; the hourly ceiling bounds paid Jina calls. On a
  429 the Worker searches its checked-in catalog for the rest of the reply, or
  tells the model that source couldn't be read.
- **Before the scope edit** the Worker gets 401 and falls back to its
  checked-in catalog, with summaries it marks as summaries.
