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


def upgrade() -> None:
    conn = op.get_bind()
    cols = [row[1] for row in conn.execute(sa.text("PRAGMA table_info(savings_goals)"))]
    if "current_cents" not in cols:
        op.add_column(
            "savings_goals",
            sa.Column("current_cents", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    with op.batch_alter_table("savings_goals") as batch_op:
        batch_op.drop_column("current_cents")
