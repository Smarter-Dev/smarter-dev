"""backfill costs for DigitalOcean-served chat models

Historically every DigitalOcean-served chat turn was priced at $0: the
models are unknown to genai-prices and calc_cost swallowed the lookup
failure. Token counts were always recorded, so the true cost is
recomputable. This migration re-prices those rows at DigitalOcean's
serverless-inference rates (docs.digitalocean.com, 2026-07, USD per
million tokens — deliberately inlined so this migration stays a frozen
snapshot independent of application code) and rebuilds the denormalised
engagement cost rollups for every affected engagement.

Only rows whose cost is currently 0/NULL are touched, so re-running or
running after the new pricing code is live never clobbers a real cost.

Revision ID: b7d4e2f8c1a5
Revises: a3f9c1e7b4d2
Create Date: 2026-07-21 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7d4e2f8c1a5"
down_revision: Union[str, None] = "a3f9c1e7b4d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# model wire id -> (input $/Mtok, output $/Mtok)
DIGITALOCEAN_RATES: dict[str, tuple[str, str]] = {
    "kimi-k2.6": ("0.76", "3.20"),
    "glm-5.2": ("1.05", "4.40"),
    "deepseek-4-flash": ("0.112", "0.224"),
    "gemma-4-31B-it": ("0.18", "0.50"),
    "qwen3.5-397b-a17b": ("0.385", "2.45"),
}

_AFFECTED_ENGAGEMENTS = """
    SELECT DISTINCT t.engagement_id
    FROM chat_agent_turns t
    WHERE t.chat_model_name IN :model_ids
    UNION
    SELECT DISTINCT t2.engagement_id
    FROM chat_agent_compaction_events ce
    JOIN chat_agent_turns t2 ON ce.turn_id = t2.id
    WHERE ce.summarizer_model_name IN :model_ids
"""


def _rebuild_engagement_rollups(connection) -> None:
    model_ids = tuple(DIGITALOCEAN_RATES)
    connection.execute(
        sa.text(
            f"""
            UPDATE chat_agent_engagements e
            SET total_chat_cost_usd = COALESCE((
                    SELECT SUM(t.chat_cost_usd)
                    FROM chat_agent_turns t
                    WHERE t.engagement_id = e.id
                ), 0),
                total_compaction_cost_usd = COALESCE((
                    SELECT SUM(ce.summarizer_cost_usd)
                    FROM chat_agent_compaction_events ce
                    JOIN chat_agent_turns t2 ON ce.turn_id = t2.id
                    WHERE t2.engagement_id = e.id
                ), 0)
            WHERE e.id IN ({_AFFECTED_ENGAGEMENTS})
            """
        ).bindparams(sa.bindparam("model_ids", expanding=True)),
        {"model_ids": list(model_ids)},
    )
    connection.execute(
        sa.text(
            f"""
            UPDATE chat_agent_engagements e
            SET total_cost_usd = COALESCE(e.total_chat_cost_usd, 0)
                + COALESCE(e.total_voice_cost_usd, 0)
                + COALESCE(e.total_compaction_cost_usd, 0)
            WHERE e.id IN ({_AFFECTED_ENGAGEMENTS})
            """
        ).bindparams(sa.bindparam("model_ids", expanding=True)),
        {"model_ids": list(model_ids)},
    )


def upgrade() -> None:
    connection = op.get_bind()
    for model_id, (input_rate, output_rate) in DIGITALOCEAN_RATES.items():
        connection.execute(
            sa.text(
                f"""
                UPDATE chat_agent_turns
                SET chat_cost_usd =
                    (chat_tokens_input * {input_rate}
                     + chat_tokens_output * {output_rate}) / 1000000
                WHERE chat_model_name = :model_id
                  AND COALESCE(chat_cost_usd, 0) = 0
                """
            ),
            {"model_id": model_id},
        )
        connection.execute(
            sa.text(
                f"""
                UPDATE chat_agent_compaction_events
                SET summarizer_cost_usd =
                    (summarizer_tokens_input * {input_rate}
                     + summarizer_tokens_output * {output_rate}) / 1000000
                WHERE summarizer_model_name = :model_id
                  AND COALESCE(summarizer_cost_usd, 0) = 0
                """
            ),
            {"model_id": model_id},
        )
    _rebuild_engagement_rollups(connection)


def downgrade() -> None:
    connection = op.get_bind()
    model_ids = list(DIGITALOCEAN_RATES)
    connection.execute(
        sa.text(
            """
            UPDATE chat_agent_turns
            SET chat_cost_usd = 0
            WHERE chat_model_name IN :model_ids
            """
        ).bindparams(sa.bindparam("model_ids", expanding=True)),
        {"model_ids": model_ids},
    )
    connection.execute(
        sa.text(
            """
            UPDATE chat_agent_compaction_events
            SET summarizer_cost_usd = 0
            WHERE summarizer_model_name IN :model_ids
            """
        ).bindparams(sa.bindparam("model_ids", expanding=True)),
        {"model_ids": model_ids},
    )
    _rebuild_engagement_rollups(connection)
