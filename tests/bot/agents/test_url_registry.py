"""Tests for the escaped-URL registry used by web_read."""

from __future__ import annotations

import importlib


def _fresh_module():
    # Each test gets a clean registry (module-level OrderedDict).
    import smarter_dev.bot.agents.url_registry as mod

    return importlib.reload(mod)


def test_register_then_resolve_returns_original():
    mod = _fresh_module()
    original = "https://cdn/x.ogg?ex=1&is=2&hm=abc"
    escaped = "https://cdn/x.ogg?ex=1&amp;is=2&amp;hm=abc"
    mod.register_escaped_url(original)
    assert mod.resolve_escaped_url(escaped) == original


def test_unregistered_url_passes_through():
    mod = _fresh_module()
    # Never registered — even with a literal &amp;, it must be returned as-is.
    weird = "https://example.com/p?a=1&amp;b=2"
    assert mod.resolve_escaped_url(weird) == weird


def test_url_without_special_chars_is_not_tracked():
    mod = _fresh_module()
    clean = "https://example.com/page"
    mod.register_escaped_url(clean)
    # Nothing to resolve; returns unchanged and nothing was stored.
    assert mod.resolve_escaped_url(clean) == clean
    assert len(mod._escaped_to_original) == 0


def test_registry_is_bounded():
    mod = _fresh_module()
    mod._MAX_TRACKED = 3
    originals = [f"https://x/{i}?a=1&b={i}" for i in range(5)]
    for o in originals:
        mod.register_escaped_url(o)
    assert len(mod._escaped_to_original) == 3
    # Oldest two evicted; their escaped forms no longer resolve.
    assert mod.resolve_escaped_url("https://x/0?a=1&amp;b=0") == "https://x/0?a=1&amp;b=0"
    assert mod.resolve_escaped_url("https://x/4?a=1&amp;b=4") == "https://x/4?a=1&b=4"
