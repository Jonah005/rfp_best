"""Each chat turn records what the person was looking at when it was said.

Revision ID: 0042_chat_focus
"""

from __future__ import annotations

from alembic import op

revision = "0042_chat_focus"
down_revision = "0041_gap_answers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS focus_key varchar(300)")
    op.execute("ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS focus jsonb")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_message_focus "
        "ON chat_message (org_id, conversation_id, focus_key, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chat_message_focus")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS focus")
    op.execute("ALTER TABLE chat_message DROP COLUMN IF EXISTS focus_key")
