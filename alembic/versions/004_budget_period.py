"""Add period column to category_budgets for daily/weekly/monthly limits.

Revision ID: 004_budget_period
Revises: 003_user_khr_rate
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004_budget_period"
down_revision: Union[str, None] = "003_user_khr_rate"
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

    if not _col_exists(conn, "category_budgets", "period"):
        op.add_column(
            "category_budgets",
            sa.Column(
                "period",
                sa.String(length=8),
                nullable=False,
                server_default="month",
            ),
        )

    # Drop old constraint if it exists, create new one
    try:
        op.drop_constraint("uq_budget_user_category", "category_budgets", type_="unique")
    except Exception:
        pass
    try:
        op.create_unique_constraint(
            "uq_budget_user_category_period",
            "category_budgets",
            ["user_id", "category_id", "period"],
        )
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_constraint("uq_budget_user_category_period", "category_budgets", type_="unique")
    except Exception:
        pass
    try:
        op.create_unique_constraint(
            "uq_budget_user_category", "category_budgets", ["user_id", "category_id"]
        )
    except Exception:
        pass
    op.drop_column("category_budgets", "period")
