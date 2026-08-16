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
    for col in ("budget_today_cents", "budget_week_cents", "budget_month_cents"):
        if not _col_exists(conn, "users", col):
            op.add_column("users", sa.Column(col, sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in ("budget_today_cents", "budget_week_cents", "budget_month_cents"):
        op.drop_column("users", col)
