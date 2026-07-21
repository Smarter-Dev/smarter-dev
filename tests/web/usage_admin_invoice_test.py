"""Tests for the invoice/channel tree builders behind the admin usage dashboard."""

from __future__ import annotations

from decimal import Decimal

from smarter_dev.web.usage_admin import build_channel_tree, build_invoice_tree
from smarter_dev.web.usage_invoice import ChannelUsageLine, InvoiceLine


def _invoice_line(**overrides) -> InvoiceLine:
    defaults = dict(
        provider_key="openai",
        provider_label="OpenAI",
        model_name="gpt-5.4",
        reasoning_level="high",
        source="chat",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal("0.010000"),
    )
    defaults.update(overrides)
    return InvoiceLine(**defaults)


def _channel_line(**overrides) -> ChannelUsageLine:
    defaults = dict(
        guild_id="g1",
        channel_id="c1",
        channel_name="general",
        provider_key="openai",
        provider_label="OpenAI",
        model_name="gpt-5.4",
        reasoning_level="high",
        source="chat",
        input_tokens=1000,
        output_tokens=200,
        cost_usd=Decimal("0.010000"),
    )
    defaults.update(overrides)
    return ChannelUsageLine(**defaults)


class TestBuildInvoiceTree:
    def test_empty_lines_produce_empty_tree_and_zero_totals(self):
        providers, totals = build_invoice_tree([])

        assert providers == []
        assert totals["cost"] == Decimal("0")
        assert totals["input_tokens"] == 0
        assert totals["output_tokens"] == 0
        assert totals["cache_read_tokens"] == 0
        assert totals["cache_write_tokens"] == 0

    def test_groups_by_provider_then_model_with_subtotals(self):
        lines = [
            _invoice_line(model_name="gpt-5.4", reasoning_level="high",
                          cost_usd=Decimal("0.30")),
            _invoice_line(model_name="gpt-5.4", reasoning_level="low",
                          input_tokens=500, output_tokens=100,
                          cost_usd=Decimal("0.10")),
            _invoice_line(provider_key="google", provider_label="Google",
                          model_name="gemini-3.1-pro", reasoning_level=None,
                          source="scan", cache_read_tokens=400,
                          cache_write_tokens=50, cost_usd=Decimal("0.20")),
        ]

        providers, totals = build_invoice_tree(lines)

        assert [p["label"] for p in providers] == ["OpenAI", "Google"]
        openai = providers[0]
        assert openai["cost"] == Decimal("0.40")
        assert openai["input_tokens"] == 1500
        assert openai["output_tokens"] == 300
        assert [m["name"] for m in openai["models"]] == ["gpt-5.4"]
        gpt = openai["models"][0]
        assert gpt["cost"] == Decimal("0.40")
        # Rows within a model sort by cost descending.
        assert [r["cost"] for r in gpt["rows"]] == [Decimal("0.30"), Decimal("0.10")]
        assert gpt["rows"][0]["reasoning"] == "High"
        assert gpt["rows"][0]["source"] == "Chat"

        google = providers[1]
        assert google["models"][0]["cache_read_tokens"] == 400
        assert google["models"][0]["cache_write_tokens"] == 50
        assert google["models"][0]["rows"][0]["reasoning"] == "—"

        assert totals["cost"] == Decimal("0.60")
        assert totals["input_tokens"] == 2500
        assert totals["cache_read_tokens"] == 400

    def test_providers_and_models_sort_by_cost_descending(self):
        lines = [
            _invoice_line(provider_key="google", provider_label="Google",
                          model_name="gemini-3.1-flash", cost_usd=Decimal("0.50")),
            _invoice_line(model_name="gpt-5.4-mini", cost_usd=Decimal("0.05")),
            _invoice_line(model_name="gpt-5.4", cost_usd=Decimal("0.25")),
        ]

        providers, _ = build_invoice_tree(lines)

        assert [p["label"] for p in providers] == ["Google", "OpenAI"]
        assert [m["name"] for m in providers[1]["models"]] == ["gpt-5.4", "gpt-5.4-mini"]

    def test_unknown_reasoning_level_passes_through_verbatim(self):
        providers, _ = build_invoice_tree(
            [_invoice_line(reasoning_level="turbo-think")]
        )

        assert providers[0]["models"][0]["rows"][0]["reasoning"] == "turbo-think"

    def test_costs_stay_decimal(self):
        providers, totals = build_invoice_tree(
            [_invoice_line(cost_usd=Decimal("0.000123"))]
        )

        assert isinstance(totals["cost"], Decimal)
        assert isinstance(providers[0]["cost"], Decimal)
        assert providers[0]["models"][0]["rows"][0]["cost"] == Decimal("0.000123")


class TestBuildChannelTree:
    def test_groups_by_channel_with_subtotals_sorted_by_cost(self):
        lines = [
            _channel_line(channel_id="c1", channel_name="general",
                          cost_usd=Decimal("0.10")),
            _channel_line(channel_id="c1", channel_name="general",
                          model_name="gemini-3.1-flash", provider_key="google",
                          provider_label="Google", reasoning_level="low",
                          input_tokens=300, output_tokens=60,
                          cost_usd=Decimal("0.30")),
            _channel_line(channel_id="c2", channel_name="bot-lab",
                          cost_usd=Decimal("0.90")),
        ]

        channels = build_channel_tree(lines)

        assert [c["channel_id"] for c in channels] == ["c2", "c1"]
        bot_lab = channels[0]
        assert bot_lab["display_name"] == "#bot-lab"
        assert bot_lab["cost"] == Decimal("0.90")
        general = channels[1]
        assert general["cost"] == Decimal("0.40")
        assert general["input_tokens"] == 1300
        assert general["output_tokens"] == 260
        # Rows within a channel sort by cost descending.
        assert [r["cost"] for r in general["rows"]] == [
            Decimal("0.30"), Decimal("0.10")
        ]
        assert general["rows"][0]["model_name"] == "gemini-3.1-flash"
        assert general["rows"][0]["reasoning"] == "Low"

    def test_display_name_falls_back_to_channel_id_then_unknown(self):
        channels = build_channel_tree([
            _channel_line(channel_id="123456", channel_name=None),
            _channel_line(guild_id=None, channel_id=None, channel_name=None,
                          cost_usd=Decimal("0.01")),
        ])

        names = {c["channel_id"]: c["display_name"] for c in channels}
        assert names["123456"] == "123456"
        assert names[None] == "unknown channel"

    def test_empty_lines_produce_empty_list(self):
        assert build_channel_tree([]) == []
