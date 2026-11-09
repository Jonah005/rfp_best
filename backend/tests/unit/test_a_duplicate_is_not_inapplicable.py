"""A duplicate deliverable is still a mandatory submission.

`coalesce_deliverables.py` first folded 18 of 32 rows by setting them
NOT_APPLICABLE. A review of that export rejected it on three counts, all
correct: 15 folded rows still read "mandatory", so the sheet said mandatory and
not-applicable about the same row; a duplicate is not inapplicable, because the
tender really does require that submission; and nothing recorded WHICH
deliverable a row had been folded into, so the merge could not be reviewed,
reversed or explained.

The schema already had the right pattern twice --
`ClarificationIssue.restates_clarification_id` and
`Requirement.superseded_by_requirement_id`. Deliverables got a status flag
instead. That is the same "pattern applied in some places and not the one in
front of you" failure that let 25 dangling requirement codes come back a day
after they were fixed.
"""

from __future__ import annotations

from app.models import Deliverable
from app.models.enums import DeliverableStatus


def test_a_deliverable_can_point_at_its_canonical_twin() -> None:
    for column in ("duplicate_of_deliverable_id", "equivalence_by", "equivalence_reason"):
        assert hasattr(Deliverable, column), column


def test_the_link_is_tenant_scoped() -> None:
    """A self-referential FK on (id, org_id), not on id alone. Without the
    composite target a duplicate could point at another organization's
    deliverable, and under RLS FORCE that resolves to nothing rather than to an
    error -- the failure mode this schema is built to refuse."""
    names = {
        getattr(c, "name", None) for c in Deliverable.__table__.constraints
    }
    assert "fk_deliverable_duplicate_of_org" in names


def test_folding_is_not_expressed_as_a_status() -> None:
    """NOT_APPLICABLE keeps its own meaning: we do not have to do this.

    If a future change re-adds a DUPLICATE status, this is the test that should
    make somebody argue for it rather than reach for it -- the link carries
    which twin, the reason and the provenance, and a status carries none of the
    three.
    """
    assert not hasattr(DeliverableStatus, "DUPLICATE")
    assert not hasattr(DeliverableStatus, "DUPLICATE_OF")


# Deliberately not written here: a test asserting the export renders
# "Duplicate of". The only way to check that without a database is to grep
# `collect`'s source for the header string, and this project has already been
# burned by exactly that -- a test read a function's source for a field name and
# kept passing after the field was dropped from the output. Cover that cannot
# fail is worse than none, because it is counted. The export assertion belongs
# in tests/integration, against a workbook that was actually written.
