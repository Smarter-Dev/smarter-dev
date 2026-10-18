"""Token counts of single provider requests, for per-request pricing.

A long-context tier (Claude Haiku 5.5 bills 5x once one prompt passes 100,000
tokens) is decided per provider request, so a run's summed usage cannot price
it: one 150K request and three 50K requests sum to the same total. Callers that
meter a run keep each response's own counts as well, read here from pydantic-ai
``ModelResponse`` objects or from their JSON form (Skrift stores run messages as
JSON), and ``llm_pricing.calc_cost_by_request`` prices them.

No pydantic-ai import: the web tier reads Skrift's stored JSON with this too.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from typing import NamedTuple


class RequestTokens(NamedTuple):
    """One request's tokens; cache reads and writes are a subset of input."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return self._asdict()


def _field(source: Any, name: str) -> Any:
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _wire_id(model: str | None) -> str:
    return (model or "").split(":", 1)[-1]


def response_request_tokens(
    messages: Iterable[Any], model_id: str | None = None
) -> list[RequestTokens]:
    """Each model response's own token counts, in order.

    *messages* are pydantic-ai messages or their JSON dicts; requests are
    skipped. With *model_id*, only responses from that model count (a wire id
    with or without its ``provider:`` prefix; a dated variant matches), so a
    fallback model's responses are not priced at this model's rates.
    """
    wanted = _wire_id(model_id)
    found = []
    for message in messages:
        if _field(message, "kind") != "response":
            continue
        if wanted and not _wire_id(_field(message, "model_name")).startswith(wanted):
            continue
        usage = _field(message, "usage")
        if usage is None:
            continue
        found.append(
            RequestTokens(
                *(int(_field(usage, name) or 0) for name in RequestTokens._fields)
            )
        )
    return found


def request_tokens_from_dicts(rows: Iterable[dict] | None) -> list[RequestTokens]:
    """RequestTokens back from the dicts a payload carried them in."""
    return [
        RequestTokens(*(int(row.get(name) or 0) for name in RequestTokens._fields))
        for row in rows or ()
    ]
