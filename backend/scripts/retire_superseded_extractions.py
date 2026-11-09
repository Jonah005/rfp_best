"""Requirements a newer prompt version has already re-read, listed for review.

`detected_by` carries a detector's prompt version, and when that version moves,
`close_superseded_detector_versions` retires what the old version said --
because those findings answered a question the system has stopped asking.
Requirements carry the same stamp in `extracted_by` and have no equivalent, so a
prompt bump leaves both answers in the matrix at once.

That is not a tidiness problem either. Identity is judged on the clause, and a
v4 paraphrase scores about 0.35 against a v5 quotation of the same obligation --
far below any matching threshold, and rightly so, since at 0.35 the same rule
would merge unrelated clauses. So reconciliation CANNOT match across a prompt
bump, and every improvement to the prompt duplicates the rows it re-reads. Block
26 of the live bid ended up holding eight requirements for three bullets: one
faithful quotation and seven earlier inventions.

The rule applied here is narrow, and deliberately so:

    a requirement is superseded only if a NEWER prompt version has decomposed
    THE SAME BLOCK

If the newest prompt version never read that block -- it was skipped, or the
block stopped being eligible -- nothing is superseded, because nothing has
replaced it. An obligation must never leave the bid merely because a prompt
changed elsewhere.

Nothing is withdrawn without being named. The dry run prints, per block, what
the newest version produced and what the older versions left behind, with each
clause's word-coverage against the block so the two can be compared on the
evidence rather than on their version stamps. Withdrawing is a second command,
and it withdraws only what the first one listed.

    python scripts/retire_superseded_extractions.py                  # review
    python scripts/retire_superseded_extractions.py --apply          # withdraw them
    python scripts/retire_superseded_extractions.py --keep R-024,R-026

`--keep` carries codes through the sweep untouched: the newest prompt sometimes
misses something an older one caught, and reading the two side by side is the
only way to notice. Nothing is deleted in any case -- a withdrawn requirement
keeps its versions, assessments and audit trail, and simply stops appearing.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
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
from app.gateway.registry import get as get_call_type  # noqa: E402
from app.models.enums import RequirementLifecycle  # noqa: E402
from app.services import requirements as requirement_service  # noqa: E402

REASON = "Withdrawn: a newer prompt version re-read this block and replaced it."


def _coverage(clause: str, block_text: str) -> float:
    words = [w for w in clause.lower().split() if len(w) > 3]
    if not words:
        return 1.0
    haystack = block_text.lower()
    return sum(1 for word in words if word.strip(".,;:()") in haystack) / len(words)


def _listed(flag: str) -> set[str]:
    for index, value in enumerate(sys.argv):
        if value == flag and index + 1 < len(sys.argv):
            return {c.strip().upper() for c in sys.argv[index + 1].split(",") if c.strip()}
        if value.startswith(f"{flag}="):
            return {c.strip().upper() for c in value.split("=", 1)[1].split(",") if c.strip()}
    return set()


def _move_clarification_links(session, org_id, *, source, targets: list) -> int:
    """DEPRECATED. Use `app.services.requirements.move_clarification_links`.

    Kept only because `repair_withdrawn_links.py` imports it from here, which
    is itself the problem: a domain rule ended up owned by a maintenance
    script and reachable only by importing one script from another. The
    logic now lives in the service, `withdraw` calls it, and this forwards
    so the existing importer keeps working. Delete both when that script is
    next touched.
    """
    from app.core.context import ExecutionContext as _Ctx
    from app.services.requirements import move_clarification_links as _moved

    return _moved(session, _Ctx(org_id=org_id, actor_kind="SYSTEM"), source=source, targets=targets)


def sweep_verdict(
    *, read_by_current: bool, stamps: set, current: str, produced: int, current_rows_here: int
) -> str:
    """Should this block's older requirements be retired, skipped, or ignored?

    Extracted as a pure function because the test that protects it used to read
    this file's SOURCE and assert that one string appeared at a lower character
    offset than another. That passed only while the code was laid out a
    particular way: moving the withdrawal into `requirements.withdraw` -- which
    changed nothing about the decision -- broke it instantly, and a rearrangement
    that broke the actual rule while leaving both strings in place would not
    have. It is the third source-grepping test this project has had to convert.

    Three answers, and the middle one is the whole point:

      UNREAD    the current extractor never looked at this block, so nothing
                here is superseded by it. A block with no record and no current
                row was not read.
      SKIP      it DID look, produced proposals, and none of them became a new
                row -- so they were absorbed by rows that already said the same
                thing. Those rows are confirmed, not superseded. This is
                agreement, not supersession, and retiring on it would delete
                requirements for being correct.
      RETIRE    older stamps remain and the interlock does not apply.
    """
    if not read_by_current and current not in stamps:
        return "UNREAD"
    if not (stamps - {current}):
        return "UNREAD"
    if produced and not current_rows_here:
        return "SKIP"
    return "RETIRE"


def main() -> int:
    apply_changes = "--apply" in sys.argv
    keep = _listed("--keep")

    from app.models import (
        Bid,
        BlockExtraction,
        DocumentBlock,
        Organization,
        Requirement,
        RequirementVersion,
    )

    call_type = get_call_type("requirement_decomposition")
    current = f"llm:{call_type.name}@{call_type.prompt_version}"
    print(f"current extractor: {current}\n")

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        bids = (
            session.execute(select(Bid).where(Bid.org_id == org.id).order_by(Bid.created_at))
            .scalars()
            .all()
        )

        withdrawn = 0
        kept_back = 0
        relinked = 0
        for bid in bids:
            rows = session.execute(
                select(
                    Requirement,
                    RequirementVersion.requirement_text,
                    RequirementVersion.source_document_version_id,
                    RequirementVersion.source_locator,
                    RequirementVersion.extracted_by,
                )
                .join(RequirementVersion, RequirementVersion.requirement_id == Requirement.id)
                .where(
                    Requirement.org_id == org.id,
                    Requirement.bid_id == bid.id,
                    Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
                    RequirementVersion.superseded_at.is_(None),
                )
            ).all()
            if not rows:
                continue

            by_block: dict[tuple, list] = defaultdict(list)
            for requirement, text, version_id, locator, extracted_by in rows:
                locator = locator if isinstance(locator, dict) else {}
                key = (version_id, locator.get("char_start"), locator.get("char_end"))
                by_block[key].append((requirement, text or "", extracted_by or ""))

            # Blocks the current extractor actually READ, from the record of the
            # reading rather than from its yield.
            #
            # The first version of this script inferred it -- "the current
            # version produced a row on this block" -- and that inference has a
            # blind spot precisely where the new prompt does its best work. v6
            # read the executive summary, applied its obligation test and rightly
            # raised nothing; with no v6 row to point at, the four v5 rows of
            # prose it had just judged not to be requirements were treated as
            # untouched and protected. `BlockExtraction` records the pass, so
            # "read it and found nothing" is now a fact this can act on.
            #
            # `found` is carried too, and it is a safety interlock rather than a
            # statistic. A block where the current prompt PRODUCED proposals but
            # where no requirement carries its stamp is a contradiction: the
            # proposals went somewhere. They were matched to existing rows whose
            # wording did not change -- agreement, not absence. A sweep that
            # reads that as "found nothing" withdraws the very rows the new
            # prompt confirmed, which is what this script did on its first run
            # against v6: it proposed retiring the B2B reconciliation
            # obligation, the Windows 2012 deployment requirement and the NDA
            # notification, all at 100% coverage.
            read_by_current: dict[tuple, int] = {
                (version_id, char_start, char_end): found
                for version_id, char_start, char_end, found in session.execute(
                    select(
                        BlockExtraction.document_version_id,
                        BlockExtraction.char_start,
                        BlockExtraction.char_end,
                        BlockExtraction.found,
                    ).where(
                        BlockExtraction.org_id == org.id,
                        BlockExtraction.bid_id == bid.id,
                        BlockExtraction.extracted_by == current,
                    )
                ).all()
            }

            reported = False
            for key, entries in sorted(by_block.items(), key=lambda item: str(item[0])):
                stamps = {stamp for _, _, stamp in entries}
                # The test is "the current extractor has read this block", not
                # "it is older than current" -- otherwise a block the newest
                # prompt never saw would lose its requirements to a version that
                # never replaced them. A block with no record and no current row
                # was not read, so nothing there is superseded.
                current_rows_here = [e for e in entries if e[2] == current]
                produced = read_by_current.get(key, 0)
                verdict = sweep_verdict(
                    read_by_current=key in read_by_current,
                    stamps=stamps,
                    current=current,
                    produced=produced,
                    current_rows_here=len(current_rows_here),
                )
                if verdict == "UNREAD":
                    continue
                if verdict == "SKIP":
                    # The interlock. Proposals were made here and none of them
                    # became a row carrying this stamp, so they were absorbed by
                    # rows that already said the same thing. Those rows are
                    # confirmed, not superseded. Reported, and skipped.
                    codes = ", ".join(r.code for r, _, _ in entries)
                    version_id, char_start, char_end = key
                    print(
                        f"\n  block chars {char_start}-{char_end}"
                        f"\n    SKIPPED -- {current} produced {produced} proposal(s) here and "
                        "none became a\n      new row, so they matched the existing "
                        f"requirements unchanged: {codes}.\n      Agreement, not supersession. "
                        "Nothing to retire."
                    )
                    continue

                version_id, char_start, char_end = key
                block = (
                    session.execute(
                        select(DocumentBlock).where(
                            DocumentBlock.org_id == org.id,
                            DocumentBlock.document_version_id == version_id,
                            DocumentBlock.char_start == char_start,
                            DocumentBlock.char_end == char_end,
                        )
                    )
                    .scalars()
                    .first()
                )
                block_text = (block.text if block else "") or ""

                if not reported:
                    print("#" * 78)
                    print(f"bid {str(bid.id)[:8]}   {len(rows)} live requirements")
                    reported = True

                print(f"\n  block chars {char_start}-{char_end}")
                current_rows = [e for e in entries if e[2] == current]
                stale_rows = [e for e in entries if e[2] != current]

                if current_rows:
                    print(f"    KEPT -- produced by {current}:")
                    for requirement, clause, _s in sorted(current_rows, key=lambda e: e[0].code):
                        covered = _coverage(clause, block_text)
                        print(
                            f"      {requirement.code}  coverage {covered:.0%}"
                            f"  {' '.join(clause.split())[:110]}"
                        )
                else:
                    # Read, and deliberately empty. Worth stating plainly,
                    # because withdrawing every row on a block leaves it with
                    # none -- which is the right answer when the block turns out
                    # to hold no obligation, and an alarming one to meet without
                    # explanation.
                    print(
                        f"    KEPT -- none. {current} read this block and raised no\n"
                        "      requirements, so everything below is left over from an earlier\n"
                        "      prompt. Read them: if they are real obligations, the newer prompt\n"
                        "      is wrong and they should be carried through with --keep."
                    )

                print("    SUPERSEDED -- an earlier prompt version read the same block:")
                for requirement, clause, stamp in sorted(stale_rows, key=lambda e: e[0].code):
                    held = requirement.code in keep
                    mark = "  [KEPT BY --keep]" if held else ""
                    print(
                        f"      {requirement.code}  {stamp.split('@')[-1]}  "
                        f"coverage {_coverage(clause, block_text):.0%}{mark}"
                    )
                    print(f"        {' '.join(clause.split())[:110]}")
                    if held:
                        kept_back += 1
                        continue
                    if apply_changes:
                        # One call, because withdrawing is three facts.
                        #
                        # This used to set the lifecycle here and move the
                        # links beside it, and `merge_duplicate_requirements.py`
                        # -- which does the same job -- moved no links at all
                        # and recorded no successor. The assessment was left
                        # claiming to be current in every one of them, which
                        # is how the live bid came to hold 158 current
                        # assessments for 89 live requirements.
                        successors = [e[0] for e in current_rows]
                        moved = requirement_service.withdraw(
                            session,
                            ctx,
                            requirement=requirement,
                            successors=[s.id for s in successors],
                            reason="Superseded by a later extraction of the same clause.",
                        )
                        relinked += moved["clarification_links_moved"]
                        withdrawn += 1

            if reported:
                print()

        if not apply_changes:
            print("#" * 78)
            print(
                "Dry run. Nothing was changed.\n"
                "Read the two lists above against each other before applying -- the newest\n"
                "prompt is not automatically the better reader, and an obligation only the\n"
                "older one caught should be carried through with --keep."
            )
            return 0

        session.commit()
        print("#" * 78)
        print(
            f"Withdrew {withdrawn} requirement(s)"
            + (f", kept {kept_back} named on --keep" if kept_back else "")
            + (f", moved {relinked} clarification link(s) onto the survivors" if relinked else "")
            + ".\nNothing was deleted -- each keeps its versions, assessments and audit trail,\n"
            "and simply stops appearing. Each records what replaced it, and its\n"
            "clarifications followed the clause rather than being left pointing at a row\n"
            "that no longer exists.\n\n"
            "Re-run the assessment so the matrix is recomputed over the remaining set."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
