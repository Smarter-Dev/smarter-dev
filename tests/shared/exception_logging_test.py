"""Tests for logging a handled exception without its text."""

from __future__ import annotations

import logging

from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.shared.message_content import MESSAGE_CONTENT_PLACEHOLDER

logger = logging.getLogger("tests.exception_logging")


def _fail() -> None:
    try:
        raise KeyError("what someone said")
    except KeyError as cause:
        raise RuntimeError("they said: what someone said") from cause


def test_logs_types_and_frames_without_the_text(caplog):
    with caplog.at_level(logging.ERROR, logger=logger.name):
        try:
            _fail()
        except RuntimeError:
            log_exception(logger, "turn failed channel=%s", "222")

    [record] = caplog.records
    text = record.getMessage()
    assert record.levelno == logging.ERROR
    assert record.exc_info is None
    assert text.startswith("turn failed channel=222\n")
    assert "what someone said" not in text
    assert f"KeyError: {MESSAGE_CONTENT_PLACEHOLDER}" in text
    assert f"RuntimeError: {MESSAGE_CONTENT_PLACEHOLDER}" in text
    assert "in _fail" in text
    # The record points at the caller, not at the helper.
    assert record.funcName == "test_logs_types_and_frames_without_the_text"


def test_a_message_with_no_args_is_not_formatted(caplog):
    with caplog.at_level(logging.WARNING, logger=logger.name):
        try:
            raise ValueError("100% someone's words")
        except ValueError:
            log_exception(logger, "50% done", level=logging.WARNING)

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("50% done\n")
    assert "someone's words" not in record.getMessage()


def test_outside_an_except_block_logs_the_message_alone(caplog):
    with caplog.at_level(logging.ERROR, logger=logger.name):
        log_exception(logger, "nothing raised %d", 1)

    assert caplog.records[0].getMessage() == "nothing raised 1"
