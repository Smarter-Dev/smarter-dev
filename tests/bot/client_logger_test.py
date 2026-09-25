"""The bot client logs under its package name however it is started."""

from __future__ import annotations

import os
import subprocess
import sys

# Runs the module as the pod does (``python -m``), then applies the bot's own
# logging config (create_bot) and logs through the module's logger. hikari
# installs its handler only while the root logger has none, so add none here.
SCRIPT = """
import logging, runpy, types
module = runpy.run_module('smarter_dev.bot.client', run_name='__main__')
module['create_bot'](types.SimpleNamespace(discord_bot_token='x.y.z'))
module['logger'].info('info probe')
module['logger'].debug('debug probe')
logging.getLogger('smarter_dev.bot.leadership').debug('leadership debug probe')
"""


def run_as_main() -> str:
    # Without a token run_bot logs an error and returns before any network use.
    env = {**os.environ, "DISCORD_BOT_TOKEN": ""}
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, env=env, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


def test_run_as_main_logs_info_under_the_package_logger() -> None:
    """Under ``__main__`` its INFO lines (the shutdown summary among them) fall
    outside the ``smarter_dev`` logger the bot's logging config enables, and
    never appear."""
    output = run_as_main()
    assert " smarter_dev.bot.client: info probe" in output, output


def test_client_debug_stays_off_while_the_rest_of_the_bot_logs_debug() -> None:
    """The client's debug lines fire on nearly every guild message."""
    output = run_as_main()
    assert " smarter_dev.bot.leadership: leadership debug probe" in output, output  # applied
    assert " smarter_dev.bot.client: debug probe" not in output, output
