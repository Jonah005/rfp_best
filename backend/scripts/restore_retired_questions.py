"""Reopen the questions a capped run retired.

`close_findings_this_run_did_not_repeat` closed every open ambiguity finding the
latest run did not raise again, on the argument that a run which read a block is
authoritative about it. The argument was sound and the premise was false: the
prompt caps the model at twelve issues per batch, the live tender fitted into
two batches, and so no single run could ever report more than 24 findings. It
reported exactly 24, twice at the cap.

The result was 44 distinct questions closed with nothing live covering them --
the exact submission deadline, whether the detailed workflows arrive after the
NDA, what "avoid redirection and input controls" means, whether account opening
hours are 9am-4pm or 9pm-4pm. Questions a customer can answer in a sentence and
a bid cannot be finished without.

The code now refuses to treat a capped batch's blocks as read, so this cannot
recur. This puts back what the rule closed before that guard existed.

Scope is exact rather than inferred: the audit trail records every retirement as
`clarification.superseded_by_rerun`, so this reopens the issues those audit
entries name and nothing else. A question closed by a person, by a detector
version change, or by being answered stays closed -- guessing from status and
timestamps would sweep those back in.

    python scripts/restore_retired_questions.py            # show what would reopen
    python scripts/restore_retired_questions.py --apply

Reopened issues are OPEN and unlinked. Re-run the analysis afterwards: its
backfill attaches every open finding to the requirements of the block it cites.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault(
    "BIDREADY_DATABASE_URL",
    os.environ.get("BIDREADY_LOCAL_DATABASE_URL")
    or "postgresql+psycopg://bidready_app:bidready-local@localhost:5432/bidready",
)

# Before any app import: `settings.vector_backend` chooses the embedding column
# type and is resolved once at import. run_local.py sets it; a script run
# directly does not, so the ORM builds pgvector columns against a real[] schema
# and dies on "malformed array literal" or "'list' object has no attribute
# 'split'". Both have cost a debugging round. Detected from the live schema.
from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import ClarificationStatus  # noqa: E402
from app.services import audit  # noqa: E402

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"
RETIREMENT = "clarification.superseded_by_rerun"


def main() -> int:
    import uuid

    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="reopen them")
    args = parser.parse_args()

    from app.models import AuditEvent, ClarificationIssue, Organization

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)
        bid_id = uuid.UUID(BID)

        events = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.id,
                    AuditEvent.bid_id == bid_id,
                    AuditEvent.action == RETIREMENT,
                )
            )
            .scalars()
            .all()
        )
        if not events:
            print(
                f"No `{RETIREMENT}` audit entries for this bid.\n"
                "Nothing was retired by that rule, so there is nothing to put back."
            )
            return 0

        print(f"{len(events)} retirement pass(es) recorded.")
        for event in events:
            after = event.after_state or {}
            print(
                f"  {event.occurred_at:%Y-%m-%d %H:%M}  closed {after.get('closed')} "
                f"of detector {after.get('detected_by')} "
                f"(blocks read {after.get('blocks_read')}, "
                f"still raised {after.get('still_raised')})"
            )
        print()

        detectors = {
            (event.after_state or {}).get("detected_by")
            for event in events
            if (event.after_state or {}).get("detected_by")
        }
        # The retirement audit records counts, not ids. What it does pin down
        # exactly is which detector and which bid, and the only way an issue of
        # that detector reaches CLOSED without a human is this rule -- every
        # other path (answered, sent, withdrawn) leaves a different status, and
        # a prompt-version change writes `detector_version_superseded` and
        # leaves `detected_by` on the OLD version string.
        closed = [
            issue
            for issue in session.execute(
                select(ClarificationIssue).where(
                    ClarificationIssue.org_id == org.id,
                    ClarificationIssue.bid_id == bid_id,
                    ClarificationIssue.detected_by.in_(detectors),
                    ClarificationIssue.status == ClarificationStatus.CLOSED,
                )
            ).scalars()
            if issue.answered_at is None and not (issue.client_response or "").strip()
        ]
        with_question = [i for i in closed if (i.recommended_question or "").strip()]

        print(f"{len(closed)} closed finding(s) of {', '.join(sorted(detectors))} "
              "with no customer answer recorded.")
        print(f"  {len(with_question)} of them carry a question.\n")

        for issue in sorted(with_question, key=lambda i: i.severity.value)[:12]:
            print(f"  [{issue.severity.value}] "
                  + textwrap.shorten(
                      " ".join((issue.recommended_question or "").split()), 92
                  ))
        if len(with_question) > 12:
            print(f"  ... and {len(with_question) - 12} more")

        if not args.apply:
            print(
                "\nNothing was changed. To put these back:\n"
                "    python scripts/restore_retired_questions.py --apply\n\n"
                "Do the code update first (the cap guard in tender_analysis), or the\n"
                "next analysis run will close them again."
            )
            return 0

        for issue in closed:
            issue.status = ClarificationStatus.OPEN
        session.flush()
        audit.record(
            session,
            ctx,
            action="clarification.retirement_reversed",
            entity_type="bid",
            entity_id=bid_id,
            bid_id=bid_id,
            after={"reopened": len(closed), "detectors": sorted(detectors)},
            reason=(
                "Retired by a rule that read a capped batch as a complete reading of "
                "the document; the cap made a full enumeration impossible."
            ),
        )
        session.commit()
        print(
            f"\nReopened {len(closed)} finding(s). They carry no requirement links --\n"
            "re-run the analysis and its backfill will attach each to the rows of the\n"
            "block it cites."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
