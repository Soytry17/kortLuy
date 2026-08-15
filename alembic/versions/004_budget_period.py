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


def upgrade() -> None:
    conn = op.get_bind()

    # Check existing columns so we can skip ones already applied
    existing_cols = {
        row[1]
        for row in conn.execute(sa.text("PRAGMA table_info(category_budgets)")).fetchall()
    }
    period_exists = "period" in existing_cols

    # batch_alter_table recreates the table, which is required for SQLite
    # constraint changes (SQLite doesn't support DROP CONSTRAINT natively).
    with op.batch_alter_table("category_budgets", recreate="always") as batch_op:
        if not period_exists:
            batch_op.add_column(
                sa.Column(
                    "period",
                    sa.String(length=8),
                    nullable=False,
                    server_default="month",
                )
            )
        # Drop old constraint if it still exists (ignore if already gone)
        try:
            batch_op.drop_constraint("uq_budget_user_category", type_="unique")
        except Exception:
            pass
        # Create new constraint (ignore if already exists)
        try:
            batch_op.create_unique_constraint(
                "uq_budget_user_category_period",
                ["user_id", "category_id", "period"],
            )
        except Exception:
            pass


def downgrade() -> None:
    with op.batch_alter_table("category_budgets", recreate="always") as batch_op:
        try:
            batch_op.drop_constraint("uq_budget_user_category_period", type_="unique")
        except Exception:
            pass
        try:
            batch_op.create_unique_constraint(
                "uq_budget_user_category", "category_budgets", ["user_id", "category_id"]
            )
        except Exception:
            pass
        batch_op.drop_column("period")
