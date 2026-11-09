"""Clarification issues: defects in the tender worth asking the customer about.

Findings are fingerprinted so that re-running analysis updates an existing issue
rather than piling up duplicates — an issue list that grows on every re-analysis
is one nobody reads.

An issue that a human has already sent to the client or answered is never
silently reopened by a later automatic run.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.models import ClarificationIssue, ClarificationRequirement, Department, Requirement, User
from app.models.enums import (
    ClarificationIssueType,
    ClarificationSeverity,
    ClarificationStatus,
)
from app.services import audit

# Statuses a human has moved on from. Automatic re-analysis leaves these alone.
HUMAN_OWNED_STATUSES = frozenset(
    {
        ClarificationStatus.SENT_TO_CLIENT,
        ClarificationStatus.ANSWERED,
        ClarificationStatus.CLOSED,
        ClarificationStatus.WITHDRAWN,
    }
)

BLOCKING_SEVERITIES = frozenset({ClarificationSeverity.BLOCKING, ClarificationSeverity.MAJOR})


def raise_issue(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    issue_type: ClarificationIssueType,
    description: str,
    severity: ClarificationSeverity,
    fingerprint: str,
    detected_by: str,
    blocks_generation: bool = False,
    recommended_question: str | None = None,
    source_a: dict | None = None,
    source_b: dict | None = None,
    requirement_ids: list[uuid.UUID] | None = None,
) -> ClarificationIssue:
    existing = session.execute(
        select(ClarificationIssue).where(
            ClarificationIssue.org_id == ctx.org_id,
            ClarificationIssue.bid_id == bid_id,
            ClarificationIssue.fingerprint == fingerprint,
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.status in HUMAN_OWNED_STATUSES:
            # Already in a human's hands. Re-detecting it is not news.
            return existing
        existing.description = description
        existing.severity = severity
        existing.blocks_generation = blocks_generation
        existing.recommended_question = recommended_question
        existing.source_a = source_a
        existing.source_b = source_b
        session.flush()
        attach_requirements(session, ctx, existing, requirement_ids)
        return existing

    issue = ClarificationIssue(
        org_id=ctx.org_id,
        bid_id=bid_id,
        issue_type=issue_type,
        description=description,
        severity=severity,
        blocks_generation=blocks_generation,
        recommended_question=recommended_question,
        source_a=source_a,
        source_b=source_b,
        status=ClarificationStatus.OPEN,
        detected_by=detected_by,
        fingerprint=fingerprint,
    )
    session.add(issue)
    session.flush()
    attach_requirements(session, ctx, issue, requirement_ids)
    route_to_department(session, ctx, issue)

    audit.record(
        session,
        ctx,
        action="clarification.raised",
        entity_type="clarification_issue",
        entity_id=issue.id,
        bid_id=bid_id,
        after={
            "issue_type": issue_type.value,
            "severity": severity.value,
            "blocks_generation": blocks_generation,
        },
    )
    return issue


def attach_requirements(
    session: Session,
    ctx: ExecutionContext,
    issue: ClarificationIssue,
    requirement_ids: list[uuid.UUID] | None,
) -> int:
    """Link an issue to the requirements it concerns. Returns links CREATED.

    The count is the number actually written, not the number offered, because
    the caller's only other option is to infer it -- and a backfill that reports
    what it was asked to do rather than what it did is how "cleared 26 rows"
    came to sit above a run that deleted none.
    """
    created = 0
    for requirement_id in requirement_ids or []:
        exists = session.execute(
            select(ClarificationRequirement).where(
                ClarificationRequirement.org_id == ctx.org_id,
                ClarificationRequirement.clarification_id == issue.id,
                ClarificationRequirement.requirement_id == requirement_id,
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                ClarificationRequirement(
                    org_id=ctx.org_id,
                    clarification_id=issue.id,
                    requirement_id=requirement_id,
                )
            )
            created += 1
    session.flush()
    return created


def update_status(
    session: Session,
    ctx: ExecutionContext,
    *,
    issue_id: uuid.UUID,
    status: ClarificationStatus,
    client_response: str | None = None,
    reason: str | None = None,
) -> ClarificationIssue:
    issue = session.get(ClarificationIssue, issue_id)
    if issue is None or issue.org_id != ctx.org_id:
        raise NotFound("Clarification issue not found.")
    if status is ClarificationStatus.ANSWERED and not (client_response or "").strip():
        raise ValidationError("Recording an assumption requires its wording.")

    before = issue.status.value
    issue.status = status
    if client_response is not None:
        issue.client_response = client_response
        issue.answered_at = datetime.now(timezone.utc)
    if status in (ClarificationStatus.ANSWERED, ClarificationStatus.CLOSED):
        issue.blocks_generation = False
    session.flush()
    if status in (ClarificationStatus.ANSWERED, ClarificationStatus.CLOSED):
        # Answering a question can lift the scope-confidence gate; regrade it now rather than
        # leaving drafting blocked until the whole tender is analysed again.
        try:
            from app.services.tender_analysis import (  # noqa: PLC0415 - avoids a cycle
                compute_scope_confidence,
                get_scope_confidence,
            )

            if get_scope_confidence(session, ctx, bid_id=issue.bid_id) is not None:
                compute_scope_confidence(session, ctx, bid_id=issue.bid_id)
        except Exception:  # noqa: BLE001, S110 - regrading must never fail the status change
            pass

    audit.record(
        session,
        ctx,
        action="clarification.status_changed",
        entity_type="clarification_issue",
        entity_id=issue.id,
        bid_id=issue.bid_id,
        before={"status": before},
        after={"status": status.value},
        reason=reason,
    )
    return issue


def list_issues(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    severity: ClarificationSeverity | None = None,
    status: ClarificationStatus | None = None,
) -> list[ClarificationIssue]:
    stmt = select(ClarificationIssue).where(
        ClarificationIssue.org_id == ctx.org_id, ClarificationIssue.bid_id == bid_id
    )
    if severity is not None:
        stmt = stmt.where(ClarificationIssue.severity == severity)
    if status is not None:
        stmt = stmt.where(ClarificationIssue.status == status)
    return list(session.execute(stmt.order_by(ClarificationIssue.created_at)).scalars().all())


def counts(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> dict[str, int]:
    """Open-issue counts, used by the Scope Confidence gate."""
    rows = session.execute(
        select(ClarificationIssue.severity, func.count())
        .where(
            ClarificationIssue.org_id == ctx.org_id,
            ClarificationIssue.bid_id == bid_id,
            ClarificationIssue.status.in_(
                [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
            ),
        )
        .group_by(ClarificationIssue.severity)
    ).all()
    by_severity = {severity.value: count for severity, count in rows}
    blocking = session.execute(
        select(func.count())
        .select_from(ClarificationIssue)
        .where(
            ClarificationIssue.org_id == ctx.org_id,
            ClarificationIssue.bid_id == bid_id,
            ClarificationIssue.blocks_generation.is_(True),
            ClarificationIssue.status.in_(
                [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
            ),
        )
    ).scalar_one()
    return {
        "open_total": sum(by_severity.values()),
        "blocking": blocking,
        **{f"open_{k.lower()}": v for k, v in by_severity.items()},
    }


def close_superseded_detector_versions(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    detector: str,
    current: str,
    reason: str,
) -> int:
    """Close issues an EARLIER VERSION of this detector raised.

    `detected_by` is "llm:<name>@<prompt_version>", so a prompt change makes the
    new run's issues land under a new string and quietly orphans the old ones.
    They stay OPEN for ever, and the ones that were BLOCKING keep halting
    generation on a judgement the system no longer makes.

    That is exactly what happened. `ambiguity_analysis` moved to v2 after its v1
    prompt was found to invite the model to report anything outside its batch as
    missing -- v2 raised twenty-four issues and not one blocker, while all eight
    of the v1 blockers remained open, still claiming the tender had no technical
    requirements and no evaluation criteria. The fix looked ineffective because
    the evidence of the old behaviour outlived it.

    Closed rather than deleted: the run happened, someone may have read those
    questions, and the audit trail should say the detector was superseded rather
    than pretend it never spoke.
    """
    issues = list(
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid_id,
                ClarificationIssue.detected_by.like(f"{detector}@%"),
                ClarificationIssue.detected_by != current,
                ClarificationIssue.status.in_(
                    [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
                ),
            )
        ).scalars()
    )
    if not issues:
        return 0

    ids = [issue.id for issue in issues]
    session.execute(
        delete(ClarificationRequirement).where(
            ClarificationRequirement.org_id == ctx.org_id,
            ClarificationRequirement.clarification_id.in_(ids),
        )
    )
    superseded = sorted({issue.detected_by for issue in issues})
    for issue in issues:
        issue.status = ClarificationStatus.CLOSED
        issue.resolution_note = reason
    session.flush()

    audit.record(
        session,
        ctx,
        action="clarification.detector_version_superseded",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        after={"superseded": superseded, "current": current, "closed": len(ids)},
        reason=reason,
    )
    return len(ids)


def close_resolved_coverage_gaps(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    detected_by: str,
    still_open: set[str],
    reason: str,
    notes: dict[str, str] | None = None,
) -> int:
    """Close coverage gaps for clauses that are no longer gaps.

    "Clause 14 governs this tender but produced neither a requirement nor an
    explicit decision that it imposes none" -- linked to R-014, R-143 and R-144,
    all produced from clause 14. 43 of the live bid's 47 coverage findings
    contradicted their own links like this, because the check compared against
    what the CURRENT RUN processed and a rerun skips every block already
    decomposed.

    Closing on absence is the rule that destroyed 44 real questions when it was
    applied to the ambiguity model, so the difference matters: this detector is
    deterministic and visits every block of the document on every run. There is
    no per-batch cap and no "most serious first", so "not raised this time"
    genuinely means "no longer a gap" rather than "ran out of room".

    Links go with the closure. Nine of these findings were the only thing still
    pointing at requirements withdrawn by a merge, which is how 25 dangling
    codes reached an export.
    """
    issues = list(
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid_id,
                ClarificationIssue.detected_by == detected_by,
                ClarificationIssue.status == ClarificationStatus.OPEN,
            )
        ).scalars()
    )
    stale = [issue for issue in issues if issue.fingerprint not in still_open]
    if not stale:
        return 0

    ids = [issue.id for issue in stale]
    session.execute(
        delete(ClarificationRequirement).where(
            ClarificationRequirement.org_id == ctx.org_id,
            ClarificationRequirement.clarification_id.in_(ids),
        )
    )
    for issue in stale:
        issue.status = ClarificationStatus.CLOSED
        # Say what accounted for the clause, per finding. Closing 47 of these
        # with one audit line left a reader unable to tell a resolved gap from
        # a dropped one.
        issue.resolution_note = (notes or {}).get(issue.fingerprint) or reason
    session.flush()

    audit.record(
        session,
        ctx,
        action="clarification.coverage_gap_resolved",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        after={"detected_by": detected_by, "closed": len(ids), "still_open": len(still_open)},
        reason=reason,
    )
    return len(ids)


def close_findings_this_run_did_not_repeat(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    detected_by: str,
    seen: set[str],
    covered: set[tuple],
    reason: str,
) -> int:
    """Close what the latest run of this detector no longer says.

    `close_superseded_detector_versions` retires a detector's output when the
    PROMPT changes, on the argument that "the old run's answers are not evidence
    about the document; they are evidence about a question the system has
    stopped asking". That argument does not depend on the prompt changing. It is
    equally true of the previous RUN of the same prompt, and not applying it
    there is what filled the live bid.

    322 open ambiguity findings on a 43-block document, and the wording is why.
    A finding's fingerprint hashes the model's own description, so the same
    clause read in a different batch window is described differently,
    `uq_clarification_fingerprint` never fires, and every run mints fresh rows
    beside the old ones. Six of the twelve widest questions were two questions:

        What is the required scope, format and duration of the source code and
            development knowledge transfer ...
        What is the required scope, format and acceptance criteria for the
            source code and development knowledge ...
        What are the required IP/source ownership terms and the expected format
            and duration of the knowledge ...

    Fingerprinting on something more stable than prose was the other candidate
    and is the wrong fix: the stable part is (type, block), and block 46
    genuinely carried two different UNDEFINED_FIELD questions -- what "final
    release" means, and how proposals are weighted. Collapsing on that key would
    have silently dropped one of them, which is the direction this codebase does
    not go.

    So the rule is about currency, not identity. A run that read a block is
    authoritative about that block: whatever it did not repeat, it no longer
    says.

    `covered` is what keeps that honest -- only findings whose cited block this
    run actually read are eligible. A run that stops early, or one scoped to
    part of the document, must not close findings about pages it never opened.
    Without that this is the same silent-skip shape that has produced three
    separate bugs here: a guard that looks like housekeeping and is really data
    loss.

    Anything in a person's hands (SENT_TO_CLIENT, ANSWERED, CLOSED, WITHDRAWN)
    is left alone, as everywhere else. Closed, not deleted, and the links go
    with it so nothing counts a closed question against a live row.
    """
    candidates = list(
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid_id,
                ClarificationIssue.detected_by == detected_by,
                ClarificationIssue.status == ClarificationStatus.OPEN,
            )
        ).scalars()
    )
    stale = []
    for issue in candidates:
        if issue.fingerprint in seen:
            continue
        # A blocker is never retired by silence.
        #
        # This pass reads "the latest run did not say it" as "the run no longer
        # says it", which is right for an ordinary finding and wrong for one
        # that STOPS GENERATION. The model rewords itself between runs, so a
        # blocker survives only as long as it happens to be described the same
        # way -- and on the live bid the BLOCKING submission-deadline question
        # vanished exactly like that, leaving a MAJOR reworded twin and a bid
        # with no known deadline that no longer blocked anything.
        #
        # A blocker is a governance state. It ends when the customer answers
        # it or a person closes it, not because a later call phrased the same
        # doubt differently. If the finding really is obsolete, the dedupe pass
        # folds it into whatever replaced it, which keeps the severity.
        if issue.blocks_generation:
            continue
        locator = issue.source_a
        if not isinstance(locator, dict):
            # No cited block, so nothing to check coverage against. Leaving it
            # is the recoverable direction.
            continue
        version_id = locator.get("document_version_id")
        if isinstance(version_id, str):
            try:
                version_id = uuid.UUID(version_id)
            except ValueError:
                continue
        if (version_id, locator.get("char_start"), locator.get("char_end")) in covered:
            stale.append(issue)

    if not stale:
        return 0

    ids = [issue.id for issue in stale]
    session.execute(
        delete(ClarificationRequirement).where(
            ClarificationRequirement.org_id == ctx.org_id,
            ClarificationRequirement.clarification_id.in_(ids),
        )
    )
    for issue in stale:
        issue.status = ClarificationStatus.CLOSED
        issue.resolution_note = reason
    session.flush()

    audit.record(
        session,
        ctx,
        action="clarification.superseded_by_rerun",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        after={
            "detected_by": detected_by,
            "closed": len(ids),
            "blocks_read": len(covered),
            "still_raised": len(seen),
        },
        reason=reason,
    )
    return len(ids)


def close_detector_issues(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    detected_by: str,
    reason: str,
) -> int:
    """Close and unlink every open issue a given detector raised.

    For retiring a detector's output when the detector itself was the mistake.
    One run raised an identity-drift note per re-worded requirement and linked
    each to its requirement — and a clarification linked to a requirement makes
    that row report an open question with the CUSTOMER. Fifty-one of fifty-nine
    rows said the bid was waiting on ADX. It was not.

    The link is deleted as well as the status changed: a closed issue that still
    holds its link is a trap for the next reader of the table, and for any query
    that counts links rather than statuses.

    Scoped by detector, so anything a person raised, or the tender analysis
    found, is untouched.
    """
    issues = list(
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid_id,
                ClarificationIssue.detected_by == detected_by,
                ClarificationIssue.status.in_(
                    [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
                ),
            )
        ).scalars()
    )
    if not issues:
        return 0

    ids = [issue.id for issue in issues]
    session.execute(
        delete(ClarificationRequirement).where(
            ClarificationRequirement.org_id == ctx.org_id,
            ClarificationRequirement.clarification_id.in_(ids),
        )
    )
    for issue in issues:
        issue.status = ClarificationStatus.CLOSED
        issue.resolution_note = reason
    session.flush()

    audit.record(
        session,
        ctx,
        action="clarification.detector_retired",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        after={"detected_by": detected_by, "closed": len(ids)},
        reason=reason,
    )
    return len(ids)


# --- whose question it is ----------------------------------------------------
#
# A clarification is routed the way a task is: by the department that answers
# for the subject. Linked requirements carry the system's own routing
# (`recommended_department_id`), so that wins. Without a link the wording
# decides, by counting which department's vocabulary the question uses. A tie
# or no match goes to Bid Management, which answers for deadlines, forms and
# customer correspondence -- the commonest tender defects.

from app.models.enums import DepartmentCode as _D  # noqa: E402

_WORDS: dict[_D, tuple[str, ...]] = {
    _D.LEGAL: (
        "contract",
        "nda",
        "non-disclosure",
        "liability",
        "indemn",
        "licen",
        "intellectual property",
        "ip ",
        "governing law",
        "jurisdiction",
        "terminat",
        "warrant",
        "confidential",
        "penalt",
        "dispute",
        "arbitration",
        "legal",
        "clause conflict",
        "ownership",
    ),
    _D.COMMERCIAL: (
        "price",
        "pricing",
        "payment",
        "invoice",
        "rate",
        "cost",
        "fee",
        "currency",
        "vat",
        "discount",
        "commercial",
        "budget",
        "quotation",
        "bill of quantit",
    ),
    _D.FINANCE: (
        "bank guarantee",
        "bid bond",
        "performance bond",
        "insurance",
        "tax",
        "audited",
        "financial statement",
        "turnover",
        "revenue",
        "credit",
    ),
    _D.HR: ("cv", "resume", "staff", "personnel", "qualification", "experience of", "headcount"),
    _D.SECURITY: (
        "security",
        "penetration",
        "pentest",
        "vulnerab",
        "iso 27001",
        "soc ",
        "threat",
        "red team",
        "encryption",
        "incident",
    ),
    _D.SOLUTION_ARCHITECTURE: (
        "architecture",
        "integration",
        "hosting",
        "infrastructure",
        "interface",
        "api ",
        "platform design",
        "technical solution",
        "scalab",
    ),
    _D.PMO: (
        "schedule",
        "timeline",
        "milestone",
        "project plan",
        "training",
        "delivery method",
        "phase",
        "handover",
        "go-live",
    ),
    _D.SALES: ("reference", "past customer", "previous client", "case stud", "account"),
    _D.QUALITY: ("iso 9001", "quality", "audit", "certification standard"),
    _D.PRODUCT: ("product", "feature", "roadmap", "licensing model", "version"),
    _D.BID_MANAGEMENT: (
        "deadline",
        "submission",
        "submit",
        "due date",
        "time zone",
        "received",
        "format",
        "signature",
        "signed",
        "form",
        "contact person",
        "address",
        "language",
        "copies",
        "evaluation",
        "proposal",
        "clarification",
        "notification",
    ),
}


def _departments_by_code(session: Session, ctx: ExecutionContext) -> dict[_D, Department]:
    rows = session.execute(select(Department).where(Department.org_id == ctx.org_id)).scalars()
    return {d.code: d for d in rows}


def _from_requirements(
    session: Session, ctx: ExecutionContext, issue: ClarificationIssue, by_code: dict
) -> Department | None:
    """The department of the requirements this clarification is linked to, if any.

    Each linked requirement votes for the department recorded on it, or else the
    one that owns its category -- the same table the tasks are grouped by -- and
    the most-voted department wins.
    """
    from app.services import category_routing

    route_table = category_routing.routes(session, ctx)
    linked = session.execute(
        select(Requirement.recommended_department_id, Requirement.category)
        .join(
            ClarificationRequirement,
            (ClarificationRequirement.requirement_id == Requirement.id)
            & (ClarificationRequirement.org_id == Requirement.org_id),
        )
        .where(
            ClarificationRequirement.org_id == ctx.org_id,
            ClarificationRequirement.clarification_id == issue.id,
        )
    ).all()
    votes: Counter = Counter()
    for recommended_id, category in linked:
        if recommended_id is not None:
            votes[recommended_id] += 1
            continue
        code = route_table.get(category)
        if code in by_code:
            votes[by_code[code].id] += 1
    if not votes:
        return None
    wanted = votes.most_common(1)[0][0]
    return next((d for d in by_code.values() if d.id == wanted), None)


def _from_words(issue: ClarificationIssue, by_code: dict) -> Department:
    """Last resort: count which department's vocabulary the question uses."""
    text = " ".join([issue.description or "", issue.recommended_question or ""]).lower()
    text = re.sub(r"\s+", " ", text) + " "
    scores = {
        code: sum(1 for word in words if word in text)
        for code, words in _WORDS.items()
        if code in by_code
    }
    best = max(scores.values(), default=0)
    if best > 0:
        winners = [c for c, n in scores.items() if n == best]
        pick = (
            _D.BID_MANAGEMENT
            if _D.BID_MANAGEMENT in winners
            else sorted(winners, key=lambda c: c.value)[0]
        )
        return by_code[pick]
    return by_code.get(_D.BID_MANAGEMENT) or next(iter(by_code.values()))


