"""The bot client logs under its package name however it is started."""

from __future__ import annotations

import os
import subprocess
import sys


def test_run_as_main_logs_under_the_package_logger() -> None:
    """The pod runs ``python -m smarter_dev.bot.client``. Under ``__main__`` its
    INFO lines (the shutdown summary among them) fall outside the ``smarter_dev``
    logger the bot's logging config enables, and never appear."""
    script = (
        "import logging, runpy\n"
        "logging.basicConfig(format='%(name)s|%(message)s')\n"
        "runpy.run_module('smarter_dev.bot.client', run_name='__main__')\n"
    )
    # Without a token run_bot logs an error and returns before any network use.
    env = {**os.environ, "DISCORD_BOT_TOKEN": ""}
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120
    )
    assert "smarter_dev.bot.client|Discord bot token not provided" in result.stderr, result.stderr
