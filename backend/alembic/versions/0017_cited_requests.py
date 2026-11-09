"""Record which cross-department answers a reply rested on.

The copilot answered a staffing question entirely out of an answer HR had
written into a cross-department request. It cited that answer four times, as
[R1], correctly and in the right places -- and the line under the reply read
"no sources cited".

Two columns existed for the two kinds of backing anyone had thought about:
corpus chunks and approved evidence. A colleague's written answer is a third,
and it was landing in neither, so an answer built entirely on a governed
cross-department resolution presented as an answer built on nothing. In a
product whose whole claim is that every statement is attributable, that reads
as the attribution failing exactly when it was working.

It gets its own column rather than being folded into `cited_evidence_ids`,
because the three are not interchangeable: a chunk is retrieved text, an
evidence item passed an approval gate, and a resolution is a named person's
word, given for a stated business reason, on a specific date. A reviewer asking
which claims rest on somebody's word rather than on a document is asking a real
question, and it should be answerable in SQL rather than by parsing JSON.

Revision ID: 0017_cited_requests
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from alembic import op

revision = "0017_cited_requests"
down_revision = "0016_notification"
branch_labels = None
depends_on = None

TABLE = "chat_message"
COLUMN = "cited_request_ids"


def upgrade() -> None:
    # Idempotent for the same reason 0015 is: local databases in this project
    # have been repaired by hand more than once, and a migration that fails on
    # an already-correct schema is a migration people learn to skip.
    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = '{TABLE}' AND column_name = '{COLUMN}'
          ) THEN
            ALTER TABLE {TABLE} ADD COLUMN {COLUMN} uuid[];
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.drop_column(TABLE, COLUMN)


_ = (sa, PGUUID)
