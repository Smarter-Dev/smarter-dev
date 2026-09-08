from __future__ import annotations

from types import SimpleNamespace

from pydantic_ai.models.openai import OpenAIChatModel

from smarter_dev.bot.proactive.models import build_twopass_model
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
