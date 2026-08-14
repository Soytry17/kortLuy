"""Add khr_rate to users table for KHR/USD conversion.

Revision ID: 003_user_khr_rate
Revises: 002_category_budgets
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_user_khr_rate"
down_revision: Union[str, None] = "002_category_budgets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("khr_rate", sa.Integer(), nullable=False, server_default="4100"),
    )


def downgrade() -> None:
    op.drop_column("users", "khr_rate")
