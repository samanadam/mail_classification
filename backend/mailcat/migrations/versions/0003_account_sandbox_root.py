"""account sandbox root for the provider test

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-04 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('sandbox_root', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('accounts', 'sandbox_root')
