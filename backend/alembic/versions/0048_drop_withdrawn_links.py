"""Tasks and drafts stop pointing at requirements that were withdrawn.

Withdrawing a requirement used to retire its verdict and move its questions, but
leave the tasks pointing at it. Those tasks went on answering a clause that no
longer exists while the diagram and gap checks, which ignore withdrawn clauses,
saw nothing. New withdrawals drop the link (see `requirements.withdraw`); this
cleans up the ones already made.

Revision ID: 0048_drop_withdrawn_links
"""

from __future__ import annotations

from alembic import op

revision = "0048_drop_withdrawn_links"
down_revision = "0047_gap_check_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("task_requirement", "artifact_requirement"):
        op.execute(
            f"DELETE FROM {table} WHERE requirement_id IN "
            "(SELECT id FROM requirement WHERE lifecycle_state = 'WITHDRAWN')"
        )


def downgrade() -> None:
    # The links were stale; there is nothing to restore.
    pass
