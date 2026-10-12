# Resources content export
These JSON files contain the seven resources topic pages and the catalog.
Each file has a matching JSON Schema in `schema/`.
Run `uv run python scripts/export_site_content.py` to regenerate them.
The site copies this directory into `smarter-dev-site/content/`.

## Runtime, not exported

Live `click_count` values, `popularity_unlocked`, account quotas, and conversations
come from the site's runtime data. The agentic `featured` list and `featured_mode`
contain the deterministic latest-first fallback used when click counts are empty;
the site may reorder featured items when live click counts unlock popularity mode.
