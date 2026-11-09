"""Did retiring the old runs lose a question nobody is asking any more?

Closing what the latest run did not repeat took the open ambiguity findings from
322 to 24, and the distinct questions from 121 to 12. Most of that is rewording
-- six of the twelve widest questions were two questions -- but 121 to 12 is a
tenfold cut, and rewording does not explain tenfold. The prompt caps the model at
12 issues per batch, "most serious first", so the 121 was the union of every run
ever made and the 12 is one pass's opinion. Those are different things.

The risk is specific and one-directional: a real question raised by an earlier
pass, not repeated by this one, is now CLOSED and invisible, with nothing live
covering it. That is the unrecoverable direction, so it gets measured rather than
assumed.

Every closed question is matched against the live ones on content words. A closed
question with a close live counterpart is a reworded duplicate and is exactly
what the change was for. One with no counterpart is a question this bid has
stopped asking, and the only thing that decides whether that is acceptable is
reading it.

Orphans are then clustered against each other, because five phrasings of one
lost question are one lost question, and a list of 130 lines is a list nobody
reads.

Nothing is written. Read-only.

    python scripts/check_retired_questions.py
    python scripts/check_retired_questions.py --near 0.6   # stricter matching
"""

from __future__ import annotations

import argparse
import os
import re
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
from app.gateway.registry import get as get_call_type  # noqa: E402
from app.models.enums import ClarificationStatus, RequirementLifecycle  # noqa: E402

BID = "0e20677e-9cc3-434e-ad88-c953d6676028"

# Words that carry no subject matter. Without this, two questions match on
# "what", "the", "required" and every pair looks related.
STOP = {
    "what", "which", "when", "where", "how", "who", "whom", "why", "is", "are",
    "was", "were", "be", "been", "will", "would", "shall", "should", "must",
    "may", "can", "could", "do", "does", "did", "the", "a", "an", "and", "or",
    "of", "to", "in", "on", "for", "with", "by", "at", "from", "as", "that",
    "this", "these", "those", "it", "its", "please", "clarify", "confirm",
    "provide", "specify", "define", "required", "require", "requirements",
    "requirement", "any", "all", "not", "if", "there", "their", "you", "your",
    "we", "our", "us", "apply", "applies", "applied", "used", "use",
}


def _terms(text: str | None) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9.\-/]*", (text or "").lower())
    return {w.strip(".-/") for w in words if len(w) > 2 and w not in STOP}


