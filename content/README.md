# Resources content export
These JSON files contain the seven resources topic pages and the catalog.
Each file has a matching JSON Schema in `schema/`.
Run `uv run python scripts/export_site_content.py` to regenerate them.
The site copies this directory into `smarter-dev-site/content/`.
