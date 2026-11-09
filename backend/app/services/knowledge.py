"""Company knowledge graph: governance, entity resolution, expiry, conflicts.

Three rules this module exists to enforce, none of which is left to the model:

  * Two people are never silently merged. Entity resolution proposes; a
    Knowledge Administrator decides.
  * Two contradictory facts are never silently ranked. Both are marked
    CONFLICTING and raised for review.
  * Validity is arithmetic, not judgement. A fact past its valid_to is EXPIRED
    on the next sweep, and anything relying on it becomes unsupported.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ProviderError, ValidationError
from app.core.logging import get_logger
from app.gateway.gateway import build_gateway
from app.models import (
    Evidence,
    EvidenceLink,
    KnowledgeEntity,
    KnowledgeFact,
    KnowledgeRelation,
    KnowledgeReviewItem,
)
from app.models.enums import (
    ApprovalStatus,
    ConfidentialityClass,
    EntityType,
    EvidenceLinkRole,
    EvidenceLinkTarget,
    KnowledgeStatus,
    RelationPredicate,
    ReviewQueueKind,
)
from app.services import audit
from app.services.placeholders import disqualifying_match

log = get_logger("knowledge")

# Above this similarity an extracted mention links to an existing entity;
# below it, a PROPOSED entity is created and queued for a human. Deliberately
# high: a wrong merge conflates two people's credentials, which is exactly the
# kind of error that ends up in a submitted proposal.
AUTO_LINK_THRESHOLD = 0.92

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    return _NON_ALNUM.sub(" ", name.lower()).strip()


def name_similarity(a: str, b: str) -> float:
    """Token-set similarity, tolerant of initials and reordering."""
    from difflib import SequenceMatcher

    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    return max(jaccard, ratio)


# --- entities ---------------------------------------------------------------


def find_entity_candidates(
    session: Session,
    ctx: ExecutionContext,
    *,
    entity_type: EntityType,
    name: str,
    limit: int = 5,
) -> list[tuple[KnowledgeEntity, float]]:
    rows = (
        session.execute(
            select(KnowledgeEntity).where(
                KnowledgeEntity.org_id == ctx.org_id,
                KnowledgeEntity.entity_type == entity_type,
                KnowledgeEntity.merged_into_entity_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    scored: list[tuple[KnowledgeEntity, float]] = []
    for row in rows:
        best = name_similarity(name, row.canonical_name)
        for alias in row.aliases or []:
            best = max(best, name_similarity(name, alias))
        if best > 0.5:
            scored.append((row, best))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def resolve_or_create_entity(
    session: Session,
    ctx: ExecutionContext,
    *,
    entity_type: EntityType,
    name: str,
    classification: ConfidentialityClass = ConfidentialityClass.INTERNAL,
    attributes: dict | None = None,
    extracted_by: str = "human",
) -> tuple[KnowledgeEntity, bool]:
    """Link to an existing entity when confident; otherwise propose a new one.

    Returns (entity, was_created). Confident matches link to the existing row.
    An ambiguous new entity is proposed with candidate matches for review; any
    new model-extracted entity is proposed even when no candidate exists. Only
    an unmatched entity entered by a human starts approved.
    """
    candidates = find_entity_candidates(session, ctx, entity_type=entity_type, name=name)
    if candidates and candidates[0][1] >= AUTO_LINK_THRESHOLD:
        matched = candidates[0][0]
        if name not in (matched.aliases or []) and normalize_name(name) != normalize_name(
            matched.canonical_name
        ):
            matched.aliases = sorted({*(matched.aliases or []), name})
        return matched, False

    proposed = extracted_by != "human"
    entity = KnowledgeEntity(
        org_id=ctx.org_id,
        entity_type=entity_type,
        canonical_name=name.strip(),
        aliases=None,
        classification=classification,
        status=KnowledgeStatus.PROPOSED
        if (candidates or proposed)
        else KnowledgeStatus.CURRENT_VERIFIED,
        approval_status=ApprovalStatus.PENDING_APPROVAL
        if (candidates or proposed)
        else ApprovalStatus.APPROVED,
        attributes=attributes,
        owner_user_id=ctx.actor_user_id,
    )
    session.add(entity)
    session.flush()

    if candidates:
        # Ambiguous: never merge silently, never guess.
        queue_review(
            session,
            ctx,
            kind=ReviewQueueKind.ENTITY_MATCH,
            subject_type="knowledge_entity",
            subject_id=entity.id,
            detail={
                "proposed_name": name,
                "entity_type": entity_type.value,
                "candidates": [
                    {"id": str(c.id), "name": c.canonical_name, "score": round(score, 3)}
                    for c, score in candidates
                ],
            },
            candidate_ids=[c.id for c, _ in candidates],
        )
    elif proposed:
        queue_review(
            session,
            ctx,
            kind=ReviewQueueKind.PROPOSED,
            subject_type="knowledge_entity",
            subject_id=entity.id,
            detail={
                "proposed_name": name,
                "entity_type": entity_type.value,
                "extracted_by": extracted_by,
            },
        )

    audit.record(
        session,
        ctx,
        action="knowledge.entity_created",
        entity_type="knowledge_entity",
        entity_id=entity.id,
        after={"name": name, "type": entity_type.value, "status": entity.status.value},
        reason=extracted_by,
    )
    return entity, True


def merge_entities(
    session: Session,
    ctx: ExecutionContext,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    reason: str,
) -> KnowledgeEntity:
    """Merge source into target. Old references still resolve through the pointer."""
    if source_id == target_id:
        raise ValidationError("An entity cannot be merged into itself.")
    source = session.get(KnowledgeEntity, source_id)
    target = session.get(KnowledgeEntity, target_id)
    if source is None or source.org_id != ctx.org_id:
        raise NotFound("Entity not found.")
    if target is None or target.org_id != ctx.org_id:
        raise NotFound("Entity not found.")
    if source.entity_type is not target.entity_type:
        raise ValidationError("Entities of different types cannot be merged.")

    for fact in session.execute(
        select(KnowledgeFact).where(
            KnowledgeFact.org_id == ctx.org_id, KnowledgeFact.entity_id == source_id
        )
    ).scalars():
        fact.entity_id = target_id
    for relation in session.execute(
        select(KnowledgeRelation).where(
            KnowledgeRelation.org_id == ctx.org_id,
            or_(
                KnowledgeRelation.subject_entity_id == source_id,
                KnowledgeRelation.object_entity_id == source_id,
            ),
        )
    ).scalars():
        if relation.subject_entity_id == source_id:
            relation.subject_entity_id = target_id
        if relation.object_entity_id == source_id:
            relation.object_entity_id = target_id

    target.aliases = sorted(
        {*(target.aliases or []), *(source.aliases or []), source.canonical_name}
    )
    source.merged_into_entity_id = target_id
    source.status = KnowledgeStatus.SUPERSEDED
    session.flush()

    audit.record(
        session,
        ctx,
        action="knowledge.entities_merged",
        entity_type="knowledge_entity",
        entity_id=target_id,
        before={"source": str(source_id)},
        after={"target": str(target_id)},
        reason=reason,
    )
    return target


# --- facts and relations ----------------------------------------------------


def add_fact(
    session: Session,
    ctx: ExecutionContext,
    *,
    entity_id: uuid.UUID,
    predicate: str,
    value_text: str | None = None,
    value_number: float | None = None,
    value_date: date | None = None,
    value_json: dict | None = None,
    unit: str | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
    evidence_ids: list[uuid.UUID] | None = None,
    extracted_by: str = "human",
    confidence: float | None = None,
    approved: bool | None = None,
) -> KnowledgeFact:
    """Record a fact about an entity.

    `approved` says whether the person entering it may vouch for it. It was
    implied by `extracted_by == "human"`, so any user with knowledge:write could
    type "Project Z, 30 web apps, completed" and it was reusable evidence the
    moment it was saved, while an uploaded completion letter for the same
    project waited for a knowledge administrator. The API now passes whether the
    caller holds knowledge:approve; left as None, service callers keep the
    previous behaviour.
    """
    if all(v is None for v in (value_text, value_number, value_date, value_json)):
        raise ValidationError("A fact must carry a value.")
    if valid_from and valid_to and valid_to < valid_from:
        raise ValidationError("valid_to cannot precede valid_from.")

    vouched = (extracted_by == "human") if approved is None else approved
    fact = KnowledgeFact(
        org_id=ctx.org_id,
        entity_id=entity_id,
        predicate=predicate,
        value_text=value_text,
        value_number=value_number,
        value_date=value_date,
        value_json=value_json,
        unit=unit,
        valid_from=valid_from,
        valid_to=valid_to,
        status=_initial_status(valid_from, valid_to) if vouched else KnowledgeStatus.PROPOSED,
        approval_status=ApprovalStatus.APPROVED if vouched else ApprovalStatus.PENDING_APPROVAL,
        confidence=confidence,
        extracted_by=extracted_by,
    )
    session.add(fact)
    session.flush()

    for evidence_id in evidence_ids or []:
        link_evidence(
            session,
            ctx,
            evidence_id=evidence_id,
            target_type=EvidenceLinkTarget.KNOWLEDGE_FACT,
            target_id=fact.id,
        )

    detect_fact_conflicts(session, ctx, fact=fact)
    if not vouched:
        queue_review(
            session,
            ctx,
            kind=ReviewQueueKind.PROPOSED,
            subject_type="knowledge_fact",
            subject_id=fact.id,
            detail={
                "entity_id": str(entity_id),
                "predicate": predicate,
                "extracted_by": extracted_by,
            },
        )
    audit.record(
        session,
        ctx,
        action="knowledge.fact_added",
        entity_type="knowledge_fact",
        entity_id=fact.id,
        after={"entity_id": str(entity_id), "predicate": predicate},
        reason=extracted_by,
    )
    return fact


def add_relation(
    session: Session,
    ctx: ExecutionContext,
    *,
    subject_entity_id: uuid.UUID,
    predicate: RelationPredicate,
    object_entity_id: uuid.UUID,
    attributes: dict | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
    evidence_ids: list[uuid.UUID] | None = None,
    extracted_by: str = "human",
    confidence: float | None = None,
) -> KnowledgeRelation:
    if subject_entity_id == object_entity_id:
        raise ValidationError("An entity cannot relate to itself.")
    if valid_from and valid_to and valid_to < valid_from:
        raise ValidationError("valid_to cannot precede valid_from.")

    relation = KnowledgeRelation(
        org_id=ctx.org_id,
        subject_entity_id=subject_entity_id,
        predicate=predicate,
        object_entity_id=object_entity_id,
        attributes=attributes,
        valid_from=valid_from,
        valid_to=valid_to,
        status=KnowledgeStatus.PROPOSED
        if extracted_by != "human"
        else _initial_status(valid_from, valid_to),
        approval_status=ApprovalStatus.APPROVED
        if extracted_by == "human"
        else ApprovalStatus.PENDING_APPROVAL,
        confidence=confidence,
        extracted_by=extracted_by,
    )
    session.add(relation)
    session.flush()

    if extracted_by != "human":
        queue_review(
            session,
            ctx,
            kind=ReviewQueueKind.PROPOSED,
            subject_type="knowledge_relation",
            subject_id=relation.id,
            detail={
                "subject_entity_id": str(subject_entity_id),
                "predicate": predicate.value,
                "object_entity_id": str(object_entity_id),
                "extracted_by": extracted_by,
            },
        )

    for evidence_id in evidence_ids or []:
        link_evidence(
            session,
            ctx,
            evidence_id=evidence_id,
            target_type=EvidenceLinkTarget.KNOWLEDGE_RELATION,
            target_id=relation.id,
        )

    audit.record(
        session,
        ctx,
        action="knowledge.relation_added",
        entity_type="knowledge_relation",
        entity_id=relation.id,
        after={
            "subject": str(subject_entity_id),
            "predicate": predicate.value,
            "object": str(object_entity_id),
        },
        reason=extracted_by,
    )
    return relation


def _initial_status(valid_from: date | None, valid_to: date | None) -> KnowledgeStatus:
    today = date.today()
    if valid_to is not None and valid_to < today:
        return KnowledgeStatus.HISTORICAL_VERIFIED
    return KnowledgeStatus.CURRENT_VERIFIED


# --- conflicts and expiry ---------------------------------------------------


def _overlaps(a_from, a_to, b_from, b_to) -> bool:
    lo_a, hi_a = a_from or date.min, a_to or date.max
    lo_b, hi_b = b_from or date.min, b_to or date.max
    return lo_a <= hi_b and lo_b <= hi_a


def _fact_value(fact: KnowledgeFact) -> Any:
    for value in (fact.value_text, fact.value_number, fact.value_date, fact.value_json):
        if value is not None:
            return value
    return None


def detect_fact_conflicts(
    session: Session, ctx: ExecutionContext, *, fact: KnowledgeFact
) -> list[KnowledgeFact]:
    """Two admissible facts with the same (entity, predicate), overlapping
    validity and different values are a contradiction, not a ranking problem."""
    siblings = (
        session.execute(
            select(KnowledgeFact).where(
                KnowledgeFact.org_id == ctx.org_id,
                KnowledgeFact.entity_id == fact.entity_id,
                KnowledgeFact.predicate == fact.predicate,
                KnowledgeFact.id != fact.id,
                KnowledgeFact.status.in_(
                    [KnowledgeStatus.CURRENT_VERIFIED, KnowledgeStatus.CONFLICTING]
                ),
            )
        )
        .scalars()
        .all()
    )
    conflicting = [
        other
        for other in siblings
        if other.supersedes_fact_id != fact.id
        and fact.supersedes_fact_id != other.id
        and _overlaps(fact.valid_from, fact.valid_to, other.valid_from, other.valid_to)
        and _fact_value(other) != _fact_value(fact)
    ]
    if not conflicting:
        return []

    fact.status = KnowledgeStatus.CONFLICTING
    for other in conflicting:
        other.status = KnowledgeStatus.CONFLICTING
    session.flush()

    queue_review(
        session,
        ctx,
        kind=ReviewQueueKind.CONFLICT,
        subject_type="knowledge_fact",
        subject_id=fact.id,
        detail={
            "entity_id": str(fact.entity_id),
            "predicate": fact.predicate,
            "values": [str(_fact_value(fact))] + [str(_fact_value(o)) for o in conflicting],
        },
        candidate_ids=[o.id for o in conflicting],
    )
    log.info(
        "knowledge_conflict",
        predicate=fact.predicate,
        entity_id=str(fact.entity_id),
        count=len(conflicting),
    )
    return conflicting


def expire_stale(
    session: Session, ctx: ExecutionContext, *, today: date | None = None
) -> dict[str, int]:
    """Flip anything past its validity window to EXPIRED."""
    moment = today or date.today()
    counts = {"facts": 0, "relations": 0, "evidence": 0}

    for fact in session.execute(
        select(KnowledgeFact).where(
            KnowledgeFact.org_id == ctx.org_id,
            KnowledgeFact.valid_to.is_not(None),
            KnowledgeFact.valid_to < moment,
            KnowledgeFact.status.in_(
                [KnowledgeStatus.CURRENT_VERIFIED, KnowledgeStatus.NEEDS_CONFIRMATION]
            ),
        )
    ).scalars():
        fact.status = KnowledgeStatus.EXPIRED
        counts["facts"] += 1

    for relation in session.execute(
        select(KnowledgeRelation).where(
            KnowledgeRelation.org_id == ctx.org_id,
            KnowledgeRelation.valid_to.is_not(None),
            KnowledgeRelation.valid_to < moment,
            KnowledgeRelation.status.in_(
                [KnowledgeStatus.CURRENT_VERIFIED, KnowledgeStatus.NEEDS_CONFIRMATION]
            ),
        )
    ).scalars():
        relation.status = KnowledgeStatus.EXPIRED
        counts["relations"] += 1

    for evidence in session.execute(
        select(Evidence).where(
            Evidence.org_id == ctx.org_id,
            Evidence.expiry_date.is_not(None),
            Evidence.expiry_date < moment,
            Evidence.status == KnowledgeStatus.CURRENT_VERIFIED,
        )
    ).scalars():
        evidence.status = KnowledgeStatus.EXPIRED
        counts["evidence"] += 1

    session.flush()
    if any(counts.values()):
        audit.record(
            session,
            ctx,
            action="knowledge.expired",
            entity_type="knowledge",
            after=counts,
            reason="scheduled validity sweep",
        )
    return counts


def find_expiring(
    session: Session, ctx: ExecutionContext, *, within_days: int = 90
) -> list[Evidence]:
    from datetime import timedelta

    horizon = date.today() + timedelta(days=within_days)
    return list(
        session.execute(
            select(Evidence).where(
                Evidence.org_id == ctx.org_id,
                Evidence.expiry_date.is_not(None),
                Evidence.expiry_date <= horizon,
                Evidence.status == KnowledgeStatus.CURRENT_VERIFIED,
            )
        )
        .scalars()
        .all()
    )


# --- evidence ---------------------------------------------------------------


def embed_excerpt(excerpt: str) -> list[float] | None:
    """Embed one excerpt, or return None if that is not possible right now.

    Deliberately swallowing: evidence that exists without a vector is still
    findable lexically and can be backfilled later, whereas evidence that failed
    to be recorded because an embedding provider was down is simply lost.
    """
    text = (excerpt or "").strip()
    if not text:
        return None
    try:
        return build_gateway().embed([text])[0]
    except Exception:  # noqa: BLE001 - see docstring
        log.warning("evidence_embedding_unavailable")
        return None


def backfill_evidence_vectors(session: Session, ctx: ExecutionContext, *, limit: int = 500) -> dict:
    """Embed evidence recorded before it had a vector column.

    Needed once, after the semantic-matching revision: every excerpt already in
    the corpus predates the column, so without this the improvement applies only
    to documents uploaded from now on — and the corpus a user has already built
    is exactly the one they want it to work for.

    Bounded per call so a large corpus is worked through in steps rather than in
    one request that times out halfway and leaves no record of how far it got.
    """
    rows = list(
        session.execute(
            select(Evidence)
            .where(Evidence.org_id == ctx.org_id, Evidence.excerpt_vector.is_(None))
            .limit(limit)
        ).scalars()
    )
    pending = [(evidence, (evidence.excerpt or "").strip()) for evidence in rows]
    pending = [(evidence, text) for evidence, text in pending if text]

    embedded = 0
    if pending:
        # One call for the batch, not one per excerpt. The model encodes a list
        # far faster than the same texts one at a time, and on CPU — which is
        # where this runs — that is the difference between a backfill measured
        # in seconds and one measured in minutes.
        #
        # Failures are raised, not logged and shrugged off.
        #
        # This used to swallow the exception, set `vectors = []` and return
        # {"embedded": 0, "remaining": 610} with HTTP 200 — so a missing model,
        # an undownloaded set of weights or a dimension mismatch all presented
        # to the user as a completed action that happened to do nothing. The
        # only place the truth existed was a WARNING in a server log nobody was
        # reading, and pressing the button again produced the same cheerful
        # nothing, for ever.
        #
        # Tolerating an embedding failure is right on the ingestion path, where
        # the user asked to upload a document and embedding is a side effect:
        # the excerpt is stored with a NULL vector and this backfill exists to
        # pick it up later. It is wrong here, where embedding is the whole
        # point of the request. Someone who asked for exactly one thing has to
        # be told whether it happened.
        try:
            vectors = build_gateway().embed([text for _, text in pending])
        except Exception as exc:  # noqa: BLE001 - re-raised as a domain error
            log.warning("evidence_embedding_unavailable", count=len(pending), error=str(exc))
            raise ProviderError(
                "The embedding model is not available, so nothing was embedded. "
                "Semantic matching stays off until it is — requirements can still "
                f"find evidence by shared words. The provider reported: {exc}"
            ) from exc
        # strict=True: a provider returning fewer vectors than texts is a bug,
        # and zipping loosely would embed a prefix of the batch and report that
        # count as a success. `remaining` would then fall by less than expected
        # with nothing anywhere saying why.
        for (evidence, _text), vector in zip(pending, vectors, strict=True):
            evidence.excerpt_vector = vector
            embedded += 1
    session.flush()
    remaining = session.execute(
        select(func.count())
        .select_from(Evidence)
        .where(Evidence.org_id == ctx.org_id, Evidence.excerpt_vector.is_(None))
    ).scalar_one()
    return {"embedded": embedded, "remaining": remaining}


def create_evidence(
    session: Session,
    ctx: ExecutionContext,
    *,
    source_document_version_id: uuid.UUID,
    source_locator: dict,
    excerpt: str,
    evidence_type,
    confidentiality_class: ConfidentialityClass = ConfidentialityClass.INTERNAL,
    effective_date: date | None = None,
    expiry_date: date | None = None,
    approved_for_reuse: bool = False,
    applicable_region: list[str] | None = None,
    proposed: bool = False,
) -> Evidence:
    evidence = Evidence(
        org_id=ctx.org_id,
        source_document_version_id=source_document_version_id,
        source_locator=source_locator,
        excerpt=excerpt,
        evidence_type=evidence_type,
        source_owner_user_id=ctx.actor_user_id,
        approval_status=ApprovalStatus.PENDING_APPROVAL,
        confidentiality_class=confidentiality_class,
        effective_date=effective_date,
        expiry_date=expiry_date,
        applicable_region=applicable_region,
        version=1,
        status=KnowledgeStatus.PROPOSED
        if proposed
        else _initial_status(effective_date, expiry_date),
        approved_for_reuse=approved_for_reuse,
        # Embedded on creation so a requirement can find this excerpt by
        # meaning. Best-effort: a missing embedder must not stop evidence being
        # recorded, and the lexical path still works without a vector.
        excerpt_vector=embed_excerpt(excerpt),
    )
    session.add(evidence)
    session.flush()
    if proposed:
        queue_review(
            session,
            ctx,
            kind=ReviewQueueKind.PROPOSED,
            subject_type="evidence",
            subject_id=evidence.id,
            detail={
                "source_document_version_id": str(source_document_version_id),
                "excerpt": excerpt[:200],
            },
        )
    audit.record(
        session,
        ctx,
        action="evidence.created",
        entity_type="evidence",
        entity_id=evidence.id,
        after={"excerpt": excerpt[:200], "expiry_date": str(expiry_date) if expiry_date else None},
    )
    return evidence


def approve_evidence_batch(
    session: Session,
    ctx: ExecutionContext,
    *,
    evidence_ids: list[uuid.UUID] | None,
    reason: str,
) -> dict:
    """Approve many pieces of evidence in one reviewed decision.

    A single company document yields dozens of excerpts and a corpus yields
    hundreds, so approving one at a time is not review — it is clicking, and
    clicking through hundreds of rows produces rubber-stamping, which is the
    failure the approval gate exists to prevent. Reviewing a document's
    excerpts together and approving them as a set is the honest version of the
    same decision.

    Every item still goes through `approve_evidence`, so each one is audited
    individually with the reason given, and the record shows who approved what.
    Nothing is approved implicitly: `evidence_ids` of None means "everything
    currently pending", which the caller has to ask for explicitly.

    Expired evidence is skipped rather than failing the batch. It cannot be
    approved for reuse — that rule is what stops a lapsed certificate reading
    as current — and one stale row should not block the other ninety-nine.
    """
    # Selected by "not yet usable", not by "not yet approved".
    #
    # Those differ, and the difference stranded a corpus. Evidence carries two
    # independent axes: approval (may a human vouch for it) and status (is it
    # verified and current). Extraction records PROPOSED, and a batch that
    # filtered only on approval left evidence APPROVED-but-PROPOSED — approved,
    # inadmissible, and invisible to a second attempt, because it no longer
    # matched the filter. Pressing the button again did nothing, for ever.
    #
    # What a reviewer means by "approve these" is "make these usable", so that
    # is what the query asks for.
    query = select(Evidence).where(
        Evidence.org_id == ctx.org_id,
        or_(
            Evidence.approval_status != ApprovalStatus.APPROVED,
            Evidence.status.in_([KnowledgeStatus.PROPOSED, KnowledgeStatus.NEEDS_CONFIRMATION]),
        ),
    )
    if evidence_ids:
        query = query.where(Evidence.id.in_(evidence_ids))
    rows = session.execute(query).scalars().all()

    approved = 0
    skipped: list[dict] = []
    for evidence in rows:
        if evidence.status is KnowledgeStatus.EXPIRED:
            skipped.append({"id": str(evidence.id), "reason": "expired"})
            continue
        # Skipped, not failed -- same reasoning as expired. One template in a
        # pack of real documents must not block the other ninety-nine, and the
        # count comes back to the caller so the reviewer sees how many were
        # held back and why.
        disqualifier = disqualifying_match(evidence.excerpt)
        if disqualifier is not None:
            skipped.append(
                {
                    "id": str(evidence.id),
                    "reason": "not evidence",
                    "matched": disqualifier,
                    "excerpt": (evidence.excerpt or "")[:120],
                }
            )
            continue
        approve_evidence(session, ctx, evidence_id=evidence.id, reason=reason)
        approved += 1
    return {"approved": approved, "skipped": skipped}


def approve_evidence(
    session: Session, ctx: ExecutionContext, *, evidence_id: uuid.UUID, reason: str
) -> Evidence:
    evidence = session.get(Evidence, evidence_id)
    if evidence is None or evidence.org_id != ctx.org_id:
        raise NotFound("Evidence not found.")
    if evidence.status is KnowledgeStatus.EXPIRED:
        raise ValidationError("Expired evidence cannot be approved for reuse. Re-verify it first.")
    # Text that says it is not real cannot be approved as proof.
    #
    # This gate did not exist, and its absence produced the most dangerous
    # output this system has managed: a bulk approval swept 306 extracted
    # excerpts to APPROVED/CURRENT_VERIFIED, and at least 52 of them say
    # "SAMPLE / NOT VALID", "SYNTHETIC / REPLACE", "DRAFT FOR APPROVAL" or
    # "certificates not supplied" in their own words -- including invented
    # revenue figures and a rate card of made-up numbers. Approved and current,
    # they are indistinguishable from real proof to retrieval, to assessment
    # and to a drafted statement, and they would have been quoted into a
    # submitted bid as demonstrated capability.
    #
    # Refused rather than silently downgraded: a person asked for this to be
    # approved, and the answer is no, with the phrase that decided it. Replace
    # the document, or edit the excerpt if the marker is incidental.
    disqualifier = disqualifying_match(evidence.excerpt)
    if disqualifier is not None:
        raise ValidationError(
            f"This excerpt says it is not evidence ({disqualifier!r}), so it cannot be "
            "approved as proof of capability. Replace the source document with a real "
            "one, or correct the excerpt if the wording is incidental."
        )
    before = evidence.approval_status.value
    before_status = evidence.status.value
    evidence.approval_status = ApprovalStatus.APPROVED
    evidence.approved_for_reuse = True
    evidence.last_verified_at = datetime.now(timezone.utc)
    # Approval is the verification. Extraction records evidence as PROPOSED —
    # a claim awaiting a human — and nothing else in the system ever moved it
    # on, so every extracted excerpt stayed permanently inadmissible: assessment
    # only considers verified states, and approving changed only the approval
    # flag. A whole company corpus could be uploaded, extracted and approved and
    # still match nothing.
    #
    # Currentness is then derived from the dates rather than assumed: evidence
    # whose validity has already ended is verified as HISTORICAL, not current.
    if evidence.status in (KnowledgeStatus.PROPOSED, KnowledgeStatus.NEEDS_CONFIRMATION):
        evidence.status = _initial_status(evidence.effective_date, evidence.expiry_date)
    session.flush()
    audit.record(
        session,
        ctx,
        action="evidence.approved",
        entity_type="evidence",
        entity_id=evidence.id,
        before={"approval_status": before, "status": before_status},
        after={"approval_status": evidence.approval_status.value, "status": evidence.status.value},
        reason=reason,
    )
    # Uploaded from inside a task? Its owner is waiting to use it.
    from app.services import task_evidence

    task_evidence.on_evidence_approved(session, ctx, evidence=evidence)
    return evidence


def link_evidence(
    session: Session,
    ctx: ExecutionContext,
    *,
    evidence_id: uuid.UUID,
    target_type: EvidenceLinkTarget,
    target_id: uuid.UUID,
    link_role: EvidenceLinkRole = EvidenceLinkRole.SUPPORTS,
    confidence: float | None = None,
) -> EvidenceLink:
    link = EvidenceLink(
        org_id=ctx.org_id,
        evidence_id=evidence_id,
        target_type=target_type,
        target_id=target_id,
        link_role=link_role,
        confidence=confidence,
    )
    session.add(link)
    session.flush()
    return link


def supporting_evidence(
    session: Session,
    ctx: ExecutionContext,
    *,
    target_type: EvidenceLinkTarget,
    target_id: uuid.UUID,
    live_only: bool = True,
) -> list[Evidence]:
    stmt = (
        select(Evidence)
        .join(EvidenceLink, EvidenceLink.evidence_id == Evidence.id)
        .where(
            Evidence.org_id == ctx.org_id,
            EvidenceLink.target_type == target_type,
            EvidenceLink.target_id == target_id,
            EvidenceLink.link_role.in_(
                [EvidenceLinkRole.SUPPORTS, EvidenceLinkRole.PARTIALLY_SUPPORTS]
            ),
        )
    )
    if live_only:
        stmt = stmt.where(
            Evidence.status.in_(
                [KnowledgeStatus.CURRENT_VERIFIED, KnowledgeStatus.HISTORICAL_VERIFIED]
            ),
            Evidence.approval_status == ApprovalStatus.APPROVED,
        )
    return list(session.execute(stmt).scalars().all())


# --- review queue -----------------------------------------------------------


def queue_review(
    session: Session,
    ctx: ExecutionContext,
    *,
    kind: ReviewQueueKind,
    subject_type: str,
    subject_id: uuid.UUID,
    detail: dict | None = None,
    candidate_ids: list[uuid.UUID] | None = None,
) -> KnowledgeReviewItem:
    item = KnowledgeReviewItem(
        org_id=ctx.org_id,
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
        detail=detail,
        candidate_ids=candidate_ids,
    )
    session.add(item)
    session.flush()
    return item


def open_reviews(
    session: Session, ctx: ExecutionContext, *, kind: ReviewQueueKind | None = None
) -> list[KnowledgeReviewItem]:
    stmt = select(KnowledgeReviewItem).where(
        KnowledgeReviewItem.org_id == ctx.org_id,
        KnowledgeReviewItem.resolved_at.is_(None),
    )
    if kind is not None:
        stmt = stmt.where(KnowledgeReviewItem.kind == kind)
    return list(session.execute(stmt).scalars().all())


def resolve_review(
    session: Session,
    ctx: ExecutionContext,
    *,
    item_id: uuid.UUID,
    resolution: str,
    merge_into_entity_id: uuid.UUID | None = None,
) -> KnowledgeReviewItem:
    item = session.get(KnowledgeReviewItem, item_id)
    if item is None or item.org_id != ctx.org_id:
        raise NotFound("Review item not found.")
    if item.resolved_at is not None:
        raise ValidationError("That review item is already resolved.")

    if merge_into_entity_id is not None:
        if item.kind is not ReviewQueueKind.ENTITY_MATCH:
            raise ValidationError("Only entity-match items can be resolved by merging.")
        merge_entities(
            session,
            ctx,
            source_id=item.subject_id,
            target_id=merge_into_entity_id,
            reason=resolution,
        )

    item.resolved_at = datetime.now(timezone.utc)
    item.resolved_by = ctx.actor_user_id
    item.resolution = resolution
    session.flush()
    audit.record(
        session,
        ctx,
        action="knowledge.review_resolved",
        entity_type="knowledge_review_item",
        entity_id=item.id,
        after={"resolution": resolution},
        reason=resolution,
    )
    return item
