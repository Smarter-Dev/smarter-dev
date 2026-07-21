"""Tests for the member-count-display extension.

Two layers, both touching ONLY this extension's own catalog directory (siblings
are authored in parallel, so nothing here scans the whole catalog):

1. Render/lint: the shipped manifest + script render cleanly from example_config
   and from a custom config, producing the expected schedule settings, channel
   scope, and typed literals — the same rails ``render_bundle`` runs at install.
2. Behaviour: the *rendered* Monty script is executed through the real handler
   runtime with a stubbed admin emitter/actor, proving the change-gated rename
   loop, the legacy ``1.2k`` formatting, and the runtime ``{count}`` placebo
   substitution actually work end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from smarter_dev.extensions.catalog.member_count_display import MANIFEST
from smarter_dev.extensions.rendering import RenderError, render_bundle
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_runtime import run_handler_script

_CATALOG_DIR = Path(__file__).resolve().parents[2] / (
    "smarter_dev/extensions/catalog/member_count_display"
)
_STATS_CHANNEL = "123456789012345678"
_BAR_CHART = "\U0001F4CA"  # 📊 — the legacy prefix, load-bearing for the gate


def _scripts() -> dict[str, str]:
    return {
        handler.key: (_CATALOG_DIR / handler.script_file).read_text()
        for handler in MANIFEST.handlers
    }


def _rendered_script(config: dict) -> str:
    bundle = render_bundle(MANIFEST, config, _scripts())
    assert len(bundle) == 1
    return bundle[0].script


# -- render / lint -------------------------------------------------------------


def test_manifest_shape():
    assert MANIFEST.slug == "member-count-display"
    assert len(MANIFEST.handlers) == 1
    handler = MANIFEST.handlers[0]
    assert handler.trigger_type == "schedule"
    # Interval is a fixed literal >= the 5-minute rename rail, not a config field.
    assert handler.settings == {"interval_seconds": 600}
    assert handler.channel_scope == ["stats_channel_id"]
    # No role grants, so no allowlist is declared.
    assert "allowed_role_ids" not in handler.settings


def test_example_config_renders_and_lints_clean():
    bundle = render_bundle(MANIFEST, MANIFEST.example_config, _scripts())
    item = bundle[0]
    # channel_scope -> channel_ids, snowflake baked in as a quoted literal.
    assert item.channel_ids == [_STATS_CHANNEL]
    assert f'STATS_CHANNEL_ID = "{_STATS_CHANNEL}"' in item.script
    assert item.settings == {"interval_seconds": 600}
    # The install marker syntax is fully gone after rendering.
    assert "{{cfg." not in item.script
    # The RUNTIME placeholder survives verbatim inside the format literal.
    assert "{count}" in item.script


def test_name_format_defaults_when_omitted():
    # name_format is optional; omitting it falls back to the legacy template.
    script = _rendered_script({"stats_channel_id": _STATS_CHANNEL})
    # The default is a pure-ASCII literal (no astral emoji ever passes through the
    # config -> json.dumps path); the emoji is a raw script-side literal instead.
    assert 'NAME_FORMAT = "{icon}Members: {count}"' in script
    assert 'COUNTER_ICON = "\U0001F4CA"' in script


def test_custom_name_format_is_substituted_as_literal_not_install_marker():
    script = _rendered_script(
        {"stats_channel_id": _STATS_CHANNEL, "name_format": "Users [{count}]"}
    )
    assert 'NAME_FORMAT = "Users [{count}]"' in script


def test_bad_snowflake_rejected():
    with pytest.raises(RenderError):
        render_bundle(
            MANIFEST, {"stats_channel_id": "not-a-snowflake"}, _scripts()
        )


# -- behaviour (rendered script through the runtime) ---------------------------


@dataclass
class _FakeEmitter:
    member_count: int = 1500
    renames: list = field(default_factory=list)

    async def rename_channel(self, channel_id: str, name: str) -> bool:
        self.renames.append((channel_id, name))
        return True

    async def get_guild_member_count(self) -> int:
        return self.member_count

    async def get_channel_guild_id(self, channel_id: str):
        return "G1"

    async def create_message(self, *a, **k):
        return "m"


@dataclass
class _StubLimiter:
    calls: list = field(default_factory=list)

    async def hit(self, key: str, limit: int, window_seconds: int | None = None) -> bool:
        self.calls.append((key, limit, window_seconds))
        return True


@dataclass
class _FakeActor:
    """Admin-tier presence: enabling the actor injects rename_channel."""

    async def rename_channel(self, *a, **k):
        return True


async def _fire(config: dict, *, member_count: int, memory: dict | None = None):
    script = _rendered_script(config)
    emitter = _FakeEmitter(member_count=member_count)
    result = await run_handler_script(
        script,
        {"trigger_type": "schedule"},
        channel_id=config["stats_channel_id"],
        guild_id="G1",
        emitter=emitter,
        limiter=_StubLimiter(),
        budget=admin_budget(),
        actor=_FakeActor(),
        channel_ids=[config["stats_channel_id"]],
        memory=dict(memory or {}),
    )
    return result, emitter


async def test_first_fire_renames_with_formatted_count_and_stores_gate():
    result, emitter = await _fire(
        {"stats_channel_id": _STATS_CHANNEL}, member_count=1500
    )
    assert result.outcome == "ok", result.error
    expected = f"{_BAR_CHART}Members: 1.5k"
    assert emitter.renames == [(_STATS_CHANNEL, expected)]
    # The change-gate key is stored so a later unchanged fire is a no-op.
    assert result.memory == {"last_counter": expected}
    # A rename spends one moderation action.
    assert result.usage["mod_actions"] == 1


async def test_sub_thousand_count_renders_plain_number():
    result, emitter = await _fire(
        {"stats_channel_id": _STATS_CHANNEL}, member_count=950
    )
    assert result.outcome == "ok", result.error
    assert emitter.renames == [(_STATS_CHANNEL, f"{_BAR_CHART}Members: 950")]


async def test_thousands_round_to_one_decimal_k():
    result, emitter = await _fire(
        {"stats_channel_id": _STATS_CHANNEL}, member_count=1234
    )
    assert result.outcome == "ok", result.error
    assert emitter.renames == [(_STATS_CHANNEL, f"{_BAR_CHART}Members: 1.2k")]


async def test_change_gate_suppresses_redundant_rename():
    # Memory already holds the exact name the current count renders to: no rename.
    seeded = {"last_counter": f"{_BAR_CHART}Members: 1.5k"}
    result, emitter = await _fire(
        {"stats_channel_id": _STATS_CHANNEL},
        member_count=1500,
        memory=seeded,
    )
    assert result.outcome == "ok", result.error
    assert emitter.renames == []
    assert result.usage["mod_actions"] == 0


async def test_change_gate_renames_when_count_crosses_bucket():
    # Stored name is the old bucket; the count moved enough to change the display.
    seeded = {"last_counter": f"{_BAR_CHART}Members: 1.5k"}
    result, emitter = await _fire(
        {"stats_channel_id": _STATS_CHANNEL},
        member_count=1600,
        memory=seeded,
    )
    assert result.outcome == "ok", result.error
    assert emitter.renames == [(_STATS_CHANNEL, f"{_BAR_CHART}Members: 1.6k")]


async def test_custom_format_replaces_only_the_count_token():
    # A format that omits {icon} yields no emoji at all — proving {icon} is opt-in.
    result, emitter = await _fire(
        {"stats_channel_id": _STATS_CHANNEL, "name_format": "Users [{count}]"},
        member_count=1500,
    )
    assert result.outcome == "ok", result.error
    assert emitter.renames == [(_STATS_CHANNEL, "Users [1.5k]")]


async def test_icon_token_expands_to_legacy_glyph_and_is_repositionable():
    result, emitter = await _fire(
        {"stats_channel_id": _STATS_CHANNEL, "name_format": "{count} members {icon}"},
        member_count=1500,
    )
    assert result.outcome == "ok", result.error
    assert emitter.renames == [(_STATS_CHANNEL, f"1.5k members {_BAR_CHART}")]
