"""Per-pipeline-run shared cache.

Lives in module state keyed by run id, so all stages within one run see the
same Jina raw reads and Scout's news summaries. Reset between runs (each
run starts cold per the plan).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import httpx

from smarter_dev.web.research_tools import RateLimiter, URLRateLimiter


@dataclass
class PipelineCache:
    """Shared per-run state for the blogging pipeline's stage agents."""

    run_id: str
    raw_reads: dict[str, str] = field(default_factory=dict)
    news_summaries: dict[str, str] = field(default_factory=dict)
    search_rate_limiter: RateLimiter = field(
        default_factory=lambda: RateLimiter(min_delay=5.0)
    )
    url_rate_limiter: URLRateLimiter = field(
        default_factory=lambda: URLRateLimiter(min_delay=5.0)
    )
    _http_client: httpx.AsyncClient | None = field(default=None, repr=False)

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            # Lazy init so a cache constructed inside a sync block (e.g.
            # admin handler) doesn't fail. The worker job constructs and
            # closes it for the lifetime of the run.
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
            )
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


# Module-level registry. The orchestrator handler (running in a single
# process) inserts and removes entries; all stage agents look up by run_id
# inside their deps_factory. Because Skrift's worker preset is `local` (in-
# process), this is safe; if we ever shard the workers, we'd switch to a
# Redis-backed pickle/serialisable cache.
_caches: dict[str, PipelineCache] = {}


def register_cache(run_id: str) -> PipelineCache:
    """Create + store a fresh cache for ``run_id``. Idempotent."""
    cache = _caches.get(run_id)
    if cache is None:
        cache = PipelineCache(run_id=run_id)
        _caches[run_id] = cache
    return cache


def get_cache(run_id: str) -> PipelineCache:
    cache = _caches.get(run_id)
    if cache is None:
        raise RuntimeError(
            f"No PipelineCache registered for run_id={run_id}. The "
            f"orchestrator must call register_cache() before stage agents run."
        )
    return cache


async def drop_cache(run_id: str) -> None:
    cache = _caches.pop(run_id, None)
    if cache is not None:
        await cache.aclose()
