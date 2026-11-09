"""Widen clause_ref so a qualified reference fits.

A clause reference used to be the bare number the document printed -- "3" --
which fitted varchar(64) with room to spare. It also meant three unrelated
obligations in three sections shared one identity, so precedence held a contest
between them, kept one, and discarded the rest before requirement extraction
ever ran.

The reference is now qualified by the section it sits under
("Business Requirements / 3"), which is what makes it name one clause. Real
headings are long: one document in the corpus carries

    8.11 Appendix I Technical Proposal Format Page 19 of 32|<RFP Template.docx> <date>

as a heading, and qualifying a reference under it produces 86 characters. At
varchar(64) that is not a truncation, it is an INSERT that fails -- so this
migration must land before any bid is re-analysed.

`clause_identity.MAX_CLAUSE_REF` is 200 and matches the width chosen here. A
value longer than that keeps a readable prefix and gains a digest, so two long
headings sharing a prefix still cannot collide.

Widening a varchar is not a rewrite in PostgreSQL and takes no table lock worth
the name. Nothing is lost, and downgrade is only safe while no stored value
exceeds 64 characters -- which it will, so the downgrade truncates explicitly
rather than failing halfway.

Revision ID: 0018_qualified_clause_ref
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_qualified_clause_ref"
down_revision = "0017_cited_requests"
branch_labels = None
depends_on = None

WIDTH = 200


def upgrade() -> None:
    op.alter_column(
        "clause_resolution",
        "clause_ref",
        existing_type=sa.String(64),
        type_=sa.String(WIDTH),
        existing_nullable=False,
    )
    op.alter_column(
        "requirement_version",
        "clause_ref",
        existing_type=sa.String(64),
        type_=sa.String(WIDTH),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Values written since the upgrade can exceed 64 characters, and a plain
    # ALTER would abort partway through. Truncating first is lossy and said out
    # loud rather than discovered: going back to a bare-reference world loses
    # the section qualification, which is the whole point of the change.
    op.execute("UPDATE clause_resolution SET clause_ref = left(clause_ref, 64)")
    op.execute("UPDATE requirement_version SET clause_ref = left(clause_ref, 64)")
    op.alter_column(
        "clause_resolution",
        "clause_ref",
        existing_type=sa.String(WIDTH),
        type_=sa.String(64),
        existing_nullable=False,
    )
    op.alter_column(
        "requirement_version",
        "clause_ref",
        existing_type=sa.String(WIDTH),
        type_=sa.String(64),
        existing_nullable=True,
    )
