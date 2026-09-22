from __future__ import annotations

import os
from types import SimpleNamespace

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.typesafe import TypeSafeModel

from smarter_dev.bot.proactive.models import build_twopass_model
from smarter_dev.bot.proactive.models import build_watcher_runner
from smarter_dev.bot.proactive.models import ensure_typesafe_key_alias
from smarter_dev.bot.proactive.models import typesafe_key_present
from smarter_dev.bot.proactive.watcher import JevWatcherRunner
from smarter_dev.bot.proactive.watcher import WatcherRunner
from smarter_dev.bot.proactive.watcher import usage_dict


def test_litellm_proxy_uses_the_available_model_aliases(monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_ENDPOINT", "https://proxy.example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "secret")

    expected = {
        "z-ai/glm-5.3-flash": "glm-5.3-flash",
        "gemini-3.7-flash": "gemini/gemini-3.7-flash",
        "gemini-3.8-flash": "gemini/gemini-3.8-flash",
    }
    for requested, proxied in expected.items():
        model = build_twopass_model(requested)
        assert isinstance(model, OpenAIChatModel)
        assert model.model_name == proxied
        assert str(model.base_url) == "https://proxy.example.test/v1/"


def test_typesafe_route_bypasses_litellm(monkeypatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("LITELLM_ENDPOINT", "https://proxy.example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "secret")

    model = build_twopass_model("typesafe:jev-latest")

    assert isinstance(model, TypeSafeModel)
    assert model.model_name == "jev-latest"


def test_typesafe_route_accepts_legacy_jev_key_alias(monkeypatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("JEV_API_KEY", "legacy-test-key")

    ensure_typesafe_key_alias()
    model = build_twopass_model("typesafe:jev-latest")

    assert typesafe_key_present()
    assert isinstance(model, TypeSafeModel)
    assert model.model_name == "jev-latest"
    assert os.environ["TYPESAFE_API_KEY"] == "legacy-test-key"


def test_watcher_factory_selects_jev_without_changing_glm(monkeypatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("LITELLM_ENDPOINT", "https://proxy.example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "secret")

    jev = build_watcher_runner(
        "typesafe:jev-latest", fallback_model_id="z-ai/glm-5.3-flash"
    )
    glm = build_watcher_runner("z-ai/glm-5.3-flash")

    assert isinstance(jev, JevWatcherRunner)
    assert jev.fallback_model_id == "z-ai/glm-5.3-flash"
    assert isinstance(glm, WatcherRunner)


def test_usage_dict_accepts_method_and_property_result_apis() -> None:
    usage = SimpleNamespace(
        input_tokens=3,
        output_tokens=2,
        cache_read_tokens=1,
    )

    assert usage_dict(usage) == usage_dict(lambda: usage) == {
        "input_tokens": 3,
        "output_tokens": 2,
        "cache_read_tokens": 1,
    }
