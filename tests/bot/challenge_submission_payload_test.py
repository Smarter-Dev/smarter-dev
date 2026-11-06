"""The bot's submit payload carries every field the submit route requires.

The challenge route rejected real submissions with 400 on the Halvard launch
day because ``username`` had been dropped from the bot side (c681f542).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

from smarter_dev.bot.views.challenge_views import SolutionSubmissionModalHandler
from smarter_dev.web.api_native.challenges import SolutionSubmissionRequest
from smarter_dev.web.api_native.quests import DailyQuestSubmitBody


def _submit(endpoint: str | None = None) -> dict:
    component = SimpleNamespace(custom_id="solution", value=" K7Q2 ")
    interaction = Mock()
    interaction.components = [SimpleNamespace(components=[component])]
    interaction.create_initial_response = AsyncMock()

    response = Mock(status_code=200)
    response.json.return_value = {"is_correct": False}
    api_client = Mock()
    api_client.post = AsyncMock(return_value=response)

    handler = SolutionSubmissionModalHandler(
        challenge_id="c1",
        challenge_title="Power On",
        guild_id="123",
        user=SimpleNamespace(id=1001, display_name="Katren", username="katren_"),
        api_client=api_client,
        endpoint=endpoint,
    )
    return handler, interaction, api_client


async def test_challenge_payload_validates_against_the_route_schema():
    handler, interaction, api_client = _submit()
    await handler.handle_submit(interaction)

    endpoint, kwargs = api_client.post.await_args.args[0], api_client.post.await_args.kwargs
    assert endpoint == "/challenges/c1/submit-solution"
    body = SolutionSubmissionRequest.model_validate(kwargs["json_data"])
    assert body.username == "Katren"
    assert body.user_id == "1001"
    assert body.submitted_solution == "K7Q2"


async def test_quest_payload_still_validates():
    handler, interaction, api_client = _submit(endpoint="/quests/q1/submit")
    await handler.handle_submit(interaction)
    DailyQuestSubmitBody.model_validate(api_client.post.await_args.kwargs["json_data"])
