"""Opportunity and bid lifecycle.

Bid state is a guarded machine. Transitions are declared in one table rather
than scattered across handlers, so an illegal move fails the same way in a
request, a worker, or a script, and every move leaves an audit record.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import IllegalTransition, NotFound, ValidationError
from app.models import Bid, Opportunity
from app.models.enums import BidState
from app.services import audit, notifications

# from_state -> allowed to_states. Phase G only drives the intake and
# understanding portion; later phases add their own transitions rather than
# loosening these.
ALLOWED_TRANSITIONS: dict[BidState, frozenset[BidState]] = {
    BidState.DRAFT: frozenset({BidState.INTAKE_IN_PROGRESS, BidState.CANCELLED}),
    BidState.INTAKE_IN_PROGRESS: frozenset(
        {BidState.UNDERSTANDING_COMPLETE, BidState.NO_BID, BidState.CANCELLED}
    ),
    BidState.UNDERSTANDING_COMPLETE: frozenset(
        {
            BidState.ASSESSMENT_COMPLETE,
            BidState.INTAKE_IN_PROGRESS,  # a new amendment reopens intake
            BidState.NO_BID,
            BidState.CANCELLED,
        }
    ),
    BidState.ASSESSMENT_COMPLETE: frozenset(
        {BidState.BID_NO_BID_DECIDED, BidState.INTAKE_IN_PROGRESS, BidState.CANCELLED}
    ),
    BidState.BID_NO_BID_DECIDED: frozenset(
        {BidState.IN_PROGRESS, BidState.NO_BID, BidState.CANCELLED}
    ),
    BidState.IN_PROGRESS: frozenset(
        {BidState.ASSEMBLY, BidState.INTAKE_IN_PROGRESS, BidState.CANCELLED}
    ),
    BidState.ASSEMBLY: frozenset({BidState.VALIDATION, BidState.IN_PROGRESS, BidState.CANCELLED}),
    BidState.VALIDATION: frozenset(
        {BidState.RELEASE_PENDING, BidState.IN_PROGRESS, BidState.CANCELLED}
    ),
    BidState.RELEASE_PENDING: frozenset(
        {BidState.RELEASED, BidState.VALIDATION, BidState.CANCELLED}
    ),
    BidState.RELEASED: frozenset({BidState.ARCHIVED}),
    BidState.ARCHIVED: frozenset(),
    BidState.NO_BID: frozenset({BidState.ARCHIVED}),
    BidState.CANCELLED: frozenset({BidState.ARCHIVED}),
}

REASON_REQUIRED = frozenset({BidState.NO_BID, BidState.CANCELLED})


def create_opportunity(
    session: Session,
    ctx: ExecutionContext,
    *,
    customer_name: str,
    tender_name: str,
    tender_reference: str | None = None,
    bid_manager_user_id: uuid.UUID | None = None,
    account_manager_user_id: uuid.UUID | None = None,
    deadline: datetime | None = None,
    currency: str | None = None,
    region: str | None = None,
    estimated_value: float | None = None,
    source: str | None = None,
) -> Opportunity:
    if not customer_name.strip() or not tender_name.strip():
        raise ValidationError("An opportunity needs a customer and a tender name.")

    opportunity = Opportunity(
        org_id=ctx.org_id,
        customer_name=customer_name.strip(),
        tender_name=tender_name.strip(),
        tender_reference=(tender_reference or "").strip() or None,
        bid_manager_user_id=bid_manager_user_id,
        account_manager_user_id=account_manager_user_id,
        deadline=deadline,
        currency=currency,
        region=region,
        estimated_value=estimated_value,
        source=source,
    )
    session.add(opportunity)
    session.flush()
    audit.record(
        session,
        ctx,
        action="opportunity.created",
        entity_type="opportunity",
        entity_id=opportunity.id,
        after={
            "customer_name": opportunity.customer_name,
            "tender_name": opportunity.tender_name,
            "tender_reference": opportunity.tender_reference,
        },
    )
    return opportunity


def correct_opportunity(
    session: Session,
    ctx: ExecutionContext,
    *,
    opportunity_id: uuid.UUID,
    changes: dict,
) -> Opportunity:
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None or opportunity.org_id != ctx.org_id:
        raise NotFound("Opportunity not found.")

    def _shown(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

    before = {k: _shown(getattr(opportunity, k)) for k in changes}
    for key, value in changes.items():
        if key == "deadline":  # a date, not text: recorded as given, or cleared
            opportunity.deadline = value
            continue
        cleaned = (value or "").strip() or None
        if key in ("customer_name", "tender_name") and cleaned is None:
            raise ValidationError("The customer and tender name cannot be blank.")
        setattr(opportunity, key, cleaned)
    session.flush()
    audit.record(
        session,
        ctx,
        action="opportunity.corrected",
        entity_type="opportunity",
        entity_id=opportunity.id,
        before=before,
        after={k: _shown(getattr(opportunity, k)) for k in changes},
    )
    return opportunity


def promote_to_bid(
    session: Session, ctx: ExecutionContext, *, opportunity_id: uuid.UUID, language: str = "en"
) -> Bid:
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None or opportunity.org_id != ctx.org_id:
        raise NotFound("Opportunity not found.")

    existing = session.execute(
        select(Bid).where(Bid.org_id == ctx.org_id, Bid.opportunity_id == opportunity_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    bid = Bid(
        org_id=ctx.org_id,
        opportunity_id=opportunity_id,
        state=BidState.DRAFT,
        language=language,
    )
    session.add(bid)
    session.flush()
    audit.record(
        session,
        ctx,
        action="bid.created",
        entity_type="bid",
        entity_id=bid.id,
        bid_id=bid.id,
        after={"opportunity_id": str(opportunity_id), "state": bid.state.value},
    )
    return bid


def get_bid(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> Bid:
    bid = session.get(Bid, bid_id)
    if bid is None or bid.org_id != ctx.org_id:
        raise NotFound("Bid not found.")
    return bid


def transition(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    to_state: BidState,
    reason: str | None = None,
) -> Bid:
    bid = get_bid(session, ctx, bid_id=bid_id)
    if bid.state is to_state:
        return bid

    allowed = ALLOWED_TRANSITIONS.get(bid.state, frozenset())
    if to_state not in allowed:
        permitted = ", ".join(sorted(s.value for s in allowed)) or "nothing"
        raise IllegalTransition(
            f"A bid in {bid.state.value} cannot move to {to_state.value}. Permitted: {permitted}."
        )
    if to_state in REASON_REQUIRED and not (reason or "").strip():
        raise ValidationError(f"Moving a bid to {to_state.value} requires a reason.")

    before = bid.state
    bid.state = to_state
    if to_state is BidState.NO_BID:
        bid.no_bid_reason = reason
    session.flush()

    audit.record(
        session,
        ctx,
        action="bid.state_changed",
        entity_type="bid",
        entity_id=bid.id,
        bid_id=bid.id,
        before={"state": before.value},
        after={"state": to_state.value},
        reason=reason,
    )
    # Everyone with work on this bid, and only them. Notifying the whole
    # organization about every bid would train people to ignore the bell, which
    # costs more than the notification is worth.
    from app.models import Task

    owners = (
        session.execute(
            select(Task.owner_user_id).where(
                Task.org_id == ctx.org_id,
                Task.bid_id == bid.id,
                Task.owner_user_id.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    notifications.notify(
        session,
        ctx,
        recipients=[owner for owner in owners if owner is not None],
        kind="bid.state_changed",
        title=f"Bid moved to {to_state.value.replace('_', ' ').lower()}",
        body=reason,
        screen="readiness",
        bid_id=bid.id,
    )
    return bid


def ensure_intake_open(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> Bid:
    """Move a bid into intake when the first tender document arrives.

    Also used when an amendment lands after understanding is complete: the bid
    returns to intake so the change is re-analysed rather than absorbed
    silently.
    """
    bid = get_bid(session, ctx, bid_id=bid_id)
    if bid.state is BidState.INTAKE_IN_PROGRESS:
        return bid
    if BidState.INTAKE_IN_PROGRESS in ALLOWED_TRANSITIONS.get(bid.state, frozenset()):
        return transition(
            session,
            ctx,
            bid_id=bid_id,
            to_state=BidState.INTAKE_IN_PROGRESS,
            reason="tender document received",
        )
    raise IllegalTransition(f"A bid in {bid.state.value} cannot accept new tender documents.")
