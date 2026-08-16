"""Add current_cents to savings_goals.

Revision ID: 007_goal_current_cents
Revises: 006_savings_goals
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007_goal_current_cents"
down_revision: Union[str, None] = "006_savings_goals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _col_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    conn = op.get_bind()
    if not _col_exists(conn, "savings_goals", "current_cents"):
        op.add_column(
            "savings_goals",
            sa.Column("current_cents", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    op.drop_column("savings_goals", "current_cents")
