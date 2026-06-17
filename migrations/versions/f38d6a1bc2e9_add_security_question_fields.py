"""add security question fields to user table

Revision ID: f38d6a1bc2e9
Revises: eefeb988f3eb
Create Date: 2026-06-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f38d6a1bc2e9'
down_revision = 'eefeb988f3eb'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user', sa.Column('security_question', sa.String(length=255), nullable=True))
    op.add_column('user', sa.Column('security_answer_hash', sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column('user', 'security_answer_hash')
    op.drop_column('user', 'security_question')
