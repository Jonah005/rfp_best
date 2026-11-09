"""Where a company document came from, and whether a person cleared it for a bid.

Documents the tender asks for with the proposal (CVs, load-test report, standard terms,
project references) were attached on the strength of their file type alone. Nothing
recorded who cleared them, from what source, or whether they may leave the company.

Six columns on `document`, all human decisions:

  * `evidence_status`      UNREVIEWED (default) / APPROVED / REJECTED
  * `evidence_use_scope`   INTERNAL_ONLY (default) / EXTERNAL_BID
  * `evidence_provenance`  where it came from, in the approver's words
  * `evidence_note`        anything the approver wants to keep
  * `evidence_decided_by`, `evidence_decided_at`  who decided, and when

Nothing is backfilled: every existing document reads UNREVIEWED and INTERNAL_ONLY, which
is the truth. A document that says it is a test fixture cannot be approved at all; that
check is made on its text, not on these columns.

Revision ID: 0049_evidence_governance
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0049_evidence_governance"
down_revision = "0048_drop_withdrawn_links"
branch_labels = None
depends_on = None

TABLE = "document"


def _columns(bind) -> set[str]:
    return {
        row[0]
        for row in bind.execute(
            sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
            {"t": TABLE},
        )
    }


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind)
    status = postgresql.ENUM(
        "UNREVIEWED", "APPROVED", "REJECTED", name="document_evidence_status", create_type=False
    )
    scope = postgresql.ENUM("INTERNAL_ONLY", "EXTERNAL_BID", name="use_scope", create_type=False)
    status.create(bind, checkfirst=True)
    scope.create(bind, checkfirst=True)
    if "evidence_status" not in existing:
        op.add_column(
            TABLE,
            sa.Column(
                "evidence_status", status, nullable=False, server_default="UNREVIEWED"
            ),
        )
    if "evidence_use_scope" not in existing:
        op.add_column(
            TABLE,
            sa.Column(
                "evidence_use_scope", scope, nullable=False, server_default="INTERNAL_ONLY"
            ),
        )
    if "evidence_provenance" not in existing:
        op.add_column(TABLE, sa.Column("evidence_provenance", sa.Text(), nullable=True))
    if "evidence_note" not in existing:
        op.add_column(TABLE, sa.Column("evidence_note", sa.Text(), nullable=True))
    if "evidence_decided_by" not in existing:
        op.add_column(
            TABLE, sa.Column("evidence_decided_by", postgresql.UUID(as_uuid=True), nullable=True)
        )
    if "evidence_decided_at" not in existing:
        op.add_column(
            TABLE, sa.Column("evidence_decided_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    for column in (
        "evidence_decided_at",
        "evidence_decided_by",
        "evidence_note",
        "evidence_provenance",
        "evidence_use_scope",
        "evidence_status",
    ):
        op.drop_column(TABLE, column)
    op.execute("DROP TYPE IF EXISTS use_scope")
    op.execute("DROP TYPE IF EXISTS document_evidence_status")
