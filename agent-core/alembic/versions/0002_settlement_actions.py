"""synthetic settlement system ledger (idempotent, reversible actions)

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settlement_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("case_id", sa.Uuid(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("inverse", postgresql.JSONB(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_settlement_actions_case_id", "settlement_actions", ["case_id"])
    # Executed actions are a record of fact: never deleted, and only `reversed_at` may change.
    op.execute(
        """
        CREATE FUNCTION settlement_actions_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'settlement_actions is append-only (DELETE not allowed)';
            END IF;
            IF NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.case_id IS DISTINCT FROM OLD.case_id
               OR NEW.action IS DISTINCT FROM OLD.action
               OR NEW.parameters IS DISTINCT FROM OLD.parameters
               OR NEW.inverse IS DISTINCT FROM OLD.inverse
               OR NEW.executed_at IS DISTINCT FROM OLD.executed_at
               OR OLD.reversed_at IS NOT NULL THEN
                RAISE EXCEPTION 'settlement_actions is append-only (only one reversal allowed)';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER settlement_actions_guard
        BEFORE UPDATE OR DELETE ON settlement_actions
        FOR EACH ROW EXECUTE FUNCTION settlement_actions_guard();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS settlement_actions_guard ON settlement_actions")
    op.drop_table("settlement_actions")
    op.execute("DROP FUNCTION IF EXISTS settlement_actions_guard()")
