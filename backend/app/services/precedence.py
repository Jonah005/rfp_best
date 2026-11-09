"""Per-clause precedence resolution.

The rule this module exists to avoid is the obvious-looking one:
``amendment > clarification > original``. That ordering is wrong the moment a
customer issues a clarification about a clause an earlier amendment changed —
and getting it wrong is silent, because the losing text simply never becomes a
requirement.

So precedence is *resolved* per clause, from evidence, with the method and
confidence recorded. When the evidence does not discriminate, the resolver
declines to pick and raises a clarification instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.models import (
    ClauseResolution,
    Document,
    DocumentBlock,
    DocumentClassification,
    DocumentRelation,
    DocumentVersion,
)
from app.models.enums import (
    ClarificationIssueType,
    ClarificationSeverity,
    DocumentRelationType,
    DocumentSide,
    DocumentType,
    DocumentVersionState,
    PrecedenceMethod,
)
from app.services import audit
from app.services.clause_identity import qualified_clause_ref

log = get_logger("precedence")

# Below this, the resolver does not assert a winner.
MIN_CONFIDENCE = 0.60

# Confidence attached to each method, reflecting how much the evidence actually
# tells us rather than how convenient the answer is.
METHOD_CONFIDENCE: dict[PrecedenceMethod, float] = {
    PrecedenceMethod.EXPLICIT_SUPERSESSION: 0.98,
    PrecedenceMethod.CUSTOMER_STATED_RULE: 0.90,
    PrecedenceMethod.ISSUE_DATE: 0.80,
    PrecedenceMethod.VERSION_LABEL: 0.70,
    PrecedenceMethod.DOCUMENT_TYPE_DEFAULT: 0.50,
    PrecedenceMethod.MANUAL: 1.0,
}

# The weakest signal, used only when nothing else discriminates. Higher wins.
# This is a tie-breaker of last resort, NOT the precedence rule.
DOCUMENT_TYPE_RANK: dict[DocumentType, int] = {
    DocumentType.CLARIFICATION: 40,
    DocumentType.AMENDMENT: 30,
    DocumentType.TECHNICAL_SPECIFICATION: 20,
    DocumentType.BOQ: 20,
    DocumentType.APPENDIX: 15,
    DocumentType.TERMS_AND_CONDITIONS: 15,
    DocumentType.RFP: 10,
    DocumentType.RFQ: 10,
}

SUPERSEDING_RELATIONS = (
    DocumentRelationType.SUPERSEDES,
    DocumentRelationType.REPLACES_CLAUSE,
    DocumentRelationType.AMENDS,
    DocumentRelationType.CLARIFIES,
)


@dataclass
class ClauseCandidate:
    version: DocumentVersion
    block: DocumentBlock
    document_type: DocumentType
    issue_date: date | None
    version_label: str | None

    @property
    def type_rank(self) -> int:
        return DOCUMENT_TYPE_RANK.get(self.document_type, 0)


@dataclass
class ResolutionOutcome:
    clause_ref: str
    winner: ClauseCandidate | None
    competitors: list[ClauseCandidate]
    method: PrecedenceMethod
    confidence: float
    rationale: str
    unresolved: bool = False
    conflict_pair: tuple[ClauseCandidate, ClauseCandidate] | None = None


@dataclass
class PrecedenceReport:
    resolved: int = 0
    unresolved: int = 0
    clauses: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "clauses": len(self.clauses),
        }


def _explicit_supersession_map(
    session: Session, ctx: ExecutionContext, version_ids: set[uuid.UUID]
) -> dict[str, set[uuid.UUID]]:
    """clause_ref, as the RELATION spells it -> version ids superseded there.

    The keys here are bare references -- "4.2", "6.1" -- because that is what an
    amendment says about itself and what `DocumentRelation.clause_refs` records.
    They are deliberately NOT the qualified keys `collect_candidates` groups by,
    and `_is_superseded` is the one place the two spellings are reconciled.
    Matching these strings against a qualified key is what broke explicit
    supersession; see the note there.
    """
    relations = (
        session.execute(
            select(DocumentRelation).where(
                DocumentRelation.org_id == ctx.org_id,
                DocumentRelation.from_version_id.in_(version_ids),
                DocumentRelation.relation_type.in_(SUPERSEDING_RELATIONS),
            )
        )
        .scalars()
        .all()
    )
    superseded: dict[str, set[uuid.UUID]] = {}
    for relation in relations:
        for clause in relation.clause_refs or ["*"]:
            key = (clause or "").strip()
            if key:
                superseded.setdefault(key, set()).add(relation.to_version_id)
    return superseded


def _is_superseded(candidate: ClauseCandidate, superseded: dict[str, set[uuid.UUID]]) -> bool:
    """Has THIS version been explicitly superseded for THIS candidate's clause?

    Two key spaces meet here, and for a while they silently failed to.

    `collect_candidates` groups by the QUALIFIED reference -- "4.2 Testing scope
    / 4.2" -- because tenders restart numbering in every section and a bare "3"
    once gathered three unrelated obligations into one contest. That change was
    right and is not being undone.

    `DocumentRelation.clause_refs` holds BARE references, because that is what
    an amendment says about itself: "this replaces clause 4.2". Nothing writing
    a relation knows, or should know, which section heading the block will end
    up under.

    The old code looked the qualified key up in the bare-keyed map, so it never
    matched anything. EXPLICIT_SUPERSESSION -- the highest-confidence method
    there is, a customer stating in writing which document governs -- simply
    never fired, and every such clause fell through to issue-date ordering
    without a word. An amendment issued BEFORE the document it amends (which is
    ordinary: the amendment is dated, the consolidated RFP is re-issued later)
    therefore lost to the document it was supposed to replace.

    So the reconciliation happens per candidate, against that candidate's own
    block, and accepts either spelling: a relation may name the bare ref, and a
    person or a future detector may name the qualified one.

    Six integration tests reported this the whole time. They were read as a
    stubbed provider running out of canned responses and left red.
    """
    if candidate.version.id in superseded.get("*", set()):
        return True
    bare = (getattr(candidate.block, "clause_ref", None) or "").strip()
    keys = {bare, qualified_clause_ref(candidate.block) or ""}
    keys.discard("")
    return any(candidate.version.id in superseded.get(key, set()) for key in keys)


def collect_candidates(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> dict[str, list[ClauseCandidate]]:
    """Every clause-bearing block across the bid's indexed tender versions."""
    rows = session.execute(
        select(DocumentBlock, DocumentVersion, DocumentClassification.document_type)
        .join(DocumentVersion, DocumentVersion.id == DocumentBlock.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .outerjoin(
            DocumentClassification,
            DocumentClassification.document_version_id == DocumentVersion.id,
        )
        .where(
            DocumentBlock.org_id == ctx.org_id,
            Document.bid_id == bid_id,
            # Tender side only. A company document must never be able to
            # contribute a clause to a tender's precedence graph.
            Document.side == DocumentSide.TENDER,
            Document.withdrawn_at.is_(None),
            DocumentVersion.state.in_([DocumentVersionState.INDEXED, DocumentVersionState.ACTIVE]),
            DocumentBlock.clause_ref.is_not(None),
        )
        .order_by(DocumentBlock.ordinal)
    ).all()

    candidates: dict[str, list[ClauseCandidate]] = {}
    for block, version, document_type in rows:
        # Keyed by the QUALIFIED reference, not the bare one.
        #
        # Precedence exists to answer "two documents both address clause 6.1,
        # which governs". Keying on `block.clause_ref` made it answer a
        # different question, because tenders restart numbering in every
        # section: on the Jadwa RFP the key "3" gathered REIT subscription
        # forms, a B2B bank integration and an SSL obligation into one contest.
        # A contest keeps one winner, so two real obligations were discarded --
        # and since requirement extraction only ever read winners, they never
        # reached the model. 39 numbered blocks collapsed to 14 resolutions and
        # the tender read as 14 requirements.
        #
        # The rationale rows were nonsense too: a stored resolution claimed SSL
        # Support governed REIT Subscription Forms, and that text feeds the
        # clarifications queue a human is asked to work.
        key = qualified_clause_ref(block) or block.clause_ref
        candidates.setdefault(key, []).append(
            ClauseCandidate(
                version=version,
                block=block,
                document_type=document_type or DocumentType.OTHER,
                issue_date=version.issue_date,
                version_label=version.version_label,
            )
        )
    return candidates


def _decide(
    clause_ref: str,
    candidates: list[ClauseCandidate],
    *,
    superseded: dict[str, set[uuid.UUID]],
    stated_priority: list[str] | None,
) -> ResolutionOutcome:
    if len(candidates) == 1:
        only = candidates[0]
        return ResolutionOutcome(
            clause_ref=clause_ref,
            winner=only,
            competitors=[],
            method=PrecedenceMethod.DOCUMENT_TYPE_DEFAULT,
            confidence=0.95,
            rationale="Only one document version addresses this clause.",
        )

    # 1. Explicit supersession: a relation naming this clause (or the document
    #    as a whole) removes the superseded version from contention.
    surviving = [c for c in candidates if not _is_superseded(c, superseded)]
    if len(surviving) < len(candidates) and len(surviving) == 1:
        return ResolutionOutcome(
            clause_ref=clause_ref,
            winner=surviving[0],
            competitors=[c for c in candidates if c is not surviving[0]],
            method=PrecedenceMethod.EXPLICIT_SUPERSESSION,
            confidence=METHOD_CONFIDENCE[PrecedenceMethod.EXPLICIT_SUPERSESSION],
            rationale=(
                "A document relation explicitly supersedes the competing version(s) "
                "for this clause."
            ),
        )
    if surviving:
        candidates = surviving

    # 2. A precedence rule the customer stated in the tender itself.
    if stated_priority:
        ranked = {name.upper(): position for position, name in enumerate(stated_priority)}
        scored = [(ranked.get(c.document_type.value, 10_000), c) for c in candidates]
        scored.sort(key=lambda pair: pair[0])
        if scored[0][0] < 10_000 and (len(scored) == 1 or scored[0][0] != scored[1][0]):
            winner = scored[0][1]
            return ResolutionOutcome(
                clause_ref=clause_ref,
                winner=winner,
                competitors=[c for _, c in scored[1:]],
                method=PrecedenceMethod.CUSTOMER_STATED_RULE,
                confidence=METHOD_CONFIDENCE[PrecedenceMethod.CUSTOMER_STATED_RULE],
                rationale=(
                    "The tender states a precedence order that ranks "
                    f"{winner.document_type.value} above the alternatives."
                ),
            )

    # 3. Issue date. This is the step that lets a later clarification beat an
    #    earlier amendment on one clause while the amendment keeps another.
    # Pair each dated candidate with its date so the ordering is provably total
    # rather than sorting over an Optional the type checker cannot vouch for.
    dated: list[tuple[date, ClauseCandidate]] = [
        (c.issue_date, c) for c in candidates if c.issue_date is not None
    ]
    if len(dated) >= 2:
        dated.sort(key=lambda pair: pair[0], reverse=True)
        (newest_date, newest), (runner_up_date, _runner_up) = dated[0], dated[1]
        if newest_date > runner_up_date:
            return ResolutionOutcome(
                clause_ref=clause_ref,
                winner=newest,
                competitors=[c for _, c in dated[1:]],
                method=PrecedenceMethod.ISSUE_DATE,
                confidence=METHOD_CONFIDENCE[PrecedenceMethod.ISSUE_DATE],
                rationale=(
                    f"Issued {newest_date.isoformat()}, later than the competing "
                    f"{runner_up_date.isoformat()}."
                ),
            )
    elif len(dated) == 1 and len(candidates) > 1:
        undated = [c for c in candidates if c.issue_date is None]
        return ResolutionOutcome(
            clause_ref=clause_ref,
            winner=dated[0][1],
            competitors=undated,
            method=PrecedenceMethod.ISSUE_DATE,
            confidence=0.65,
            rationale=(
                "Only one competing version carries an issue date; the undated "
                "version(s) cannot be shown to be later."
            ),
        )

    # 4. Version label.
    labelled = [c for c in candidates if (c.version_label or "").strip()]
    if len(labelled) >= 2:
        labelled.sort(key=lambda c: _label_key(c.version_label), reverse=True)
        if _label_key(labelled[0].version_label) != _label_key(labelled[1].version_label):
            return ResolutionOutcome(
                clause_ref=clause_ref,
                winner=labelled[0],
                competitors=labelled[1:],
                method=PrecedenceMethod.VERSION_LABEL,
                confidence=METHOD_CONFIDENCE[PrecedenceMethod.VERSION_LABEL],
                rationale=f"Version label {labelled[0].version_label} is the highest.",
            )

    # 5. Document type, the weakest signal.
    by_type = sorted(candidates, key=lambda c: c.type_rank, reverse=True)
    if by_type[0].type_rank > by_type[1].type_rank:
        return ResolutionOutcome(
            clause_ref=clause_ref,
            winner=by_type[0],
            competitors=by_type[1:],
            method=PrecedenceMethod.DOCUMENT_TYPE_DEFAULT,
            confidence=METHOD_CONFIDENCE[PrecedenceMethod.DOCUMENT_TYPE_DEFAULT],
            rationale=(
                f"No date, label or explicit rule discriminates; {by_type[0].document_type.value} "
                "outranks the alternatives by document type alone."
            ),
        )

    # Nothing discriminates. Decline rather than guess.
    return ResolutionOutcome(
        clause_ref=clause_ref,
        winner=None,
        competitors=candidates,
        method=PrecedenceMethod.DOCUMENT_TYPE_DEFAULT,
        confidence=0.0,
        rationale=(
            "Two or more document versions address this clause and no explicit "
            "supersession, stated rule, issue date, version label or document type "
            "distinguishes them."
        ),
        unresolved=True,
        conflict_pair=(by_type[0], by_type[1]),
    )


def _label_key(label: str | None) -> tuple:
    """Compare version labels numerically where possible, textually otherwise."""
    if not label:
        return (0,)
    parts = []
    for token in label.replace("-", ".").replace("_", ".").split("."):
        token = token.strip().lstrip("vV")
        parts.append((1, int(token)) if token.isdigit() else (0, token.lower()))
    return tuple(parts)


def resolve_bid_precedence(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    stated_priority: list[str] | None = None,
) -> PrecedenceReport:
    """Resolve every clause in a bid and persist the outcomes."""
    from app.services import clarifications

    candidates = collect_candidates(session, ctx, bid_id=bid_id)
    if not candidates:
        return PrecedenceReport()

    version_ids = {c.version.id for group in candidates.values() for c in group}
    superseded = _explicit_supersession_map(session, ctx, version_ids)

    report = PrecedenceReport()
    now = datetime.now(timezone.utc)

    for clause_ref, group in sorted(candidates.items()):
        # One candidate per version: a clause repeated inside one document is
        # still one claim by that document.
        deduped: dict[uuid.UUID, ClauseCandidate] = {}
        for candidate in group:
            deduped.setdefault(candidate.version.id, candidate)
        outcome = _decide(
            clause_ref,
            list(deduped.values()),
            superseded=superseded,
            stated_priority=stated_priority,
        )
        _persist(session, ctx, bid_id=bid_id, outcome=outcome, now=now)
        report.clauses.append(clause_ref)

        if outcome.unresolved or outcome.confidence < MIN_CONFIDENCE:
            report.unresolved += 1
            clarifications.raise_issue(
                session,
                ctx,
                bid_id=bid_id,
                issue_type=ClarificationIssueType.PRECEDENCE_UNRESOLVED,
                description=(
                    f"Clause {clause_ref} is addressed by more than one tender document "
                    f"and precedence could not be established. {outcome.rationale}"
                ),
                severity=ClarificationSeverity.BLOCKING
                if outcome.unresolved
                else ClarificationSeverity.MAJOR,
                blocks_generation=outcome.unresolved,
                recommended_question=(f"Which document governs clause {clause_ref}?"),
                source_a=_locator(outcome.conflict_pair[0]) if outcome.conflict_pair else None,
                source_b=_locator(outcome.conflict_pair[1]) if outcome.conflict_pair else None,
                detected_by="precedence_resolver@v1",
                fingerprint=f"precedence:{clause_ref}",
            )
        else:
            report.resolved += 1

    audit.record(
        session,
        ctx,
        action="precedence.resolved",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        after=report.as_dict(),
    )
    return report


def _locator(candidate: ClauseCandidate) -> dict:
    from app.schemas.source_locator import SourceLocator

    block = candidate.block
    return SourceLocator(
        document_version_id=candidate.version.id,
        heading_path=list(block.heading_path or []),
        clause_ref=block.clause_ref,
        table_name=block.table_name,
        page_from=block.page_from,
        page_to=block.page_to,
        char_start=block.char_start,
        char_end=block.char_end,
    ).as_json()


def _persist(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    outcome: ResolutionOutcome,
    now: datetime,
) -> ClauseResolution:
    previous = session.execute(
        select(ClauseResolution).where(
            ClauseResolution.org_id == ctx.org_id,
            ClauseResolution.bid_id == bid_id,
            ClauseResolution.clause_ref == outcome.clause_ref,
            ClauseResolution.is_current.is_(True),
        )
    ).scalar_one_or_none()

    resolution = ClauseResolution(
        org_id=ctx.org_id,
        bid_id=bid_id,
        clause_ref=outcome.clause_ref,
        winning_document_version_id=outcome.winner.version.id if outcome.winner else None,
        winning_block_id=outcome.winner.block.id if outcome.winner else None,
        competing_version_ids=[c.version.id for c in outcome.competitors] or None,
        method=outcome.method,
        confidence=outcome.confidence,
        rationale=outcome.rationale,
        resolved_at=now,
        is_current=True,
    )

    if previous is not None:
        # History is retained; only one row per clause is current.
        previous.is_current = False
        session.flush()
        session.add(resolution)
        session.flush()
        previous.superseded_by_resolution_id = resolution.id
    else:
        session.add(resolution)
        session.flush()
    return resolution


def override(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    clause_ref: str,
    winning_document_version_id: uuid.UUID,
    reason: str,
) -> ClauseResolution:
    """A human decides. Recorded as MANUAL with the actor and the reason."""
    if not (reason or "").strip():
        raise ValidationError("A precedence override requires a reason.")

    version = session.get(DocumentVersion, winning_document_version_id)
    if version is None or version.org_id != ctx.org_id:
        raise NotFound("Document version not found.")

    # Resolutions are keyed by the qualified reference ("Business Requirements /
    # 3"), so the block must be found the same way. Comparing the column
    # directly would look up a bare "3" against a store of qualified keys and
    # find nothing -- and a human override that silently cannot find its clause
    # is the worst possible failure for the one path that exists to let a person
    # correct the machine.
    candidates = (
        session.execute(
            select(DocumentBlock)
            .where(
                DocumentBlock.org_id == ctx.org_id,
                DocumentBlock.document_version_id == winning_document_version_id,
                DocumentBlock.clause_ref.is_not(None),
            )
            .order_by(DocumentBlock.ordinal)
        )
        .scalars()
        .all()
    )
    # Either spelling is accepted, and the QUALIFIED one is what gets stored.
    #
    # A screen passes the key it displays, which is qualified. A person types
    # what the tender calls the clause, which is bare -- and refusing "4.2" with
    # "that document version does not contain clause 4.2" is a lie about a
    # document that plainly does.
    #
    # Canonicalising matters more than accepting. `clause_ref` is the key
    # `is_current` is bookkept against, so storing the caller's spelling would
    # let a bare override and a qualified one coexist as two current
    # resolutions for one clause -- the override path quietly creating the
    # ambiguity it exists to remove.
    wanted = (clause_ref or "").strip()
    matches = [
        b
        for b in candidates
        if (qualified_clause_ref(b) or b.clause_ref) == wanted
        or (b.clause_ref or "").strip() == wanted
    ]
    if not matches:
        raise ValidationError(f"That document version does not contain clause {wanted}.")
    distinct = {qualified_clause_ref(b) or b.clause_ref or "" for b in matches}
    if len(distinct) > 1:
        # Exactly what qualification exists to prevent: one bare number used by
        # several sections. Guessing here would override the wrong clause.
        raise ValidationError(
            f"Clause {wanted} is ambiguous in that document version -- it appears as "
            + ", ".join(sorted(distinct))
            + ". Name the one you mean."
        )
    block = matches[0]
    clause_ref = qualified_clause_ref(block) or block.clause_ref or ""

    previous = session.execute(
        select(ClauseResolution).where(
            ClauseResolution.org_id == ctx.org_id,
            ClauseResolution.bid_id == bid_id,
            ClauseResolution.clause_ref == clause_ref,
            ClauseResolution.is_current.is_(True),
        )
    ).scalar_one_or_none()

    if previous is not None:
        previous.is_current = False
        session.flush()

    resolution = ClauseResolution(
        org_id=ctx.org_id,
        bid_id=bid_id,
        clause_ref=clause_ref,
        winning_document_version_id=winning_document_version_id,
        winning_block_id=block.id,
        competing_version_ids=previous.competing_version_ids if previous else None,
        method=PrecedenceMethod.MANUAL,
        confidence=METHOD_CONFIDENCE[PrecedenceMethod.MANUAL],
        rationale=reason.strip(),
        resolved_by=ctx.actor_user_id,
        resolved_at=datetime.now(timezone.utc),
        is_current=True,
    )
    session.add(resolution)
    session.flush()
    if previous is not None:
        previous.superseded_by_resolution_id = resolution.id

    audit.record(
        session,
        ctx,
        action="precedence.overridden",
        entity_type="clause_resolution",
        entity_id=resolution.id,
        bid_id=bid_id,
        before={
            "clause_ref": clause_ref,
            "winning_document_version_id": str(previous.winning_document_version_id)
            if previous and previous.winning_document_version_id
            else None,
            "method": previous.method.value if previous else None,
        },
        after={
            "winning_document_version_id": str(winning_document_version_id),
            "method": PrecedenceMethod.MANUAL.value,
        },
        reason=reason,
    )
    return resolution


def current_resolutions(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> list[ClauseResolution]:
    return list(
        session.execute(
            select(ClauseResolution)
            .where(
                ClauseResolution.org_id == ctx.org_id,
                ClauseResolution.bid_id == bid_id,
                ClauseResolution.is_current.is_(True),
            )
            .order_by(ClauseResolution.clause_ref)
        )
        .scalars()
        .all()
    )


def winning_block_ids(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    """clause_ref -> the block that governs it.

    Requirement extraction reads only these. A superseded clause cannot become a
    requirement, which is the whole point of resolving precedence first.
    """
    return {
        resolution.clause_ref: resolution.winning_block_id
        for resolution in current_resolutions(session, ctx, bid_id=bid_id)
        if resolution.winning_block_id is not None and resolution.confidence >= MIN_CONFIDENCE
    }
