"""Tests for open_url HTML sanitization and YouTube metadata support."""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from smarter_dev.bot.agents.tools import fetch_youtube_metadata
from smarter_dev.bot.agents.tools import is_youtube_url
from smarter_dev.bot.agents.tools import sanitize_html


class TestSanitizeHtml:
    def test_strips_scripts_and_styles(self):
        html = """<html><head><title>Test</title></head><body>
        <script>alert('xss')</script>
        <style>.foo{color:red}</style>
        <nav>Nav stuff</nav>
        <footer>Footer stuff</footer>
        <p>Real content</p>
        </body></html>"""
        result = sanitize_html(html)
        assert "alert" not in result
        assert "color:red" not in result
        assert "Nav stuff" not in result
        assert "Footer stuff" not in result
        assert "Real content" in result

    def test_preserves_content(self):
        html = """<div><h1>Title</h1><p>Paragraph text</p><ul><li>Item</li></ul></div>"""
        result = sanitize_html(html)
        assert "Title" in result
        assert "Paragraph text" in result
        assert "Item" in result

    def test_removes_non_content_classes(self):
        html = """<div>
        <div class="sidebar">Sidebar</div>
        <div id="cookie-banner">Cookie</div>
        <div class="ad-container">Ad</div>
        <div class="main-content">Real content</div>
        </div>"""
        result = sanitize_html(html)
        assert "Sidebar" not in result
        assert "Cookie" not in result
        assert "Ad" not in result
        assert "Real content" in result

    def test_removes_iframes_and_noscript(self):
        html = """<div><iframe src="x"></iframe><noscript>no js</noscript><p>Content</p></div>"""
        result = sanitize_html(html)
        assert "iframe" not in result
        assert "no js" not in result
        assert "Content" in result


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
        mock_oembed_resp = MagicMock()
        mock_oembed_resp.status_code = 200
        mock_oembed_resp.json.return_value = {"title": "Test Video", "author_name": "Test Channel"}

        mock_page_resp = MagicMock()
        mock_page_resp.status_code = 200
        mock_page_resp.text = '<html><head><meta name="description" content="A test description"></head></html>'

        async def mock_get(url, **kwargs):
            if "oembed" in url:
                return mock_oembed_resp
            return mock_page_resp

        mock_client = MagicMock()
        mock_client.get = mock_get

        async def aenter(_):
            return mock_client

        async def aexit(_, *args):
            pass

        with patch("smarter_dev.bot.agents.tools.httpx.AsyncClient") as mock_client_cls:
            ctx = MagicMock()
            ctx.__aenter__ = aenter
            ctx.__aexit__ = aexit
            mock_client_cls.return_value = ctx

            metadata = await fetch_youtube_metadata("https://www.youtube.com/watch?v=abc")
            assert metadata["title"] == "Test Video"
            assert metadata["author"] == "Test Channel"
            assert metadata["description"] == "A test description"

    @pytest.mark.asyncio
    async def test_returns_empty_on_failure(self):
        async def mock_get(url, **kwargs):
            raise Exception("Network error")

        mock_client = MagicMock()
        mock_client.get = mock_get

        async def aenter(_):
            return mock_client

        async def aexit(_, *args):
            pass

        with patch("smarter_dev.bot.agents.tools.httpx.AsyncClient") as mock_client_cls:
            ctx = MagicMock()
            ctx.__aenter__ = aenter
            ctx.__aexit__ = aexit
            mock_client_cls.return_value = ctx

            metadata = await fetch_youtube_metadata("https://www.youtube.com/watch?v=abc")
            assert metadata == {}
