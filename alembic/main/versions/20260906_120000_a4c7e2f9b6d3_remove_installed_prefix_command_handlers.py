"""delete the installed handlers of the retired prefix-command extensions

Prefix commands — bot behaviour triggered by a member's message text — are
prohibited under Discord's message-content-intent policy, so the catalog stopped
shipping the ``sus`` extension and disboard-bumping's ``bump-commands`` handler.
Removing them from the repo stops new installs; it does nothing about the copies
already materialised into guilds, which keep firing on ``!sus`` / ``!bumpers``
until an admin happens to notice the "Update available" badge or the orphaned
install panel. This revision removes them.

What goes:

- every ``admin_handlers`` row rendered from disboard-bumping's ``bump-commands``
  template, matched by ``extension_handler_key`` within that extension's own
  installs. The install itself and its ``bump-tracker`` row stay: the tracker
  fires on the Disboard bot's confirmation embed, never on human text. The
  install is deliberately left at its old ``installed_version`` so it still
  offers "update available", which is what re-renders it against the current
  manifest and drops the retired ``commands_channel_id`` from its config.
- every ``sus`` install and the handler rows it owns. The whole extension was
  the ``!sus`` / ``!list_sus`` commands, so nothing remains once they go.

Both handlers are ``message`` triggers, so neither row carries a
``scheduled_job_id`` recurrence to cancel. ``sus`` armed one-shot timers via
``schedule_timer``, and a queued re-fire whose handler row is gone is answered
with ``{"status": "missing"}`` by the fire job, so a plain DELETE strands
nothing. ``handler_runs`` history is left in place: it is the audit record of
fires that really happened, and the retention sweep owns its lifetime.

Revision ID: a4c7e2f9b6d3
Revises: e2a6b9c4d7f1
Create Date: 2026-09-06 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4c7e2f9b6d3"
down_revision: str | None = "e2a6b9c4d7f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DISBOARD_SLUG = "disboard-bumping"
_RETIRED_DISBOARD_HANDLER_KEY = "bump-commands"
_RETIRED_SLUG = "sus"


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM admin_handlers"
            " WHERE extension_handler_key = :handler_key"
            " AND extension_install_id IN ("
            "  SELECT id FROM extension_installs WHERE extension_slug = :slug"
            " )"
        ).bindparams(handler_key=_RETIRED_DISBOARD_HANDLER_KEY, slug=_DISBOARD_SLUG)
    )
    op.execute(
        sa.text(
            "DELETE FROM admin_handlers"
            " WHERE extension_install_id IN ("
            "  SELECT id FROM extension_installs WHERE extension_slug = :slug"
            " )"
        ).bindparams(slug=_RETIRED_SLUG)
    )
    op.execute(
        sa.text(
            "DELETE FROM extension_installs WHERE extension_slug = :slug"
        ).bindparams(slug=_RETIRED_SLUG)
    )


def downgrade() -> None:
    """Nothing to restore: the manifests and scripts these rows were rendered
    from left the repo with the extensions, so a downgrade cannot rebuild them.
    """
