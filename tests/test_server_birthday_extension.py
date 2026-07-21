"""Tests for the server-birthday catalog extension.

Two layers, per the extension test plan:

1. Render layer — the manifest renders with its ``example_config`` and every
   render-time rail (placeholder resolution, leftover-marker sweep, lint,
   schedule floor) passes; the rendered artifacts (settings, channel scope,
   baked-in literals) are what the install service would materialise.
2. Behaviour layer — the rendered Monty script is executed through the real
   handler runtime with a stubbed emitter, covering the anniversary post, the
   per-year idempotence gate, the non-anniversary no-op, the optional GIF, and
   the ordinal computation.

The manifest is imported directly (never ``load_registry``): sibling catalog
entries are authored in parallel and a whole-catalog scan would pick up their
half-written state. The catalog-wide gate runs in the final verifier.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from smarter_dev.extensions.catalog.server_birthday import MANIFEST
from smarter_dev.extensions.rendering import RenderedHandler, render_bundle
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_runtime import run_handler_script

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / (
    "smarter_dev/extensions/catalog/server_birthday"
)
_ANNOUNCE_CHANNEL_ID = "123456789012345678"


def _scripts() -> dict[str, str]:
    return {
        handler.key: (_PACKAGE_DIR / handler.script_file).read_text()
        for handler in MANIFEST.handlers
    }


def _render(config: dict) -> RenderedHandler:
    bundle = render_bundle(MANIFEST, config, _scripts())
    assert len(bundle) == 1
    return bundle[0]


def _config(**overrides) -> dict:
    base = dict(MANIFEST.example_config)
    base.update(overrides)
    return base


# -- fakes for the runtime -----------------------------------------------------


@dataclass
class _Emitter:
    messages: list = field(default_factory=list)

    async def create_message(
        self,
        channel_id: str,
        content: str,
        ping_role_id: str | None = None,
        tolerate_missing_target: bool = False,
    ) -> str:
        self.messages.append((channel_id, content))
        return f"m{len(self.messages)}"

    async def add_reaction(self, *args) -> None:
        return None


@dataclass
class _Limiter:
    async def hit(self, key: str, limit: int, window_seconds: int | None = None) -> bool:
        return True


@dataclass
class _Actor:
    """Admin actor presence — the extension materialises admin-handler rows."""

    calls: list = field(default_factory=list)


async def _fire(rendered: RenderedHandler, *, memory: dict):
    emitter = _Emitter()
    result = await run_handler_script(
        rendered.script,
        {"trigger_type": "schedule"},
        channel_id=_ANNOUNCE_CHANNEL_ID,
        guild_id="G1",
        emitter=emitter,
        limiter=_Limiter(),
        budget=admin_budget(),
        actor=_Actor(),
        channel_ids=rendered.channel_ids,
        memory=memory,
    )
    return result, emitter


# -- render layer --------------------------------------------------------------


def test_example_config_renders_and_lints_clean():
    rendered = _render(MANIFEST.example_config)
    assert rendered.trigger_type == "schedule"
    assert rendered.settings == {"daily_time": "01:00"}
    # channel_scope materialises the announcement channel as the row's scope.
    assert rendered.channel_ids == [_ANNOUNCE_CHANNEL_ID]
    # No placeholder survived; the ids/ints/strings are baked in as literals.
    assert "{{cfg." not in rendered.script
    assert f'ANNOUNCE_CHANNEL_ID = "{_ANNOUNCE_CHANNEL_ID}"' in rendered.script
    assert "FOUNDING_MONTH = 11" in rendered.script
    assert "FOUNDING_DAY = 13" in rendered.script
    assert "FOUNDING_YEAR = 2020" in rendered.script
    assert 'CELEBRATION_GIF_URL = "https://example.com/party.gif"' in rendered.script


def test_manifest_declares_a_single_schedule_handler():
    assert MANIFEST.slug == "server-birthday"
    assert len(MANIFEST.handlers) == 1
    handler = MANIFEST.handlers[0]
    assert handler.trigger_type == "schedule"
    assert handler.settings == {"daily_time": "01:00"}
    # Guild-wide role/ban grants are not part of this handler; no allowlist.
    assert "allowed_role_ids" not in handler.settings


def test_blank_gif_default_renders_empty_literal():
    # The optional GIF field falls back to its "" default and renders to a
    # falsy literal so the script skips the GIF send.
    config = dict(MANIFEST.example_config)
    del config["celebration_gif_url"]
    rendered = _render(config)
    assert 'CELEBRATION_GIF_URL = ""' in rendered.script


# -- behaviour layer -----------------------------------------------------------


async def test_posts_on_the_anniversary_and_records_the_year():
    today = datetime.date.today()
    rendered = _render(
        _config(
            founding_month=today.month,
            founding_day=today.day,
            founding_year=today.year - 5,
        )
    )
    result, emitter = await _fire(rendered, memory={})
    assert result.outcome == "ok", result.error
    # GIF first, then the banner — two sends, well under the 5-message budget.
    assert emitter.messages == [
        (_ANNOUNCE_CHANNEL_ID, "https://example.com/party.gif"),
        (_ANNOUNCE_CHANNEL_ID, "🎂 Happy 5th birthday to the server!! 🎉"),
    ]
    # The idempotence gate is recorded.
    assert result.memory_changed is True
    assert result.memory == {"last_announced_year": today.year}


async def test_idempotent_repeat_fire_same_year_is_a_noop():
    today = datetime.date.today()
    rendered = _render(
        _config(founding_month=today.month, founding_day=today.day)
    )
    first, _ = await _fire(rendered, memory={})
    assert first.outcome == "ok", first.error

    # A second fire on the same anniversary day, seeded with the saved memory,
    # posts nothing.
    second, emitter = await _fire(rendered, memory=first.memory)
    assert second.outcome == "ok", second.error
    assert emitter.messages == []
    assert second.memory == {"last_announced_year": today.year}


async def test_no_post_when_it_is_not_the_anniversary():
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    rendered = _render(
        _config(founding_month=tomorrow.month, founding_day=tomorrow.day)
    )
    result, emitter = await _fire(rendered, memory={})
    assert result.outcome == "ok", result.error
    assert emitter.messages == []
    # No announcement, so the year gate is never written.
    assert result.memory_changed is False


async def test_blank_gif_sends_only_the_banner():
    today = datetime.date.today()
    rendered = _render(
        _config(
            founding_month=today.month,
            founding_day=today.day,
            founding_year=today.year - 1,
            celebration_gif_url="",
        )
    )
    result, emitter = await _fire(rendered, memory={})
    assert result.outcome == "ok", result.error
    assert emitter.messages == [
        (_ANNOUNCE_CHANNEL_ID, "🎂 Happy 1st birthday to the server!! 🎉"),
    ]


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (1, "1st"),
        (2, "2nd"),
        (3, "3rd"),
        (4, "4th"),
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (21, "21st"),
        (22, "22nd"),
        (111, "111th"),
    ],
)
async def test_ordinal_of_the_birthday_number(age: int, expected: str):
    today = datetime.date.today()
    rendered = _render(
        _config(
            founding_month=today.month,
            founding_day=today.day,
            founding_year=today.year - age,
            celebration_gif_url="",
        )
    )
    result, emitter = await _fire(rendered, memory={})
    assert result.outcome == "ok", result.error
    assert emitter.messages == [
        (_ANNOUNCE_CHANNEL_ID, f"🎂 Happy {expected} birthday to the server!! 🎉"),
    ]
