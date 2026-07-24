"""Inject prices for models newer than the bundled genai-prices snapshot.

The pip snapshot (and its remote refresh) lags new releases, so models like
Gemini 3.1 Flash Lite and GPT 5.4 nano/mini or GPT 5.6 Luna aren't priceable
out of the box. We register them as a custom snapshot so calc_price() resolves.

Prices are list price per 1M tokens, sourced from each provider's published
pricing. Update the table if list prices change.
"""

from __future__ import annotations

from decimal import Decimal

from genai_prices import data_snapshot, types

# provider_id -> { api_id: (display_name, input_mtok, output_mtok, cache_read_mtok, cache_write_mtok) }
CUSTOM_PRICES: dict[str, dict[str, tuple[str, str, str, str | None, str | None]]] = {
    "google": {
        # Gemini 3.5 Flash Lite — launched 2026-07-21. $0.30 in / $2.50 out / $0.03 cached.
        # Google bills no distinct cache-write premium, so cache-write == cache-read.
        # Must precede gemini-3.5-flash: that id's "gemini-3.5-flash-" prefix
        # matcher would otherwise swallow the lite id.
        # https://ai.google.dev/gemini-api/docs/pricing
        "gemini-3.5-flash-lite": ("Gemini 3.5 Flash Lite", "0.3", "2.5", "0.03", "0.03"),
        # Gemini 3.5 Flash — launched 2026-05-19. $1.50 in / $9.00 out / $0.15 cached.
        # https://devtk.ai/en/models/gemini-3-5-flash/ , https://pricepertoken.com/pricing-page/model/google-gemini-3.5-flash
        "gemini-3.5-flash": ("Gemini 3.5 Flash", "1.5", "9", "0.15", None),
        # Gemini 3.6 Flash — launched 2026-07-21. $1.50 in / $7.50 out / $0.15 cached.
        # https://ai.google.dev/gemini-api/docs/pricing
        "gemini-3.6-flash": ("Gemini 3.6 Flash", "1.5", "7.5", "0.15", None),
        # Gemini 3.1 Flash Lite — GA 2026-05-07. $0.25 in / $1.50 out.
        # https://devtk.ai/en/models/gemini-3-1-flash-lite/
        "gemini-3.1-flash-lite": ("Gemini 3.1 Flash Lite", "0.25", "1.5", None, None),
        # gemini-3-flash-preview is already in the bundled snapshot ($0.50/$3.00).
    },
    "openai": {
        # GPT-5.4 Nano — $0.20 in / $1.25 out / $0.02 cached.
        # https://openrouter.ai/openai/gpt-5.4-nano , https://tokencost.app/blog/gpt-5-4-mini-vs-nano-pricing
        "gpt-5.4-nano": ("GPT 5.4 Nano", "0.2", "1.25", "0.02", None),
        # GPT-5.4 Mini — $0.75 in / $4.50 out / $0.075 cached.
        "gpt-5.4-mini": ("GPT 5.4 Mini", "0.75", "4.5", "0.075", None),
        # GPT-5.6 Luna — preview pricing, July 2026. $1.00 in / $6.00 out /
        # $0.10 cached-read / $1.25 cache-write.
        "gpt-5.6-luna": ("GPT 5.6 Luna", "1", "6", "0.1", "1.25"),
        # GPT-5.6 Terra — $2.50 in / $15.00 out / $0.25 cached-read / $3.125 cache-write.
        "gpt-5.6-terra": ("GPT 5.6 Terra", "2.5", "15", "0.25", "3.125"),
    },
}


def _model_info(
    api_id: str, name: str, inp: str, out: str, cache: str | None, cache_write: str | None
) -> types.ModelInfo:
    return types.ModelInfo(
        id=api_id,
        match=types.ClauseOr(
            or_=[
                types.ClauseEquals(equals=api_id),
                types.ClauseStartsWith(starts_with=f"{api_id}-"),
            ]
        ),
        name=name,
        description=None,
        context_window=None,
        price_comments=None,
        deprecated=False,
        prices=types.ModelPrice(
            input_mtok=Decimal(inp),
            output_mtok=Decimal(out),
            cache_read_mtok=Decimal(cache) if cache is not None else None,
            cache_write_mtok=Decimal(cache_write) if cache_write is not None else None,
        ),
    )


def install() -> list[str]:
    """Append custom models to the snapshot and activate it. Returns the
    list of api_ids added (skips any the snapshot already knows)."""
    snap = data_snapshot.get_snapshot()
    added: list[str] = []
    for prov_id, models in CUSTOM_PRICES.items():
        prov = next((p for p in snap.providers if p.id == prov_id), None)
        if prov is None:
            continue
        existing = {m.id for m in prov.models}
        for api_id, (name, inp, out, cache, cache_write) in models.items():
            if api_id in existing:
                continue
            prov.models.append(_model_info(api_id, name, inp, out, cache, cache_write))
            added.append(api_id)
    data_snapshot.set_custom_snapshot(snap)
    return added


if __name__ == "__main__":
    from genai_prices import Usage, calc_price

    print("added:", install())
    pd = calc_price(
        Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        model_ref="gpt-5.6-luna",
        provider_id="openai",
    )
    print(f"gpt-5.6-luna 1M/1M -> ${pd.total_price} (matched {pd.model.id})")
