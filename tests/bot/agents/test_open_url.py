"""Tests for open_url Jina Reader integration and YouTube metadata support."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from smarter_dev.bot.agents.tools import _HTML_TAG_RE
from smarter_dev.bot.agents.tools import _SCRIPT_STYLE_RE
from smarter_dev.bot.agents.tools import fetch_via_jina
from smarter_dev.bot.agents.tools import fetch_youtube_metadata
from smarter_dev.bot.agents.tools import is_youtube_url


class TestFallbackHtmlStripping:
    """Test the regex-based fallback when Jina is unavailable."""

    def test_strips_scripts(self):
        html = "<div><script>alert('xss')</script><p>Content</p></div>"
        cleaned = _SCRIPT_STYLE_RE.sub("", html)
        cleaned = _HTML_TAG_RE.sub("", cleaned)
        assert "alert" not in cleaned
        assert "Content" in cleaned

    def test_strips_styles(self):
        html = "<div><style>.foo{color:red}</style><p>Content</p></div>"
        cleaned = _SCRIPT_STYLE_RE.sub("", html)
        cleaned = _HTML_TAG_RE.sub("", cleaned)
        assert "color:red" not in cleaned
        assert "Content" in cleaned

    def test_preserves_text_content(self):
        html = "<div><h1>Title</h1><p>Paragraph</p></div>"
        cleaned = _HTML_TAG_RE.sub("", html)
        assert "Title" in cleaned
        assert "Paragraph" in cleaned


class TestFetchViaJina:
    @pytest.mark.asyncio
    async def test_returns_content_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "title": "Test Page",
                "description": "A test page",
                "content": "# Test\nHello world",
                "url": "https://example.com",
            }
        }

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with patch("smarter_dev.bot.agents.tools.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await fetch_via_jina("https://example.com")

        assert result is not None
        assert result["title"] == "Test Page"
        assert result["content"] == "# Test\nHello world"

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with patch("smarter_dev.bot.agents.tools.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await fetch_via_jina("https://example.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Network error")

        with patch("smarter_dev.bot.agents.tools.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await fetch_via_jina("https://example.com")

        assert result is None


class TestIsYoutubeUrl:
    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    ])
    def test_youtube_urls(self, url):
        assert is_youtube_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://www.google.com",
        "https://example.com/watch?v=abc",
        "https://notyoutube.com/video",
        "https://vimeo.com/12345",
    ])
    def test_non_youtube_urls(self, url):
        assert is_youtube_url(url) is False


class TestFetchYoutubeMetadata:
    @pytest.mark.asyncio
    async def test_returns_metadata(self):
        jina_result = {
            "title": "Test Video",
            "description": "A test description",
            "content": "Video transcript...",
            "url": "https://www.youtube.com/watch?v=abc",
        }

        mock_oembed_resp = MagicMock()
        mock_oembed_resp.status_code = 200
        mock_oembed_resp.json.return_value = {"title": "Test Video", "author_name": "Test Channel"}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_oembed_resp

        with (
            patch("smarter_dev.bot.agents.tools.fetch_via_jina", return_value=jina_result) as mock_jina,
            patch("smarter_dev.bot.agents.tools.httpx.AsyncClient") as mock_cls,
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            metadata = await fetch_youtube_metadata("https://www.youtube.com/watch?v=abc")

        assert metadata["title"] == "Test Video"
        assert metadata["author"] == "Test Channel"
        assert metadata["description"] == "A test description"

    @pytest.mark.asyncio
    async def test_returns_empty_on_failure(self):
        with (
            patch("smarter_dev.bot.agents.tools.fetch_via_jina", return_value=None),
            patch("smarter_dev.bot.agents.tools.httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = Exception("Network error")
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            metadata = await fetch_youtube_metadata("https://www.youtube.com/watch?v=abc")
            assert metadata == {}
