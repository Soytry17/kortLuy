"""Add category_budgets table for monthly spending limits.

Revision ID: 002_category_budgets
Revises: 001_initial
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_category_budgets"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "category_budgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "category_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=False
        ),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.UniqueConstraint("user_id", "category_id", name="uq_budget_user_category"),
    )
    op.create_index("ix_category_budgets_user_id", "category_budgets", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_category_budgets_user_id", table_name="category_budgets")
    op.drop_table("category_budgets")