def choose_department(
    session: Session, ctx: ExecutionContext, issue: ClarificationIssue
) -> tuple[Department | None, str]:
    """(department, how it was decided): "category" or "rules"."""
    by_code = _departments_by_code(session, ctx)
    if not by_code:
        return None, "rules"
    linked = _from_requirements(session, ctx, issue, by_code)
    if linked is not None:
        return linked, "category"
    return _from_words(issue, by_code), "rules"


def department_for(
    session: Session, ctx: ExecutionContext, issue: ClarificationIssue
) -> Department | None:
    """Which department answers for this clarification, or None if there are none."""
    return choose_department(session, ctx, issue)[0]


def route_to_department(session: Session, ctx: ExecutionContext, issue: ClarificationIssue) -> bool:
    """Fill in the department and its head as owner, where either is missing."""
    changed = False
    department = session.get(Department, issue.department_id) if issue.department_id else None
    if department is None:
        department, source = choose_department(session, ctx, issue)
        if department is not None:
            issue.routing_source = source
    if department is None:
        return False
    if issue.department_id != department.id:
        issue.department_id = department.id
        changed = True
    if issue.owner_user_id is None and department.head_user_id is not None:
        head = session.get(User, department.head_user_id)
        if head is not None and head.is_active:
            issue.owner_user_id = head.id
            changed = True
    if changed:
        session.flush()
    return changed


