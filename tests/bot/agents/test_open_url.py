"""Tests for open_url HTML sanitization and YouTube transcript support."""

from dataclasses import dataclass
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from smarter_dev.bot.agents.tools import fetch_youtube_transcript
from smarter_dev.bot.agents.tools import is_youtube_url
from smarter_dev.bot.agents.tools import sanitize_html


@dataclass
class FakeSnippet:
    text: str
    start: float = 0.0
    duration: float = 1.0


class FakeTranscript:
    def __init__(self, snippets):
        self.snippets = snippets


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


class TestFetchYoutubeTranscript:
    @patch("smarter_dev.bot.agents.tools.YouTubeTranscriptApi")
    def test_returns_transcript(self, mock_cls):
        mock_api = MagicMock()
        mock_cls.return_value = mock_api
        mock_api.fetch.return_value = FakeTranscript([
            FakeSnippet("Hello world"),
            FakeSnippet("Second line"),
        ])
        result = fetch_youtube_transcript("https://www.youtube.com/watch?v=abc123")
        assert result == "Hello world\nSecond line"
        mock_api.fetch.assert_called_once_with("abc123")

    @patch("smarter_dev.bot.agents.tools.YouTubeTranscriptApi")
    def test_returns_none_on_failure(self, mock_cls):
        mock_api = MagicMock()
        mock_cls.return_value = mock_api
        mock_api.fetch.side_effect = Exception("No transcript")
        result = fetch_youtube_transcript("https://www.youtube.com/watch?v=abc123")
        assert result is None

    def test_returns_none_for_invalid_url(self):
        result = fetch_youtube_transcript("https://www.google.com")
        assert result is None

    @patch("smarter_dev.bot.agents.tools.YouTubeTranscriptApi")
    def test_youtu_be_format(self, mock_cls):
        mock_api = MagicMock()
        mock_cls.return_value = mock_api
        mock_api.fetch.return_value = FakeTranscript([FakeSnippet("Short url")])
        result = fetch_youtube_transcript("https://youtu.be/xyz789")
        assert result == "Short url"
        mock_api.fetch.assert_called_once_with("xyz789")
