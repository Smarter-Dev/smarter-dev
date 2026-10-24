# Site resources API

`/api/site/resources` lets the site Worker fill in chat answer cards with the
same resource metadata Python's own answer renderer uses. Given the URLs an
answer cites, it returns the catalog rows `sdanswer` reads
(`_load_sources`, exact URL match on `resource_sources`) with the fields of
`_source_to_article_card`. It is read-only and holds no member data.

Every route requires the site service principal (`principal_type="service"`,
`service_name="site"`) with an explicit **site-resources** scope, and the
owner's `site-service` role. A browser never calls these routes. Calls go
through the same per-key rate limits as the site memory routes.

| Route | Body / result |
| ----- | ------------- |
| `POST /api/site/resources/sources` | `{urls}` → 200 `{sources}`. |

`urls` is a list of 1–100 strings, each 1–500 characters; any other field,
a non-string entry or a list outside those limits is a 422 with the API's
usual validation body. Repeated URLs are looked up once.

`sources` holds one entry per requested URL found in the catalog, in the order
of the request, with `url`, `title`, `byline`, `blurb`, `learning_type` and
`track_key`. `byline` and `blurb` are `""` when empty, as on the article card.
URLs must match exactly (no normalisation); unknown URLs are left out, so the
Worker decides what to show for them. If two catalog rows share a URL, the one
with the lowest `track_key` is returned.

## Enabling it

The site guard (`site_guards.py`) checks the key's own
`scoped_permission_list`, so the `site-service` role entry alone does not
authorize the existing site key. No re-mint is needed: add `site-resources` to
the existing site key's scopes in place, which keeps its secret (and the
Worker's `SKRIFT_SITE_KEY`) unchanged. Either use the Skrift
`/admin/api-keys/{id}/edit` form, or run a one-off Job (as in
[admin-host.md](admin-host.md)) calling

```python
from skrift.db.services.api_key_service import update_api_key

await update_api_key(
    session, key, scoped_permissions=[*key.scoped_permission_list, "site-resources"]
)
```

Until then the Worker's lookups receive 401 and it falls back to its bundled
catalog.

Deploy order: this API first, then the scope edit, then the Worker change that
calls it.