def settle_clarification_owners(session: Session, ctx: ExecutionContext) -> int:
    """Route every unfinished clarification that has no department or owner yet."""
    rows = (
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.status.in_(
                    [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
                ),
                (ClarificationIssue.department_id.is_(None))
                | (ClarificationIssue.owner_user_id.is_(None)),
            )
        )
        .scalars()
        .all()
    )
    return sum(1 for issue in rows if route_to_department(session, ctx, issue))


def _get(session: Session, ctx: ExecutionContext, issue_id: uuid.UUID) -> ClarificationIssue:
    issue = session.get(ClarificationIssue, issue_id)
    if issue is None or issue.org_id != ctx.org_id:
        raise NotFound("Clarification issue not found.")
    return issue


def assignable(session: Session, ctx: ExecutionContext, *, issue_id: uuid.UUID) -> dict:
    from app.services import tasks as task_service

    issue = _get(session, ctx, issue_id)
    department = session.get(Department, issue.department_id) if issue.department_id else None
    return task_service.assignable_for_department(session, ctx, department=department)


def assign_owner(
    session: Session, ctx: ExecutionContext, *, issue_id: uuid.UUID, owner_user_id: uuid.UUID
) -> ClarificationIssue:
    from app.services import tasks as task_service

    issue = _get(session, ctx, issue_id)
    department = session.get(Department, issue.department_id) if issue.department_id else None
    task_service.authorise_for_department(
        session, ctx, department=department, owner_user_id=owner_user_id
    )
    before = issue.owner_user_id
    issue.owner_user_id = owner_user_id
    session.flush()
    audit.record(
        session,
        ctx,
        action="clarification.owner_assigned",
        entity_type="clarification_issue",
        entity_id=issue.id,
        bid_id=issue.bid_id,
        before={"owner_user_id": str(before) if before else None},
        after={"owner_user_id": str(owner_user_id)},
    )
    return issue


