"""Add runtime-only Team Agent profiles and pin project Agents to a version.

Revision ID: 20260830_51
Revises: 20260830_50
Create Date: 2026-08-30
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_51"
down_revision: str | None = "20260830_50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    profiles = op.create_table(
        "product_team_agent_profiles",
        sa.Column("profile_id", sa.String(128), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("tool_policy_id", sa.String(128), nullable=False),
        sa.Column("skill_policy_id", sa.String(128), nullable=False),
        sa.Column("model_policy_id", sa.String(128), nullable=False),
        sa.Column("memory_policy_id", sa.String(128), nullable=False),
        sa.Column("autonomy_level", sa.String(32), nullable=False),
        sa.Column("max_run_budget_profile", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["product_teams.team_id"]),
        sa.CheckConstraint("version >= 1", name="positive_version"),
        sa.CheckConstraint(
            "autonomy_level IN ('supervised','bounded','autonomous')",
            name="autonomy_level",
        ),
        sa.UniqueConstraint(
            "team_id", "version", name="uq_team_agent_profile_version"
        ),
    )
    op.create_index(
        "ix_product_team_agent_profiles_team",
        "product_team_agent_profiles",
        ["team_id", "version"],
    )
    bind = op.get_bind()
    teams = sa.table(
        "product_teams",
        sa.column("team_id", sa.String(128)),
        sa.column("name", sa.String(128)),
    )
    now = datetime.now(UTC)
    rows = bind.execute(sa.select(teams.c.team_id, teams.c.name)).mappings().all()
    if rows:
        op.bulk_insert(
            profiles,
            [
                {
                    "profile_id": row["team_id"],
                    "version": 1,
                    "team_id": row["team_id"],
                    "display_name": f"{row['name']} Agent",
                    "tool_policy_id": "default",
                    "skill_policy_id": "default",
                    "model_policy_id": "default",
                    "memory_policy_id": "default",
                    "autonomy_level": "bounded",
                    "max_run_budget_profile": {
                        "max_turns": 20,
                        "max_tool_calls": 50,
                        "max_total_tokens": 100000,
                        "max_model_cost_microusd": 10000000,
                    },
                    "created_at": now,
                }
                for row in rows
            ],
        )
    with op.batch_alter_table("product_team_project_agents") as batch:
        batch.add_column(sa.Column("profile_id", sa.String(128), nullable=True))
        batch.add_column(
            sa.Column(
                "profile_version",
                sa.Integer(),
                nullable=True,
                server_default="1",
            )
        )
    op.execute(
        "UPDATE product_team_project_agents "
        "SET profile_id = team_id, profile_version = 1"
    )
    with op.batch_alter_table("product_team_project_agents") as batch:
        batch.alter_column(
            "profile_id", existing_type=sa.String(128), nullable=False
        )
        batch.alter_column(
            "profile_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="1",
        )
        batch.create_check_constraint(
            "positive_profile_version", "profile_version >= 1"
        )
        batch.create_foreign_key(
            "fk_team_project_agent_profile",
            "product_team_agent_profiles",
            ["profile_id", "profile_version"],
            ["profile_id", "version"],
        )


def downgrade() -> None:
    with op.batch_alter_table("product_team_project_agents") as batch:
        batch.drop_constraint("fk_team_project_agent_profile", type_="foreignkey")
        batch.drop_constraint("positive_profile_version", type_="check")
        batch.drop_column("profile_version")
        batch.drop_column("profile_id")
    op.drop_index(
        "ix_product_team_agent_profiles_team",
        table_name="product_team_agent_profiles",
    )
    op.drop_table("product_team_agent_profiles")