def _overlap(a: set[str], b: set[str]) -> float:
    """Share of the smaller question's terms present in the other.

    Containment rather than Jaccard: a short question fully covered by a longer
    one is covered, and Jaccard would punish it for the length difference.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _wrap(text: str, indent: str = "        ", width: int = 100) -> str:
    return textwrap.fill(" ".join((text or "").split()), width=width,
                         initial_indent=indent, subsequent_indent=indent)


def main() -> int:
    import uuid

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--near",
        type=float,
        default=0.5,
        help="a closed question sharing this share of its terms with a live one "
             "counts as covered (default 0.5)",
    )
    args = parser.parse_args()

    from app.models import (
        ClarificationIssue,
        DocumentBlock,
        Organization,
        Requirement,
        RequirementVersion,
    )

    call_type = get_call_type("ambiguity_analysis")
    detector = f"llm:{call_type.name}@{call_type.prompt_version}"

    session = get_session_factory()()
    try:
        org = session.execute(select(Organization)).scalars().first()
        if org is None:
            print("No organization.")
            return 1
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)
        bid_id = uuid.UUID(BID)

        issues = (
            session.execute(
                select(ClarificationIssue).where(
                    ClarificationIssue.org_id == org.id,
                    ClarificationIssue.bid_id == bid_id,
                    ClarificationIssue.detected_by == detector,
                )
            )
            .scalars()
            .all()
        )
        live = [
            i for i in issues
            if i.status in (ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT)
            and (i.recommended_question or "").strip()
        ]
        closed = [
            i for i in issues
            if i.status is ClarificationStatus.CLOSED
            and (i.recommended_question or "").strip()
        ]
        print(f"detector {detector}")
        print(f"  live questions:   {len(live)}")
        print(f"  closed questions: {len(closed)}")
        print(f"  a closed question counts as covered at {args.near:.0%} term overlap\n")

        live_terms = [(i, _terms(i.recommended_question)) for i in live]

        covered = 0
        orphans = []
        for issue in closed:
            terms = _terms(issue.recommended_question)
            best, score = None, 0.0
            for other, other_terms in live_terms:
                value = _overlap(terms, other_terms)
                if value > score:
                    best, score = other, value
            if score >= args.near:
                covered += 1
            else:
                orphans.append((issue, terms, best, score))

        print(f"{covered} closed question(s) are covered by a live one -- rewordings, "
              "which is what the change was for.")
        print(f"{len(orphans)} have no live counterpart.\n")
        if not orphans:
            print("Nothing was lost. The retirement was pure de-duplication.")
            return 0

        # Cluster the orphans among themselves: five phrasings of one lost
        # question are one lost question.
        clusters: list[list[tuple]] = []
        for entry in orphans:
            for cluster in clusters:
                if _overlap(entry[1], cluster[0][1]) >= args.near:
                    cluster.append(entry)
                    break
            else:
                clusters.append([entry])
        clusters.sort(key=len, reverse=True)
        print(f"they are {len(clusters)} distinct question(s). Longest first.\n")

        # Which rows each cited block maps to, so a lost question can be judged
        # against the obligations it concerned.
        blocks = {
            (b.document_version_id, b.char_start, b.char_end): b
            for b in session.execute(
                select(DocumentBlock).where(DocumentBlock.org_id == org.id)
            ).scalars()
        }
        rows = session.execute(
            select(Requirement, RequirementVersion)
            .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
            .where(
                Requirement.org_id == org.id,
                Requirement.bid_id == bid_id,
                Requirement.lifecycle_state.notin_(
                    [RequirementLifecycle.WITHDRAWN, RequirementLifecycle.SUPERSEDED]
                ),
            )
        ).all()
        per_block: dict[tuple, list[str]] = {}
        for req, version in rows:
            locator = version.source_locator or {}
            per_block.setdefault(
                (version.source_document_version_id,
                 locator.get("char_start"),
                 locator.get("char_end")),
                [],
            ).append(req.code)

        for index, cluster in enumerate(clusters, start=1):
            issue, _terms_, best, score = cluster[0]
            print("=" * 100)
            print(f"[{index}] asked {len(cluster)} time(s), no live counterpart "
                  f"(closest live question: {score:.0%})")
            print(f"    type {issue.issue_type.value}, severity {issue.severity.value}")
            print("    question:")
            print(_wrap(issue.recommended_question))
            locator = issue.source_a if isinstance(issue.source_a, dict) else {}
            version_id = locator.get("document_version_id")
            if isinstance(version_id, str):
                try:
                    version_id = uuid.UUID(version_id)
                except ValueError:
                    version_id = None
            key = (version_id, locator.get("char_start"), locator.get("char_end"))
            codes = per_block.get(key)
            block = blocks.get(key)
            if codes:
                print(f"    concerned: {', '.join(sorted(codes))}")
            if block is not None and block.heading_path:
                print(f"    section:   {' > '.join(block.heading_path)}")
            if best is not None:
                print("    nearest live question:")
                print(_wrap(best.recommended_question, indent="        | "))
        print("=" * 100)
        print(
            f"\n{len(clusters)} question(s) the bid has stopped asking. Read them: if they are\n"
            "things a customer should still answer, the per-batch cap of 12 issues is too\n"
            "tight for this document and the fix is smaller batches, not a looser rule."
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