def move_to_department(
    session: Session, ctx: ExecutionContext, *, issue_id: uuid.UUID, department_id: uuid.UUID
) -> ClarificationIssue:
    """Re-route a misfiled clarification; its owner becomes the new department's head."""
    issue = _get(session, ctx, issue_id)
    department = session.get(Department, department_id)
    if department is None or department.org_id != ctx.org_id:
        raise NotFound("That department does not exist.")
    before = {
        "department_id": str(issue.department_id) if issue.department_id else None,
        "owner_user_id": str(issue.owner_user_id) if issue.owner_user_id else None,
    }
    issue.department_id = department.id
    issue.owner_user_id = None
    issue.routing_source = "person"
    issue.routing_reason = None
    session.flush()
    route_to_department(session, ctx, issue)
    audit.record(
        session,
        ctx,
        action="clarification.department_changed",
        entity_type="clarification_issue",
        entity_id=issue.id,
        bid_id=issue.bid_id,
        before=before,
        after={
            "department_id": str(issue.department_id),
            "owner_user_id": str(issue.owner_user_id) if issue.owner_user_id else None,
        },
    )
    return issue


def describe(session: Session, ctx: ExecutionContext, issues: list[ClarificationIssue]) -> list:
    """Each issue as the API returns it, with department and owner named."""
    from app.schemas.tender import ClarificationOut

    departments = {
        d.id: d
        for d in session.execute(
            select(Department).where(Department.org_id == ctx.org_id)
        ).scalars()
    }
    owner_ids = {i.owner_user_id for i in issues if i.owner_user_id}
    owners = (
        {
            u.id: u
            for u in session.execute(
                select(User).where(User.org_id == ctx.org_id, User.id.in_(owner_ids))
            ).scalars()
        }
        if owner_ids
        else {}
    )
    out = []
    for issue in issues:
        row = ClarificationOut.model_validate(issue)
        department = departments.get(issue.department_id) if issue.department_id else None
        owner = owners.get(issue.owner_user_id) if issue.owner_user_id else None
        row.department = department.name if department else None
        row.owner_name = owner.display_name if owner else None
        if owner and department:
            row.owner_position = "Head" if department.head_user_id == owner.id else "Team member"
        out.append(row)
    return out


