"""Put R-055's text back through the matcher and see what it decides now.

Both hypotheses are dead. The block key is shared, the candidate lookup returns
R-050, and the two rows score 1.000 on BOTH the quoted clause and the model's
restatement -- so the failure is not a split key, not an empty candidate list,
not the wrong field, and not a threshold.

Reasoning has now been wrong four times on this file, so this replays the
decision instead of arguing about it.

Method, and the one part worth checking before trusting the output: R-055 is
marked WITHDRAWN in an open transaction that is ROLLED BACK at the end. Nothing
is committed. That is not a trick to hide the row -- `_requirements_from_block`
filters on lifecycle_state, so withdrawing R-055 in the session reconstructs
exactly the candidate set the matcher saw before R-055 existed. The real
`_find_existing` and the real `_reconcile_against_existing` are then called with
a proposal carrying R-055's own text and block ordinal. No part of either
decision is reimplemented here; a diagnostic that restates its subject is how
`check_extraction_funnel.py` came to report the pre-fix number after the fix
landed.

Two outcomes, two different jobs:

  matched R-050  -- the code in the tree is correct and R-055 came from a run
                    that predates it. Nothing to fix in the pipeline; withdraw
                    the row and confirm by re-running the analysis.

  matched nothing -- a live bug, still reproducing, and the printout says which
                    of the two paths dropped it.

    python scripts/replay_r055.py [CODE]        (default R-055)
"""

from __future__ import annotations

import os
import sys
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
from app.models.enums import RequirementCategory, RequirementLifecycle  # noqa: E402
from app.schemas.tender_extraction import ExtractedRequirement  # noqa: E402
from app.services.clause_identity import qualified_clause_ref  # noqa: E402
from app.services.requirements import (  # noqa: E402
    _ECHO_OF,
    _find_existing,
    _reconcile_against_existing,
    _requirements_from_block,
    _similarity,
)


def main() -> int:
    code = (sys.argv[1] if len(sys.argv) > 1 else "R-055").upper()

    from app.models import DocumentBlock, Organization, Requirement, RequirementVersion

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        row = session.execute(
            select(Requirement, RequirementVersion)
            .join(RequirementVersion, RequirementVersion.requirement_id == Requirement.id)
            .where(
                Requirement.org_id == org.id,
                Requirement.code == code,
                RequirementVersion.superseded_at.is_(None),
            )
        ).first()
        if row is None:
            print(f"No live requirement {code}.")
            return 1
        requirement, version = row
        locator = version.source_locator if isinstance(version.source_locator, dict) else {}

        block = session.execute(
            select(DocumentBlock).where(
                DocumentBlock.org_id == org.id,
                DocumentBlock.document_version_id == version.source_document_version_id,
                DocumentBlock.char_start == locator.get("char_start"),
                DocumentBlock.char_end == locator.get("char_end"),
            )
        ).scalars().first()
        if block is None:
            print("No document block at that locator.")
            return 1

        reference = qualified_clause_ref(block)
        print(f"{code}: block ordinal {block.ordinal}, chars {block.char_start}-{block.char_end}")
        print(f"  block.clause_ref        = {block.clause_ref!r}")
        # The branch selector. `_find_existing` matches on wording only when
        # this is None; when it is set, identity is the reference and a stored
        # version whose own clause_ref is NULL is invisible to that query.
        print(f"  qualified_clause_ref()  = {reference!r}")
        print(f"  stored version.clause_ref = {version.clause_ref!r}")
        print(f"  reconciliation {'SKIPS' if reference is not None else 'HANDLES'} this block")

        print("\n  candidates WITH " + code + " still live:")
        for candidate, _text in _requirements_from_block(
            session, ctx, bid_id=requirement.bid_id, block=block
        ):
            print(f"    {candidate.code}  clause_ref={_stored_ref(session, candidate)!r}")

        # Reconstruct the state the matcher saw before this row existed.
        # Rolled back below; nothing is committed by this script.
        #
        # The ONE place that sets this column directly on purpose, and it is
        # not a withdrawal: it is a simulation of one, to ask what
        # `_requirements_from_block` would have returned. Calling
        # `requirements.withdraw` here would move real clarification links and
        # write a real audit row for something that never happened. Every
        # actual withdrawal goes through the service.
        requirement.lifecycle_state = RequirementLifecycle.WITHDRAWN
        session.flush()

        candidates = _requirements_from_block(
            session, ctx, bid_id=requirement.bid_id, block=block
        )
        normalized = (version.normalized_requirement or version.requirement_text).strip()
        # The stored side of this comparison is the CLAUSE, so the proposal side
        # must be too. This script previously scored the restatement here and
        # printed 0.748 beside a pipeline that had already been fixed -- the
        # diagnostic reporting the bug it was written to confirm gone.
        match_text = version.requirement_text.strip()
        print(f"\n  candidates WITHOUT {code} ({len(candidates)}):")
        for candidate, text in candidates:
            print(
                f"    {candidate.code}  clause {_similarity(text, match_text):.3f}"
                f"   (restatement, the old comparison: {_similarity(text, normalized):.3f})"
            )

        print("\n  --- path 1: _find_existing (used when reconciliation declines) ---")
        found = _find_existing(
            session,
            ctx,
            bid_id=requirement.bid_id,
            block=block,
            normalized=normalized,
            match_text=match_text,
        )
        print(f"    -> {found.code if found is not None else 'NOTHING -- would create a new row'}")

        print("\n  --- path 2: _reconcile_against_existing (the batch matcher) ---")
        proposal = ExtractedRequirement(
            requirement_text=version.requirement_text,
            normalized_requirement=version.normalized_requirement,
            category=requirement.category or RequirementCategory.TECHNICAL,
            is_mandatory=bool(requirement.is_mandatory),
            source_block_ordinal=block.ordinal,
            confidence=1.0,
        )
        assignments = _reconcile_against_existing(
            session,
            ctx,
            bid_id=requirement.bid_id,
            proposals=[proposal],
            block_by_ordinal={block.ordinal: (block, None)},
        )
        verdict = assignments.get(id(proposal), "ABSENT")
        if verdict is _ECHO_OF:
            shown = "echo of an earlier proposal in the same answer"
        elif verdict == "ABSENT":
            shown = "no entry -- falls through to _find_existing above"
        elif verdict is None:
            shown = "None -- WOULD CREATE A NEW ROW"
        else:
            shown = verdict.code
        print(f"    -> {shown}")

        print(
            "\n  If either path names a requirement, the code in the tree matches this text\n"
            "  today and the duplicate came from an earlier build. If both decline, the bug\n"
            "  is live and this is the smallest reproduction of it."
        )
        return 0
    finally:
        session.rollback()
        session.close()


def _stored_ref(session, requirement) -> str | None:
    from app.models import RequirementVersion

    return session.execute(
        select(RequirementVersion.clause_ref).where(
            RequirementVersion.requirement_id == requirement.id,
            RequirementVersion.superseded_at.is_(None),
        )
    ).scalars().first()


if __name__ == "__main__":
    sys.exit(main())
