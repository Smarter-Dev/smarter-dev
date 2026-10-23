"""The classifier sees the message's images, and is never clean without them.

The case is 20:30 Oct 23 2026: four posts, each with four images and short,
harmless-looking text. Haiku was shown only the text and the image URLs, so it
judged the posts clean and the provisional hold was released. Now it is shown
the images, and a "no" given while any of them could not be loaded is unknown.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic_ai.messages import BinaryContent
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.moderation_coordinator import classifier as clf
from smarter_dev.bot.moderation_coordinator.classifier import ClassifierBudget
from smarter_dev.bot.moderation_coordinator.classifier import ClassifierRequest
from smarter_dev.bot.moderation_coordinator.classifier import ImageTooLarge
from smarter_dev.bot.moderation_coordinator.classifier import ModerationClassifier
from smarter_dev.bot.moderation_coordinator.classifier import choose_backend
from smarter_dev.bot.moderation_coordinator.classifier import fetch_image
from smarter_dev.bot.moderation_coordinator.classifier import image_type
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import ANSWERED
from smarter_dev.bot.moderation_coordinator.model import BUDGET
from smarter_dev.bot.moderation_coordinator.model import EVENT_PROMOTION
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import UNSEEN_IMAGES
from smarter_dev.bot.moderation_coordinator.model import Attachment

#: Short and harmless on its own, as the 20:30 posts' text was.
TEXT = "selling my old setup, pics below. dm me if keen"
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff\xe0"
CDN = "https://cdn.discordapp.com/attachments/1/2"


def _image(name: str, content_type: str = "image/png", size: int = 50_000):
    return Attachment(name, content_type, size, f"{CDN}/{name}")


FOUR = tuple(_image(f"shot{i}.png") for i in range(4))


def _request(attachments=FOUR, labels=frozenset({SCAM, ABUSE}), **over):
    return ClassifierRequest(
        guild_id="G",
        channel_id="C",
        author_id="A",
        labels=labels,
        content=TEXT,
        attachments=attachments,
        **over,
    )


class Fetches:
    """Every attachment loads as an image whose bytes name it, unless told."""

    def __init__(self, *, fail=(), not_image=(), body=None):
        self.urls: list[str] = []
        self.fail = set(fail)
        self.not_image = set(not_image)
        self.body = body

    async def __call__(self, url: str, limit: int) -> bytes:
        self.urls.append(url)
        name = url.rsplit("/", 1)[-1]
        if name in self.fail:
            raise httpx.ConnectError("refused")
        if name in self.not_image:
            return b"<html>not an image</html>"
        return (self.body or PNG) + name.encode()


class Haiku:
    """Haiku that finds a scam only in an image whose bytes say SCAM."""

    def __init__(self, answer_no: float = 0.9):
        self.prompts: list[str] = []
        self.images: list[list[BinaryContent]] = []
        self.answer_no = answer_no

    def model(self) -> FunctionModel:
        def respond(messages, info):
            content = messages[-1].parts[-1].content
            parts = [content] if isinstance(content, str) else list(content)
            self.prompts.append(parts[0])
            images = [p for p in parts if isinstance(p, BinaryContent)]
            self.images.append(images)
            scam = any(b"SCAM" in image.data for image in images)
            body = {
                SCAM: {"answer": scam, "confidence": 0.95 if scam else self.answer_no},
                ABUSE: {"answer": False, "confidence": 0.9},
                EVENT_PROMOTION: {"answer": False, "confidence": 0.9},
            }
            return ModelResponse(parts=[TextPart(json.dumps(body))])

        return FunctionModel(respond, model_name="claude-haiku-5-5")


def _classifier(haiku: Haiku, fetches: Fetches, **over) -> ModerationClassifier:
    return ModerationClassifier(
        budget=over.pop("budget", ClassifierBudget(per_guild_day=100)),
        haiku_model=haiku.model(),
        jev_model=over.pop("jev_model", None),
        clock=lambda: 1000.0,
        fetch_image=fetches,
        **over,
    )


# -- 20:30 Oct 23 ---------------------------------------------------------------


def test_the_text_is_the_2030_length():
    assert len(TEXT) == 47


async def test_2030_shape_all_four_images_are_shown_and_a_clean_answer_stands():
    haiku, fetches = Haiku(answer_no=0.7), Fetches()
    result = await _classifier(haiku, fetches).classify(_request())

    assert result.backend == "haiku"
    assert result.requests == 1
    assert [len(images) for images in haiku.images] == [4]
    assert [image.media_type for image in haiku.images[0]] == ["image/png"] * 4
    # In the message's order, each named in the prompt.
    assert [image.data[len(PNG) :] for image in haiku.images[0]] == [
        f"shot{i}.png".encode() for i in range(4)
    ]
    for i in range(4):
        assert f"shot{i}.png" in haiku.prompts[0]
        assert f"[shown as image {i + 1}]" in haiku.prompts[0]
    # Every image seen, so the "no" (as 20:30's was, at 0.70) is clean.
    assert result.verdicts[SCAM].outcome == ANSWERED
    assert result.verdicts[SCAM].no
    assert result.as_record()["images"] == {
        "sent": 4,
        "unseen": 0,
        "over_limit": 0,
        "other_files": 0,
    }


async def test_2030_shape_a_scam_only_in_the_images_is_found():
    haiku = Haiku()
    fetches = Fetches(body=PNG + b"SCAM ")
    result = await _classifier(haiku, fetches).classify(_request())
    assert result.verdicts[SCAM].yes


async def test_2030_shape_one_image_not_loaded_makes_no_unknown():
    haiku, fetches = Haiku(), Fetches(fail={"shot2.png"})
    result = await _classifier(haiku, fetches).classify(_request())

    assert len(haiku.images[0]) == 3
    assert "shot2.png" in haiku.prompts[0]
    assert "[not shown: could not be loaded]" in haiku.prompts[0]
    for label in (SCAM, ABUSE):
        verdict = result.verdicts[label]
        assert verdict.outcome == UNSEEN_IMAGES
        assert not verdict.no and not verdict.known
    assert result.as_record()["images"]["unseen"] == 1


async def test_a_yes_stands_when_another_image_was_not_loaded():
    haiku = Haiku()
    fetches = Fetches(fail={"shot0.png"}, body=PNG + b"SCAM ")
    result = await _classifier(haiku, fetches).classify(_request())
    assert result.verdicts[SCAM].yes


async def test_every_image_failing_still_asks_on_the_text_and_is_unknown():
    haiku = Haiku()
    fetches = Fetches(fail={f"shot{i}.png" for i in range(4)})
    result = await _classifier(haiku, fetches).classify(_request())
    assert result.requests == 1
    assert haiku.images == [[]]
    assert TEXT in haiku.prompts[0]
    assert result.verdicts[SCAM].outcome == UNSEEN_IMAGES


async def test_only_scam_and_abuse_noes_become_unknown():
    haiku, fetches = Haiku(), Fetches(fail={"shot1.png"})
    labels = frozenset({SCAM, ABUSE, EVENT_PROMOTION})
    result = await _classifier(haiku, fetches).classify(_request(labels=labels))
    assert result.verdicts[EVENT_PROMOTION].no


# -- bounds ------------------------------------------------------------------------


async def test_at_most_four_images_are_shown_and_the_rest_are_listed():
    haiku, fetches = Haiku(), Fetches()
    six = tuple(_image(f"shot{i}.png") for i in range(6))
    result = await _classifier(haiku, fetches).classify(_request(six))

    assert len(haiku.images[0]) == 4
    assert len(fetches.urls) == 4
    assert "shot5.png" in haiku.prompts[0]
    assert "[not shown: over the image limit]" in haiku.prompts[0]
    assert result.as_record()["images"]["over_limit"] == 2
    assert result.verdicts[SCAM].no


async def test_files_that_are_not_images_are_listed_but_never_loaded():
    haiku, fetches = Haiku(), Fetches()
    files = (
        Attachment("notes.txt", "text/plain", 10, f"{CDN}/notes.txt"),
        _image("shot0.png"),
        Attachment("setup.zip", None, 10, f"{CDN}/setup.zip"),
    )
    result = await _classifier(haiku, fetches).classify(_request(files))

    assert fetches.urls == [f"{CDN}/shot0.png"]
    assert len(haiku.images[0]) == 1
    assert "notes.txt" in haiku.prompts[0] and "setup.zip" in haiku.prompts[0]
    assert result.as_record()["images"]["other_files"] == 2
    assert result.verdicts[SCAM].no


async def test_an_image_whose_bytes_are_not_an_image_is_unseen():
    haiku, fetches = Haiku(), Fetches(not_image={"shot3.png"})
    result = await _classifier(haiku, fetches).classify(_request())
    assert len(haiku.images[0]) == 3
    assert result.verdicts[SCAM].outcome == UNSEEN_IMAGES


async def test_an_image_reported_over_the_size_cap_is_not_loaded_and_unseen():
    haiku, fetches = Haiku(), Fetches()
    big = _image("big.png", size=clf.MAX_IMAGE_BYTES + 1)
    result = await _classifier(haiku, fetches).classify(_request((*FOUR[:3], big)))
    assert f"{CDN}/big.png" not in fetches.urls
    assert len(haiku.images[0]) == 3
    assert result.verdicts[SCAM].outcome == UNSEEN_IMAGES


async def test_a_spent_budget_loads_no_image():
    haiku, fetches = Haiku(), Fetches()
    result = await _classifier(
        haiku, fetches, budget=ClassifierBudget(per_guild_day=0)
    ).classify(_request())
    assert result.outcome == BUDGET
    assert fetches.urls == [] and haiku.prompts == []


async def test_a_slow_image_is_unseen_and_the_request_still_goes(monkeypatch):
    monkeypatch.setattr(clf, "IMAGE_FETCH_SECONDS", 0.05)
    haiku, fetches = Haiku(), Fetches()

    async def slow_one(url, limit):
        if url.endswith("shot1.png"):
            await asyncio.sleep(5)
        return await fetches(url, limit)

    result = await _classifier(haiku, slow_one).classify(_request())
    assert len(haiku.images[0]) == 3
    assert result.verdicts[SCAM].outcome == UNSEEN_IMAGES


def test_image_type_is_read_from_the_bytes():
    assert image_type(PNG + b"x") == "image/png"
    assert image_type(JPEG + b"x") == "image/jpeg"
    assert image_type(b"GIF89a....") == "image/gif"
    assert image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert image_type(b"<svg xmlns=...>") is None
    assert image_type(b"") is None


# -- backend -------------------------------------------------------------------


def test_an_image_always_means_haiku():
    ring = frozenset({EVENT_PROMOTION})
    assert choose_backend(ring) == "jev"
    assert choose_backend(ring, images=True) == "haiku"
    assert choose_backend(frozenset(), images=True) is None


async def test_ring_questions_on_a_message_with_an_image_go_to_haiku():
    haiku, fetches = Haiku(), Fetches()

    def never(messages, info):
        raise AssertionError("Jev cannot see images")

    result = await _classifier(haiku, fetches, jev_model=FunctionModel(never)).classify(
        _request(FOUR[:1], labels=frozenset({EVENT_PROMOTION}))
    )
    assert result.backend == "haiku"
    assert len(haiku.images[0]) == 1
    assert result.verdicts[EVENT_PROMOTION].no


async def test_ring_questions_on_a_message_with_only_files_stay_on_jev():
    haiku, fetches = Haiku(), Fetches()
    calls = []

    def jev(messages, info):
        calls.append(1)
        tool = info.output_tools[0]
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool.name, args={"promotes_event": False})],
            provider_details={"confidence": {"promotes_event": 0.9}},
        )

    files = (Attachment("notes.txt", "text/plain", 10, f"{CDN}/notes.txt"),)
    result = await _classifier(haiku, fetches, jev_model=FunctionModel(jev)).classify(
        _request(files, labels=frozenset({EVENT_PROMOTION}))
    )
    assert result.backend == "jev"
    assert calls == [1] and haiku.prompts == [] and fetches.urls == []


# -- the real fetch ---------------------------------------------------------------


@pytest.fixture
def served(monkeypatch):
    """Route fetch_image's client to a handler; record what was requested."""
    seen: list[str] = []
    replies = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return replies.get(str(request.url), httpx.Response(404))

    real = httpx.AsyncClient

    def client(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(clf.httpx, "AsyncClient", client)
    return seen, replies


async def test_fetch_loads_a_discord_attachment(served):
    seen, replies = served
    replies[f"{CDN}/a.png"] = httpx.Response(200, content=PNG + b"x")
    assert await fetch_image(f"{CDN}/a.png", 1000) == PNG + b"x"
    assert seen == [f"{CDN}/a.png"]


async def test_fetch_stops_past_the_cap(served):
    seen, replies = served
    replies[f"{CDN}/a.png"] = httpx.Response(200, content=PNG + bytes(2000))
    with pytest.raises(ImageTooLarge):
        await fetch_image(f"{CDN}/a.png", 1000)


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/attachments/1/2/a.png",
        "https://example.com/a.png",
        "https://cdn.discordapp.com.evil.example/a.png",
        "https://169.254.169.254/latest/meta-data",
        "",
    ],
)
async def test_fetch_refuses_anything_but_discord_over_https(served, url):
    seen, _ = served
    with pytest.raises(ValueError):
        await fetch_image(url, 1000)
    assert seen == []


async def test_fetch_never_follows_a_redirect(served):
    seen, replies = served
    replies[f"{CDN}/a.png"] = httpx.Response(
        302, headers={"location": "https://example.com/x.png"}
    )
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_image(f"{CDN}/a.png", 1000)
    assert seen == [f"{CDN}/a.png"]