# --- the model chooses, where the clause does not ----------------------------
#
# A clarification linked to requirements follows their category. One that is not
# -- a missing deadline, an unnamed contact -- has only its wording to go on, and
# a word list cannot read a sentence. So those are put to the model in one batched
# call with the company's departments and what each answers for. The model names a
# department; code decides whether to believe it: a code the company does not have
# is ignored, and any failure leaves the word-list answer in place.

BATCH = 20


def _owner_follows(
    session: Session, issue: ClarificationIssue, old: Department | None, new: Department
) -> None:
    """Move the owner with the department, unless a person picked it."""
    old_head = old.head_user_id if old else None
    if issue.owner_user_id is None or issue.owner_user_id == old_head:
        if new.head_user_id is not None:
            head = session.get(User, new.head_user_id)
            issue.owner_user_id = head.id if head is not None and head.is_active else None
        else:
            issue.owner_user_id = None


def route_open_issues(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, gateway=None
) -> dict[str, int]:
    """Settle the department of every open clarification on the bid.

    The model chooses for all of them, from the problem text and the company's
    departments (it is not shown the fixed rules' answer, which kept its choices
    independent when the two were compared). Where the model gives no usable answer
    -- the call fails, or it names a department the company does not have -- the
    fixed rules decide: the linked requirements' category, then the wording.
    A person's choice, and an answer the model already gave, are never revisited.
    """
    from app.core.logging import get_logger

    log = get_logger("clarification_routing")
    by_code = _departments_by_code(session, ctx)
    counts = {"model": 0, "category": 0, "rules": 0}
    if not by_code:
        return counts
    rows = (
        session.execute(
            select(ClarificationIssue)
            .where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid_id,
                ClarificationIssue.status.in_(
                    [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
                ),
                (ClarificationIssue.routing_source.is_(None))
                | (ClarificationIssue.routing_source.in_(["rules", "category"])),
            )
            .order_by(ClarificationIssue.created_at)
        )
        .scalars()
        .all()
    )
    codes = {d.code.value: d for d in by_code.values()}
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        answers: dict[int, tuple[str, str]] = {}
        if gateway is not None:
            answers = _ask_model(session, ctx, chunk, by_code, gateway)
            if not answers:
                log.info("clarification_routing_unavailable", bid_id=str(bid_id))
        for i, issue in enumerate(chunk, start=1):
            current = session.get(Department, issue.department_id) if issue.department_id else None
            chosen = codes.get(answers[i][0]) if i in answers else None
            if chosen is not None:
                issue.routing_source = "model"
                issue.routing_reason = answers[i][1][:300] or None
                counts["model"] += 1
            else:
                chosen, how = choose_department(session, ctx, issue)
                issue.routing_source = how
                issue.routing_reason = None
                counts[how] += 1
            if chosen is not None and (current is None or current.id != chosen.id):
                issue.department_id = chosen.id
                _owner_follows(session, issue, current, chosen)
    session.flush()
    return counts


