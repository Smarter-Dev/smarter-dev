"""Every provider request a model run makes, for the usage ledger.

A ``UsageMeter`` wraps the model an agent runs on (``meter.wrap(model)``, or
``agent.run(..., model=meter.wrap(agent.model))``) and keeps each response's
own token counts as it comes back. That is what a run's ``result.usage`` cannot
give: it is only there when the run finishes, and only as a total. A run that
raises after the provider answered (an output validator that gives up, a
request limit, a tool error, a timeout between requests) has still been billed
for every response it got, and the meter has them. Each request's counts are
also what a long-context tier is priced on (``llm_pricing.calc_cost_by_request``).

The meter keeps counts only, never content. Where the counts go is the
caller's: the web tier writes them with ``smarter_dev.web.usage_metering``, the
bot posts them with ``smarter_dev.bot.services.usage_reporter``.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.models import infer_model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel

from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.shared.request_usage import usage_tokens


@dataclass
class UsageMeter:
    """The requests of the runs whose models it wrapped, by model."""

    requests: dict[str, list[RequestTokens]] = field(default_factory=dict)

    def wrap(self, model: Model | str, *, model_name: str | None = None) -> Model:
        """*model*, recording every response into this meter.

        The requests are kept under *model_name*, by default the wire id the
        model sends (``model.model_name``), which ``llm_pricing`` prices. A
        ``FallbackModel``'s models are each wrapped, so a fallback's requests
        are priced at its own model's rates.
        """
        # Metering never stops a run: anything that is not a model it can
        # wrap (no model set, a stand-in, a name it cannot build) goes through
        # as it is, unmetered.
        if isinstance(model, str):
            try:
                model = infer_model(model)
            except Exception:  # noqa: BLE001
                return model
        if not isinstance(model, Model):
            return model
        if isinstance(model, MeteredModel) and model.meter is self:
            return model
        if isinstance(model, FallbackModel) and model_name is None:
            # Each model it tries, so a response the fallback rejects (billed
            # all the same) is kept, at its own model's rates.
            fallback = copy.copy(model)
            fallback.__dict__.pop("_enter_lock", None)
            fallback.models = [self.wrap(inner) for inner in model.models]
            return fallback
        return MeteredModel(model, self, model_name or model.model_name)

    def add(self, model_name: str, tokens: RequestTokens) -> None:
        self.requests.setdefault(model_name, []).append(tokens)

    def stages(self) -> list[tuple[str, RequestTokens, list[RequestTokens]]]:
        """``(model, totals, requests)`` for each model that answered."""
        return [
            (
                model_name,
                RequestTokens(*(sum(column) for column in zip(*requests))),
                list(requests),
            )
            for model_name, requests in self.requests.items()
            if requests
        ]

    def stage_dicts(self) -> list[dict]:
        """``stages()`` as JSON payload dicts: the model, its totals, and each
        request's counts."""
        return [
            {
                "model_name": model_name,
                **total.as_dict(),
                "requests": [request.as_dict() for request in requests],
            }
            for model_name, total, requests in self.stages()
        ]

    def merge(self, other: UsageMeter) -> None:
        for model_name, requests in other.requests.items():
            self.requests.setdefault(model_name, []).extend(requests)

    def clear(self) -> None:
        self.requests.clear()

    def __bool__(self) -> bool:
        return any(
            any(request) for requests in self.requests.values() for request in requests
        )


def run_through(meter: UsageMeter | None, agent: Any) -> dict:
    """``agent.run`` keywords that send *agent*'s model through *meter*: none
    without a meter, or for a stand-in agent with no pydantic-ai model."""
    model = getattr(agent, "model", None)
    if meter is None or not isinstance(model, Model):
        return {}
    return {"model": meter.wrap(model)}


class MeteredModel(WrapperModel):
    """A model whose every response's usage lands in a ``UsageMeter``."""

    def __init__(self, wrapped: Model, meter: UsageMeter, model_name: str):
        super().__init__(wrapped)
        self.meter = meter
        self.metered_name = model_name

    def _record(self, response: ModelResponse | None) -> None:
        if response is None:
            return
        tokens = usage_tokens(response.usage)
        if any(tokens):
            self.meter.add(self.metered_name, tokens)

    async def request(self, messages, model_settings, model_request_parameters):
        response = await self.wrapped.request(
            messages, model_settings, model_request_parameters
        )
        self._record(response)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages,
        model_settings,
        model_request_parameters,
        run_context: Any = None,
    ) -> AsyncIterator[Any]:
        stream = None
        try:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                yield stream
        finally:
            # A stream cut off part way has still been billed for what the
            # provider reported so far.
            response = None
            if stream is not None:
                with suppress(Exception):
                    response = stream.get()
            self._record(response)
