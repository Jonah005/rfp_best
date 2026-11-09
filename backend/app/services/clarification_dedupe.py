"""Collapse questions that one customer reply would answer.

The last stage of a chain that began with clarifications being invisible. Once
they reached their rows, the rows were crowded; once the fragments behind that
were merged, what remained was the same question stored several times.

Two deterministic fixes were tried against the live data before this one, and
the failures are the argument for a model call:

    fingerprint on (issue_type, block)   collapses two real questions about one
                                         paragraph into one
    close what a rerun did not repeat    closed 44 real questions, because the
                                         prompt caps output at 12 per batch and
                                         no single run can enumerate a document

Character similarity fails from both sides at once. Two phrasings of the
knowledge-transfer question share few words and are one question; two different
questions about one clause share their whole lead-in and are two. Meaning is
what separates them, so the model judges meaning and the code records the
verdict with a version stamp -- the same division as every detector here.

The model is never allowed to decide what happens. It answers one question --
would a single reply answer both of these -- and this module decides which row
survives, which links come off, and what the matrix shows.

## Scope of a comparison

Questions are compared within the block they cite, not across the whole bid.
Restatements come from re-reading one clause on different passes, so that is
where they are; and comparing 79 questions with each other would be a single
enormous call whose mistakes are hard to see. A question that appears against
two different blocks stays twice, which is the recoverable direction.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.gateway.gateway import ModelGateway, build_gateway
from app.gateway.registry import get as get_call_type
from app.models import ClarificationIssue, ClarificationRequirement
from app.models.enums import ClarificationSeverity, ClarificationStatus
from app.schemas.clarification_dedupe import QuestionDedupeResult
from app.services import audit

log = get_logger("clarification_dedupe")

# Below this there is nothing to compare, and above it one call is being asked
# to hold too much in view at once.
MIN_GROUP = 2
MAX_PER_CALL = 20


# Worst first is what a fold must preserve. MINOR losing to MAJOR losing to
# BLOCKING, so `max` over a group is the severity the survivor must carry.
_SEVERITY_ORDER = {
    ClarificationSeverity.MINOR: 0,
    ClarificationSeverity.MAJOR: 1,
    ClarificationSeverity.BLOCKING: 2,
}


def _rank(severity) -> int:
    return _SEVERITY_ORDER.get(severity, 0)


def _locator_key(locator: object) -> tuple | None:
    if not isinstance(locator, dict):
        return None
    version_id = locator.get("document_version_id")
    if isinstance(version_id, str):
        try:
            version_id = uuid.UUID(version_id)
        except ValueError:
            return None
    return (version_id, locator.get("char_start"), locator.get("char_end"))


def _lift_severity_onto_canonicals(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> int:
    """Make every surviving question as serious as the ones folded into it.

    Run every time, for the folds made before this rule existed. One of them
    closed the BLOCKING submission-deadline question into a MAJOR one, so a bid
    with no known deadline stopped blocking generation -- and no later run
    would have revisited it, because that group is already judged under the
    current prompt version.

    Idempotent: a second run finds every canonical already carrying the worst
    severity in its family.
    """
    families: dict[uuid.UUID, list[ClarificationIssue]] = {}
    for issue in session.execute(
        select(ClarificationIssue).where(
            ClarificationIssue.org_id == ctx.org_id,
            ClarificationIssue.bid_id == bid_id,
            ClarificationIssue.restates_clarification_id.is_not(None),
        )
    ).scalars():
        if issue.restates_clarification_id is None:
            continue
        families.setdefault(issue.restates_clarification_id, []).append(issue)

    lifted = 0
    for canonical_id, restatements in families.items():
        canonical = session.get(ClarificationIssue, canonical_id)
        if canonical is None or canonical.org_id != ctx.org_id:
            continue
        worst = max(restatements, key=lambda i: _rank(i.severity))
        changed = False
        if _rank(worst.severity) > _rank(canonical.severity):
            canonical.severity = worst.severity
            changed = True
        if any(i.blocks_generation for i in restatements) and not canonical.blocks_generation:
            canonical.blocks_generation = True
            changed = True
        lifted += changed
    if lifted:
        session.flush()
    return lifted


def _groups_by_requirement(
    session: Session, ctx: ExecutionContext, issues: list[ClarificationIssue]
) -> list[list[ClarificationIssue]]:
    """Open questions grouped by the requirement each one was linked to.

    The second scope. Questions about one clause are caught by the block they
    cite; questions about one OBLIGATION can cite three different blocks and
    still be one question -- which is what the deadline did: the calendar date
    from the instructions block, the time zone from the submission block, and
    whether the 2017 document still governs from the cover page.

    A question linked to several requirements appears in several groups. That
    is harmless: the first group to fold it wins, and the rest skip it because
    a restatement is never offered as a candidate again.
    """
    if not issues:
        return []
    by_id = {issue.id: issue for issue in issues}
    rows = session.execute(
        select(
            ClarificationRequirement.requirement_id,
            ClarificationRequirement.clarification_id,
        ).where(
            ClarificationRequirement.org_id == ctx.org_id,
            ClarificationRequirement.clarification_id.in_(list(by_id)),
        )
    ).all()
    grouped: dict[uuid.UUID, list[ClarificationIssue]] = {}
    for requirement_id, clarification_id in rows:
        issue = by_id.get(clarification_id)
        if issue is not None:
            grouped.setdefault(requirement_id, []).append(issue)
    return [group for _rid, group in sorted(grouped.items(), key=lambda kv: str(kv[0]))]


def dedupe_open_questions(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    model_gateway: ModelGateway | None = None,
) -> int:
    """Fold restatements into the question they repeat, and close them.

    Returns the number newly marked. Idempotent in the sense that matters: a
    question already judged under the current prompt version is not re-judged,
    and a question already marked as a restatement is never offered as a
    candidate again.
    """
    model_gateway = model_gateway or build_gateway()
    call_type = get_call_type("question_equivalence")
    stamp = f"llm:{call_type.name}@{call_type.prompt_version}"

    # Questions folded in before folding meant closing.
    #
    # Restatements used to be marked and left OPEN. When that changed, the
    # ones already marked stayed open for ever: the selection below skips
    # anything that already carries a pointer, so nothing would ever look at
    # them again. The live bid had 39 of them -- forty per cent of its open
    # questions were questions it had already decided were duplicates.
    #
    # One statement, run every time, so the backlog drains on the next
    # analysis and this needs no migration. It is idempotent: a second run
    # finds nothing OPEN with a pointer.
    already_marked = list(
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid_id,
                ClarificationIssue.status == ClarificationStatus.OPEN,
                ClarificationIssue.restates_clarification_id.is_not(None),
            )
        ).scalars()
    )
    if already_marked:
        session.execute(
            delete(ClarificationRequirement).where(
                ClarificationRequirement.org_id == ctx.org_id,
                ClarificationRequirement.clarification_id.in_(
                    [issue.id for issue in already_marked]
                ),
            )
        )
        for issue in already_marked:
            issue.status = ClarificationStatus.CLOSED
            issue.resolution_note = issue.resolution_note or (
                "Same question as another clarification on this clause; that one "
                "carries it." + (f" {issue.equivalence_reason}" if issue.equivalence_reason else "")
            )
        session.flush()
        log.info(
            "clarification_restatements_closed_late",
            bid_id=str(bid_id),
            closed=len(already_marked),
        )

    lifted = _lift_severity_onto_canonicals(session, ctx, bid_id=bid_id)
    if lifted:
        log.info("clarification_severity_lifted", bid_id=str(bid_id), lifted=lifted)

    issues = (
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid_id,
                ClarificationIssue.status == ClarificationStatus.OPEN,
                ClarificationIssue.restates_clarification_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    # Only questions. A finding with no `recommended_question` is a note about
    # our own processing, and two notes being similar is not interesting.
    issues = [i for i in issues if (i.recommended_question or "").strip()]

    by_block: dict[tuple, list[ClarificationIssue]] = {}
    for issue in issues:
        key = _locator_key(issue.source_a)
        if key is None:
            continue
        by_block.setdefault(key, []).append(issue)

    def judge(groups: list[list[ClarificationIssue]], stamp: str) -> int:
        """Fold restatements within each group, under one version stamp.

        Called twice with different groupings and different stamps. A question
        already settled by one pass is not settled for the other -- the stamp
        is what records which question was asked of it.
        """
        marked = 0
        for group in groups:
            if len(group) < MIN_GROUP:
                continue
            # Already settled under this prompt version? Then the only thing to
            # judge is what has arrived since.
            pending = [i for i in group if i.equivalence_by != stamp]
            if not pending:
                continue
            # Judge the whole block's set, not only the new arrivals: a new question
            # can be a restatement of an old one, and the old one is the canonical
            # candidate. Sorted by id so the same set produces the same numbering.
            candidates = sorted(group, key=lambda i: str(i.id))[:MAX_PER_CALL]
            if len(candidates) < MIN_GROUP:
                continue

            payload = json.dumps(
                {
                    "questions": [
                        {
                            "index": index,
                            "issue_type": issue.issue_type.value,
                            "question": " ".join((issue.recommended_question or "").split()),
                        }
                        for index, issue in enumerate(candidates)
                    ]
                },
                ensure_ascii=False,
            )
            result = model_gateway.generate_structured(
                "question_equivalence", payload, ctx=ctx, session=session
            )
            if not isinstance(result, QuestionDedupeResult):
                raise ValidationError("question_equivalence returned an unexpected schema.")

            # Every index is checked before anything is written. An out-of-range
            # index is the model miscounting, and applying the in-range part of a
            # miscounted answer would attach a verdict to the wrong question --
            # worse than not deduping at all.
            for group_result in result.groups:
                indexes = [group_result.canonical_index, *group_result.restatement_indexes]
                if any(index >= len(candidates) or index < 0 for index in indexes):
                    log.info(
                        "question_equivalence_index_out_of_range",
                        bid_id=str(bid_id),
                        indexes=indexes,
                        offered=len(candidates),
                    )
                    continue

                # The OLDEST question in the group survives, not the model's pick.
                #
                # Which of several phrasings is "the" question is not a judgement
                # the model is better at than a clock, and letting it choose made
                # the active set drift: every run reworded the survivor, so the
                # same tender produced a different canonical open-question set each
                # time, and a reviewer could not tell a real change from a
                # rephrasing. The earliest row is the one other things already
                # point at, and it is the same row on every future run -- which is
                # what makes consecutive runs over an unchanged document agree.
                #
                # The model still decides WHICH questions are one question. That is
                # the judgement it is for.
                group_members = [
                    candidates[i]
                    for i in [group_result.canonical_index, *group_result.restatement_indexes]
                ]
                canonical = min(group_members, key=lambda i: (i.created_at, str(i.id)))
                # Point at the ROOT, never at another restatement.
                #
                # A question the model names as canonical may have been folded into
                # an older one on a previous run, and a chain of three was visible
                # in the live export: B restates A, A restates C. Nothing reading
                # the table follows a chain, so it is flattened here.
                seen_canonical = {canonical.id}
                while canonical.restates_clarification_id is not None:
                    parent = session.get(ClarificationIssue, canonical.restates_clarification_id)
                    if parent is None or parent.id in seen_canonical:
                        break
                    seen_canonical.add(parent.id)
                    canonical = parent
                for index in group_result.restatement_indexes:
                    restatement = candidates[index]
                    if restatement.id == canonical.id:
                        continue
                    restatement.restates_clarification_id = canonical.id
                    restatement.equivalence_by = stamp
                    restatement.equivalence_reason = group_result.reason or None
                    # Marked AND closed.
                    #
                    # Marking alone left 48 of 123 open questions on the live bid
                    # pointing at another open question. The number a bid manager
                    # reads as "what we are still waiting on the customer for" was
                    # inflated by two thirds, and the clarification letter offered
                    # to ask the same thing four times over. A restatement is not
                    # something we are waiting on -- the canonical question is --
                    # so it is closed here, with the reason naming what carries it.
                    #
                    # CLOSED is stable under re-detection: `raise_issue` returns
                    # early for the human-owned statuses, so the next analysis run
                    # does not reopen it and this does not oscillate.
                    restatement.status = ClarificationStatus.CLOSED
                    restatement.resolution_note = (
                        "Same question as another clarification on this clause; that one "
                        "carries it." + (f" {group_result.reason}" if group_result.reason else "")
                    )
                    marked += 1
                    # The canonical inherits the strongest severity in the
                    # group, and any blocker in it.
                    #
                    # Folding is about how many questions to ASK, never about
                    # how serious the answer is. The requirement-scoped pass
                    # caught three deadline questions on the live bid and kept
                    # the MAJOR one -- so the BLOCKING question about the
                    # submission deadline closed as a duplicate, and a bid with
                    # no known deadline stopped blocking generation. A
                    # deduplication that quietly downgrades a blocker is worse
                    # than the duplication it removed.
                    #
                    # The model chose which question survives; what that
                    # survivor CARRIES is decided here.
                    if _rank(restatement.severity) > _rank(canonical.severity):
                        canonical.severity = restatement.severity
                    if restatement.blocks_generation:
                        canonical.blocks_generation = True
                canonical.equivalence_by = stamp

            # Anything the model did not group stands on its own, and saying so is
            # what stops the next run paying to ask again.
            for issue in candidates:
                if issue.equivalence_by is None:
                    issue.equivalence_by = stamp

        return marked

    # Pass one: within the block a question cites. Restatements come from
    # re-reading one clause on different passes, so that is where most of them
    # are.
    marked = judge(
        [g for _k, g in sorted(by_block.items(), key=lambda kv: kv[0][1] or 0)],
        stamp,
    )
    # Pass two: within the REQUIREMENT a question was linked to.
    #
    # Three questions about the submission deadline survived pass one on the
    # live bid -- a MAJOR asking the calendar date, a BLOCKING asking date,
    # time and time zone, and a MINOR asking whether the 2017 document still
    # governs -- because they cite three different blocks. One reply from the
    # customer settles all three, and a clarification letter that asks the
    # deadline three ways reads as though nobody read the tender.
    #
    # The scope is still bounded, which is the point: a requirement holds a
    # handful of questions, not the bid's seventy, so each call stays small
    # enough for its mistakes to be visible.
    marked += judge(_groups_by_requirement(session, ctx, issues), f"{stamp}#by-requirement")

    if not marked:
        session.flush()
        return 0

    # A restatement holds no links. The canonical question carries the row, and
    # a matrix that counts links would otherwise count the same question four
    # times against one requirement.
    restatement_ids = [issue.id for issue in issues if issue.restates_clarification_id is not None]
    if restatement_ids:
        session.execute(
            delete(ClarificationRequirement).where(
                ClarificationRequirement.org_id == ctx.org_id,
                ClarificationRequirement.clarification_id.in_(restatement_ids),
            )
        )
    session.flush()

    audit.record(
        session,
        ctx,
        action="clarification.restatements_marked",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        after={"marked": marked, "judged_by": stamp},
        reason="Questions a single customer reply would answer, grouped under one.",
    )
    log.info("clarification_restatements_marked", bid_id=str(bid_id), marked=marked)
    return marked