# --- comparing the model with the fixed rules ---------------------------------


def _ask_model(
    session: Session,
    ctx: ExecutionContext,
    chunk: Sequence[ClarificationIssue],
    by_code: dict,
    gateway,
) -> dict[int, tuple[str, str]]:
    """Ask the model for each item's department. {n: (code, reason)}; {} on any failure."""
    import json

    from app.gateway.registry import wrap_untrusted
    from app.schemas.clarification_routing import ClarificationRoutingResult
    from app.services.routing import responsibilities_of

    payload = {
        "DEPARTMENTS": [
            {"code": d.code.value, "name": d.name, "responsible_for": responsibilities_of(d)}
            for d in by_code.values()
        ],
        "ITEMS": [
            {
                "n": i,
                "problem": (issue.description or "")[:900],
                "question_for_client": (issue.recommended_question or "")[:500],
            }
            for i, issue in enumerate(chunk, start=1)
        ],
    }
    answers: dict[int, tuple[str, str]] = {}
    try:
        result = gateway.generate_structured(
            "clarification_routing",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
            ctx=ctx,
            session=session,
        )
        if isinstance(result, ClarificationRoutingResult):
            for item in result.items:
                if 1 <= item.n <= len(chunk):
                    answers[item.n] = (item.department.strip(), item.reason.strip())
    except Exception:  # noqa: BLE001
        return {}
    return answers


