# Site search API

`/api/site/search` lets the site Worker run a member's web search from site
chat. It is a facade over the dashboard's `WebSearchRun`: the same Luna query
plan, Brave searches, Jev ranking and optional grounded answer on the agent
worker, the same limits (6 starts a minute, 100 a day) and the same deletion
rules. Nothing about the pipeline is ported to the Worker.

Every route requires the site service principal (`principal_type="service"`,
`service_name="site"`) with an explicit **site-search** scope, and the owner's
`site-service` role. The member is the synced site user in the path; it must
exist and be active in Python. A browser never calls these routes.

| Route | Body / result |
| ----- | ------------- |
| `POST /api/site/search/users/{user_id}/runs` | `{request, key}` → 201 `{search}`. `key` (8–64 letters, digits, `-`) is one per chat turn: a retry returns the same run with `idempotent: true`, before limits are checked. `request` is collapsed to one line and must be 3–500 characters. 429 carries the dashboard's limit message. |
| `GET /api/site/search/users/{user_id}/runs/{run_id}` | `{search}` for the owner's run, 404 otherwise. |
| `DELETE /api/site/search/users/{user_id}/runs/{run_id}` | Deletes a finished run and its results; 409 while it runs. |
| `GET /api/site/search/users/{user_id}/keys/{key}` | `{search}` for the run a start with `key` made, or `{search: null}` when none did; never a 404 for a key without a run. For a start whose reply was lost: the site rejoins the run without starting one. |
| `DELETE /api/site/search/users/{user_id}/keys/{key}` | Forgets a start's key: marks it forgotten, so a start with it refuses (410), then deletes its run if there is one; 409 while that run runs (the mark stays; ask again). 200 `{status: "forgotten"}` is done, also for an account already gone. The forget and `POST …/runs` both lock the member's user row, so a start still on its way either made its run first (the forget deletes it) or finds the key forgotten. The marks go with the account. |

`search` is structured data: `id`, `request`, `status` (`queued`, `planning`,
`searching`, `ranking`, `answering`, `complete`, `error`), `active`,
`queries[]` (`query`, `angle`, `status`, `count`, `domains`), `results[]`
(`title`, `url`, `domain`, `snippet`, `favicon`, `age`, `relevant`, `best`,
`score`, `quality` (Jev's trusted-source verdict) and `queries`, the indexes
into `queries[]` of the searches that found it), `ranked`, `needs_answer`, `answer` (`status`, `markdown`, `reads`,
`error`) and `version`. The answer is Markdown with `[n]` citations into
`results`; it is never HTML, so the site renders it with its own safe
renderer. The pipeline now stores the streaming Markdown alongside the HTML it
already kept, so the answer can be read while it is written.

## Enabling it

Deploy this API, then give the site service key the **site-search** scope:
the `site-service` role grants it once roles sync, but the key's own scopes
must include it too, so mint a replacement key with the runbook in
`admin-host.md` (its required set now includes `site-search`) and update the
Worker's `SKRIFT_SITE_KEY`. Until then the Worker's search calls receive 401
and chat answers without the web.
