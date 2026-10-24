"""Print Python's pick for every catalog URL shared by more than one row.

    python scripts/parity/pick_sources.py <catalog.json>

The file is either a JSON list of catalog rows (``[{"url", "track_key", ...}]``)
or an object holding them under ``resource_sources`` (the site's bundled
``content/resources-catalog.json``). The rows go through
``smarter_dev.web.sdanswer.pick_sources``, the same pick the answer renderer
and ``/api/site/resources/sources`` use, and the output is a JSON object
``{url: chosen_track_key}`` for duplicated URLs only, sorted by URL. Use it as
a differential fixture for the site's own pick.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from smarter_dev.web.sdanswer import pick_sources


def duplicate_picks(rows: list[dict]) -> dict[str, str]:
    counts = Counter(row["url"] for row in rows)
    picked = pick_sources(SimpleNamespace(**row) for row in rows)
    return {url: picked[url].track_key for url in sorted(picked) if counts[url] > 1}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    data = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    rows = data["resource_sources"] if isinstance(data, dict) else data
    json.dump(duplicate_picks(rows), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