def compare_routing(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, gateway) -> dict:
    """Side by side: what the fixed rules say, and what the model says, for every open
    clarification. Changes nothing. The model is shown only the text and the
    departments -- not the rules' answer -- so the two are independent."""
    by_code = _departments_by_code(session, ctx)
    rows = (
        session.execute(
            select(ClarificationIssue)
            .where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid_id,
                ClarificationIssue.status.in_(
                    [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
                ),
            )
            .order_by(ClarificationIssue.created_at)
        )
        .scalars()
        .all()
    )
    codes = {d.code.value: d for d in by_code.values()}
    out = []
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        answers = _ask_model(session, ctx, chunk, by_code, gateway) if by_code else {}
        for i, issue in enumerate(chunk, start=1):
            fixed, how = choose_department(session, ctx, issue)
            ai_code, reason = answers.get(i, ("", ""))
            ai = codes.get(ai_code)
            out.append(
                {
                    "id": str(issue.id),
                    "problem": (issue.description or "")[:400],
                    "fixed": fixed.name if fixed else None,
                    "fixed_by": how,
                    "ai": ai.name if ai else None,
                    "ai_reason": reason if ai else None,
                    "agree": bool(fixed and ai and fixed.id == ai.id),
                }
            )
    asked = [r for r in out if r["ai"]]
    return {
        "total": len(out),
        "ai_answered": len(asked),
        "agree": sum(1 for r in asked if r["agree"]),
        "rows": out,
    }
