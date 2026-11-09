"""Separate what a piece of evidence IS from whether it exists yet.

`Evidence.evidence_type` says what kind of material an excerpt is, and
`NOT_SELF_EVIDENCING` downgrades whole KINDS -- a blank form, a pricing
template, a customer's old tender. That set is correct and it is not enough for
the material this company actually holds:

    Credential: ISO/IEC 27001 Information Security | Scope: [REPLACE scope]
      | Certificate no.: [REPLACE] | Evidence status: NOT EVIDENCED

That row is genuinely about a certificate. Typing it CERTIFICATE is right, and
makes it read as proof the company holds one. Typing it FORM to force the
downgrade would be false about what it is and would drop "ISO 27001" out of
every shortlist that should surface it. One enum cannot answer both questions,
which is the same reason `evidence_status` and `bid_readiness` were never
collapsed into one column.

So: a second axis, nullable on purpose.

    NULL   nobody has judged this row yet   -- today, every row
    false  a request for evidence, not evidence
    true   judged to stand on its own

NULL rather than a `true` default. A default of true would mean "assume proof
until told otherwise" and would make an unjudged row indistinguishable from a
judged-good one -- the shape of bug that has cost this project five separate
incidents, most recently a 25-character minimum that silently deleted "Working
days". An unjudged row must be visibly unjudged.

`classified_by` carries `markers@v1`, `llm:evidence_classification@vN` or
`human:<user_id>`, like every other derived value here, so a verdict always
names the reading that produced it and can be re-judged when that reading moves.

Revision ID: 0021_evidence_substantiation
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0021_evidence_substantiation"
down_revision = "0020_clarification_equivalence"
branch_labels = None
depends_on = None

TABLE = "evidence"


def _columns(bind) -> set[str]:
    return {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
            ),
            {"t": TABLE},
        )
    }


def upgrade() -> None:
    # Idempotent, for the reason 0015, 0017, 0019 and 0020 give: databases in
    # this project have been repaired by hand, and a migration that fails
    # against an already-correct schema is one people learn to skip.
    bind = op.get_bind()
    existing = _columns(bind)

    if "is_substantiated" not in existing:
        op.add_column(TABLE, sa.Column("is_substantiated", sa.Boolean(), nullable=True))
    if "substantiation_note" not in existing:
        op.add_column(TABLE, sa.Column("substantiation_note", sa.Text(), nullable=True))
    if "classified_by" not in existing:
        op.add_column(TABLE, sa.Column("classified_by", sa.String(128), nullable=True))

    # Assessment asks for the unsubstantiated rows attached to a requirement,
    # and the classifier asks for the unjudged ones. Both are org-scoped and
    # both are the narrow end of the table, so a partial index on the judged
    # column serves them without carrying the 600 NULLs.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evidence_substantiation "
        "ON evidence (org_id, is_substantiated) "
        "WHERE is_substantiated IS NOT NULL"
    )


def downgrade() -> None:
    # Dropping these loses the verdicts, not the evidence. Every excerpt keeps
    # its text, its type and its links; re-running the classifier restores the
    # judgments, and the marker pass restores its share of them for free.
    op.execute("DROP INDEX IF EXISTS ix_evidence_substantiation")
    bind = op.get_bind()
    existing = _columns(bind)
    for column in ("classified_by", "substantiation_note", "is_substantiated"):
        if column in existing:
            op.drop_column(TABLE, column)
