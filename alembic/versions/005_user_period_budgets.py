"""Add period budget columns to users table.

Revision ID: 005_user_period_budgets
Revises: 004_budget_period
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005_user_period_budgets"
down_revision: Union[str, None] = "004_budget_period"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    existing = {
        row[1]
        for row in conn.execute(sa.text("PRAGMA table_info(users)")).fetchall()
    }
    for col in ("budget_today_cents", "budget_week_cents", "budget_month_cents"):
        if col not in existing:
            op.add_column("users", sa.Column(col, sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        for col in ("budget_today_cents", "budget_week_cents", "budget_month_cents"):
            batch_op.drop_column(col)
