"""A person's attestation that a company document is genuine, as fields, not prose.

Approval says a document is good evidence if it is real. Attestation says it IS real, and
who vouches for it. Until now that rested on a phrase in the free-text provenance, which
anyone could copy or mistype. Five columns on `document`:

  * `evidence_reality`          UNVERIFIED (default) / GENUINE / SYNTHETIC
  * `attested_by`, `attested_at`  who vouched, and when (set only with GENUINE)
  * `attestation_method`        how they checked (signed original seen, issuer confirmed ...)
  * `attested_version_hash`     sha256 of the file version that was vouched for; a new
                                version of the document voids the attestation

Only the evidence-approval endpoint writes them. The document's own text never grants
GENUINE.

Revision ID: 0050_evidence_attestation
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0050_evidence_attestation"
down_revision = "0049_evidence_governance"
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
    existing = _columns(op.get_bind())
    if "evidence_reality" not in existing:
        op.add_column(
            TABLE,
            sa.Column(
                "evidence_reality", sa.String(16), nullable=False, server_default="UNVERIFIED"
            ),
        )
    if "attested_by" not in existing:
        op.add_column(TABLE, sa.Column("attested_by", postgresql.UUID(as_uuid=True), nullable=True))
    if "attested_at" not in existing:
        op.add_column(TABLE, sa.Column("attested_at", sa.DateTime(timezone=True), nullable=True))
    if "attestation_method" not in existing:
        op.add_column(TABLE, sa.Column("attestation_method", sa.Text(), nullable=True))
    if "attested_version_hash" not in existing:
        op.add_column(TABLE, sa.Column("attested_version_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    for column in (
        "attested_version_hash",
        "attestation_method",
        "attested_at",
        "attested_by",
        "evidence_reality",
    ):
        op.drop_column(TABLE, column)
