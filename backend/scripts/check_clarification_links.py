"""Why does every requirement now carry a clarification?

The backfill worked in the sense that links exist -- 22 became 1,317 -- and
failed in the sense that matters: all 129 requirements are linked, so a column
that flags "this row has an open question" now flags every row, which tells a
reviewer exactly as much as flagging none. 73 rows read
NEEDS_CLIENT_CLARIFICATION.

Two explanations were on the table before any of this was measured. Either a
finding fans out over a crowded block (block 10 holds 9 requirements, so one
question about that block lands on all 9), or clause-number collision is
matching findings to rows they never cited. They imply different fixes, so this
counts rather than argues.

Nothing is written. Read-only.

    python scripts/check_clarification_links.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
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
from app.models.enums import (  # noqa: E402
    ClarificationStatus,
    RequirementLifecycle,
)

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"


def _short(text: str | None, width: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def main() -> int:
    import uuid

    from app.models import (
        Bid,
        ClarificationIssue,
        ClarificationRequirement,
        DocumentBlock,
        Organization,
        Requirement,
        RequirementVersion,
    )

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        bid = session.execute(
            select(Bid).where(Bid.org_id == org.id, Bid.id == uuid.UUID(BID))
        ).scalar_one_or_none()
        if bid is None:
            print(f"Bid {BID} not visible under this tenant context.")
            return 1

        # --- the population --------------------------------------------------
        issues = (
            session.execute(
                select(ClarificationIssue).where(
                    ClarificationIssue.org_id == org.id,
                    ClarificationIssue.bid_id == bid.id,
                )
            )
            .scalars()
            .all()
        )
        by_id = {issue.id: issue for issue in issues}
        open_issues = [
            issue
            for issue in issues
            if issue.status
            in (ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT)
        ]
        # A restatement stays OPEN on purpose -- "asked seven ways" is worth
        # seeing, and a merge a reviewer disagrees with has to be visible to be
        # argued with. Counting it here made this diagnostic report the
        # pre-dedupe numbers unchanged after 37 questions had been absorbed,
        # which is the same defect as `check_extraction_funnel.py` printing the
        # pre-fix number after the fix landed: a diagnostic that does not know
        # what its subject now does.
        restated = [i for i in open_issues if i.restates_clarification_id is not None]
        open_issues = [i for i in open_issues if i.restates_clarification_id is None]
        print(
            f"clarifications: {len(issues)} total, "
            f"{len(open_issues) + len(restated)} open/sent"
        )
        if restated:
            canonical = {i.restates_clarification_id for i in restated}
            print(
                f"  of those, {len(restated)} are other wordings of {len(canonical)} "
                "question(s) and are excluded from everything below"
            )
        print()

        print("open findings by detector, and how many carry a question:")
        per_detector: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for issue in open_issues:
            row = per_detector[issue.detected_by]
            row[0] += 1
            if (issue.recommended_question or "").strip():
                row[1] += 1
        for detector, (total, asked) in sorted(
            per_detector.items(), key=lambda kv: -kv[1][0]
        ):
            print(f"  {total:5d}  {asked:5d} with a question   {detector}")
        print()

        # --- requirements and their source blocks -----------------------------
        rows = session.execute(
            select(Requirement, RequirementVersion)
            .join(
                RequirementVersion,
                RequirementVersion.id == Requirement.current_version_id,
            )
            .where(
                Requirement.org_id == org.id,
                Requirement.bid_id == bid.id,
                Requirement.lifecycle_state.notin_(
                    [
                        RequirementLifecycle.WITHDRAWN,
                        RequirementLifecycle.SUPERSEDED,
                    ]
                ),
            )
            .order_by(Requirement.code)
        ).all()
        requirements = {req.id: (req, version) for req, version in rows}
        print(f"live requirements: {len(requirements)}")

        # How crowded is each source block? This is the fan-out multiplier if
        # the block-crowding explanation is the right one.
        per_block: dict[tuple, list[str]] = defaultdict(list)
        for req, version in rows:
            locator = version.source_locator or {}
            key = (
                str(version.source_document_version_id),
                locator.get("char_start"),
                locator.get("char_end"),
            )
            per_block[key].append(req.code)
        crowding = Counter(len(codes) for codes in per_block.values())
        print(f"source blocks holding them: {len(per_block)}")
        print("  requirements per block: " + ", ".join(
            f"{n}×{count}" for n, count in sorted(crowding.items())
        ))
        worst = sorted(per_block.items(), key=lambda kv: -len(kv[1]))[:5]
        for key, codes in worst:
            print(f"    {key[1]}-{key[2]}  {len(codes)}: {', '.join(codes[:12])}")
        print()

        # --- the links --------------------------------------------------------
        links = session.execute(
            select(ClarificationRequirement).where(
                ClarificationRequirement.org_id == org.id
            )
        ).scalars().all()
        links = [
            link
            for link in links
            if link.clarification_id in by_id and link.requirement_id in requirements
        ]
        print(f"links touching this bid's live requirements: {len(links)}")

        per_issue: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        per_req: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for link in links:
            per_issue[link.clarification_id].append(link.requirement_id)
            per_req[link.requirement_id].append(link.clarification_id)

        open_ids = {issue.id for issue in open_issues}
        open_per_issue = {i: r for i, r in per_issue.items() if i in open_ids}
        print(f"  from {len(per_issue)} findings ({len(open_per_issue)} of them open)")
        spread = Counter(len(v) for v in open_per_issue.values())
        print("  requirements per open finding: " + ", ".join(
            f"{n}×{count}" for n, count in sorted(spread.items())
        ))
        print()

        # --- is the fan-out exactly the crowding? -----------------------------
        # If every finding links to precisely the requirements of the block it
        # cites, then its link count equals the crowding of that block. Any
        # finding linking to MORE than the blocks it cites can hold is the
        # collision hypothesis, and is what to fix.
        print("does each finding's reach match the blocks it cites?")
        blocks = {
            (str(b.document_version_id), b.char_start, b.char_end): b
            for b in session.execute(
                select(DocumentBlock).where(DocumentBlock.org_id == org.id)
            ).scalars()
        }
        exact = over = under = uncited = 0
        overreaching = []
        for issue in open_issues:
            linked_ids = open_per_issue.get(issue.id)
            if not linked_ids:
                continue
            expected: set[str] = set()
            cited_blocks = 0
            for locator in (issue.source_a, issue.source_b):
                if not isinstance(locator, dict):
                    continue
                key = (
                    str(locator.get("document_version_id")),
                    locator.get("char_start"),
                    locator.get("char_end"),
                )
                if key in blocks:
                    cited_blocks += 1
                expected |= set(per_block.get(key, []))
            if cited_blocks == 0:
                uncited += 1
                continue
            got = {requirements[r][0].code for r in linked_ids}
            if got == expected:
                exact += 1
            elif got - expected:
                over += 1
                overreaching.append((issue, sorted(got - expected)))
            else:
                under += 1
        print(f"  exactly the cited blocks' requirements: {exact}")
        print(f"  reaching beyond them:                   {over}")
        print(f"  reaching fewer:                         {under}")
        print(f"  linked but citing no known block:       {uncited}")
        for issue, extra in overreaching[:5]:
            print(f"    + {issue.detected_by}: {_short(issue.description, 70)}")
            print(f"      extra rows: {', '.join(extra[:12])}")
        print()

        # --- what actually gates ----------------------------------------------
        gating_reqs: set[uuid.UUID] = set()
        gate_counts: Counter = Counter()
        for issue in open_issues:
            if not (issue.recommended_question or "").strip():
                continue
            reach = open_per_issue.get(issue.id, [])
            if not reach:
                continue
            gating_reqs |= set(reach)
            gate_counts[issue.id] = len(reach)
        print(f"requirements carrying at least one linked question: {len(gating_reqs)}")
        print(f"requirements carrying any linked open finding:      "
              f"{len({r for r, v in per_req.items() if set(v) & open_ids})}")
        print()
        print("the widest questions -- each of these gates every row it reaches:")
        for issue_id, count in gate_counts.most_common(12):
            issue = by_id[issue_id]
            codes = sorted(requirements[r][0].code for r in open_per_issue[issue_id])
            print(f"  {count:3d} rows  {issue.detected_by}  [{issue.issue_type.value}]")
            print(f"           Q: {_short(issue.recommended_question, 100)}")
            print(f"           rows: {', '.join(codes[:14])}")
        print()

        # --- duplication --------------------------------------------------------
        # 959 findings on a 47-block document is itself a number worth a look:
        # if the same question is stored many times under different
        # fingerprints, every copy links, and the row count multiplies.
        questions = Counter(
            " ".join((issue.recommended_question or "").split()).lower()
            for issue in open_issues
            if (issue.recommended_question or "").strip()
        )
        repeated = [(q, n) for q, n in questions.items() if n > 1]
        print(f"distinct open questions: {len(questions)}; "
              f"asked more than once: {len(repeated)}")
        for question, n in sorted(repeated, key=lambda kv: -kv[1])[:8]:
            print(f"  ×{n:3d}  {_short(question, 96)}")
        print()

        # --- rows with the most ------------------------------------------------
        print("rows carrying the most open findings:")
        busiest = sorted(
            ((r, [i for i in v if i in open_ids]) for r, v in per_req.items()),
            key=lambda kv: -len(kv[1]),
        )[:8]
        for req_id, issue_ids in busiest:
            req, _version = requirements[req_id]
            asked = sum(
                1 for i in issue_ids if (by_id[i].recommended_question or "").strip()
            )
            print(f"  {req.code}: {len(issue_ids)} open, {asked} with a question")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
