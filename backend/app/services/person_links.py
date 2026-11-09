"""Match the people in the CVs to the people with accounts.

Two populations of "people" exist in this system, and they are separate on
purpose. `app_user` rows are identities — they sign in, hold roles, own tasks —
and are created only by seeding or an org admin. `KnowledgeEntity` rows of type
PERSON are evidence, extracted from uploaded CVs, and can do none of those
things. Uploaded documents are untrusted input, so extraction is never allowed
to create or claim an account: that would make "upload a CV" a way to mint an
identity.

The cost of that boundary is a real gap. A staffing requirement asks for named
personnel with their qualifications; the corpus knows the people, the
organization knows the accounts, and nothing connects them. This module closes
it in the only way that keeps the boundary intact:

    the matcher proposes  →  a person decides  →  the link is a stated fact

**A link grants nothing.** No role, no permission, no access to any evidence.
Nothing in the permission evaluator reads it. It exists so a staffing answer can
name a real employee and so a task about that employee can be routed to them by
a human who now knows which account to pick.

**No model is involved.** Matching a name to an account is a string problem with
a right answer, not a judgement call. A regular expression that is wrong is
wrong the same way every time and can be inspected; a model that is wrong about
identity is wrong unpredictably. Where the strings do not settle it, this
proposes nothing and says why, which is the honest answer.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.models import KnowledgeEntity, PersonLink, User
from app.models.enums import EntityType, PersonLinkStatus
from app.services import audit

log = get_logger("person_links")

# How a pair was found. Shown to the reviewer, because "matched on a surname"
# deserves more scrutiny than "matched on an email address".
EXACT_NAME = "exact_name"
EMAIL_LOCAL = "email_local_part"
SURNAME_INITIAL = "surname_and_initial"
HUMAN = "chosen_by_reviewer"

CONFIDENCE = {EXACT_NAME: 0.99, EMAIL_LOCAL: 0.9, SURNAME_INITIAL: 0.7}

# Particles that carry no identity and would otherwise make "van der Berg" and
# "Berg" look like different surnames.
_PARTICLES = frozenset({"van", "der", "den", "de", "di", "da", "bin", "ibn", "al", "el", "la"})


def normalise(name: str) -> list[str]:
    """Name to comparable tokens: unaccented, lowercase, no punctuation.

    Accents are folded rather than preserved because the same person is written
    both ways across a CV, an HR export and an email address, and a match that
    depends on which one you happened to read is not a match.
    """
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    tokens = [t for t in re.split(r"[^a-z0-9]+", folded.lower()) if t]
    # Single letters are initials, not names: "Sara K." must not match every
    # account whose surname starts with K on the strength of the K alone.
    return [t for t in tokens if t not in _PARTICLES]


def _name_parts(tokens: list[str]) -> tuple[str, str] | None:
    """(first name, surname) for a name written as "Sara K." or "Sara Khalifa"."""
    if len(tokens) < 2:
        return None
    return tokens[0], tokens[-1]


def _compatible(a: str, b: str) -> bool:
    """Could these two be the same name part, one of them possibly abbreviated?

    An abbreviation compares on its letter; two full words must be equal, or one
    a prefix of the other. Comparing full words by first letter alone is how
    "Sara" and "Sami" become the same person — a real near-miss in a small
    organization, and the kind of wrong answer that is confidently presented
    rather than caught.
    """
    if not a or not b:
        return False
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    return a == b or a.startswith(b) or b.startswith(a)


@dataclass
class Match:
    user: User | None
    method: str
    confidence: float | None
    note: str


def _match_one(entity_name: str, users: list[User]) -> Match:
    """Find the one account this name denotes, or say why there isn't one."""
    tokens = normalise(entity_name)
    if not tokens:
        return Match(None, "", None, "The extracted name is empty.")

    joined = " ".join(tokens)
    exact = [u for u in users if " ".join(normalise(u.display_name)) == joined]
    if len(exact) == 1:
        return Match(exact[0], EXACT_NAME, CONFIDENCE[EXACT_NAME], "Full name matches exactly.")
    if len(exact) > 1:
        # Two accounts with the same display name is a real situation and not
        # one a string can resolve. Proposing either would be a coin toss
        # presented as a finding.
        return Match(
            None,
            "",
            None,
            f"{len(exact)} accounts share this name; a person must choose.",
        )

    by_email = [u for u in users if set(normalise(u.email.split("@")[0])) <= set(tokens)]
    by_email = [u for u in by_email if normalise(u.email.split("@")[0])]
    if len(by_email) == 1:
        return Match(
            by_email[0],
            EMAIL_LOCAL,
            CONFIDENCE[EMAIL_LOCAL],
            f"The account's email ({by_email[0].email}) is built from this name.",
        )

    # "Sara K." in an account against "Sara Khalifa" in a CV, or the reverse.
    parts = _name_parts(tokens)
    if parts is not None:
        first, surname = parts
        candidates = []
        for user in users:
            other = _name_parts(normalise(user.display_name))
            if other is None:
                continue
            if _compatible(first, other[0]) and _compatible(surname, other[1]):
                candidates.append(user)
        if len(candidates) == 1:
            return Match(
                candidates[0],
                SURNAME_INITIAL,
                CONFIDENCE[SURNAME_INITIAL],
                "Surname and first initial match; check this one.",
            )
        if len(candidates) > 1:
            return Match(
                None,
                "",
                None,
                f"{len(candidates)} accounts match on surname; a person must choose.",
            )

    return Match(None, "", None, "No account matches this name.")


def propose_links(session: Session, ctx: ExecutionContext) -> dict[str, int]:
    """Propose a link for every extracted person that has no decision yet.

    Confirmed and rejected rows are left exactly as they are. A reviewer's
    decision that a pair is wrong has to survive the next run, or the review
    list becomes a chore that regenerates itself.
    """
    people = list(
        session.execute(
            select(KnowledgeEntity)
            .where(
                KnowledgeEntity.org_id == ctx.org_id,
                KnowledgeEntity.entity_type == EntityType.PERSON,
                KnowledgeEntity.merged_into_entity_id.is_(None),
            )
            .order_by(KnowledgeEntity.canonical_name)
        ).scalars()
    )
    users = list(
        session.execute(
            select(User).where(User.org_id == ctx.org_id, User.is_active.is_(True))
        ).scalars()
    )
    decided = {
        link.entity_id: link
        for link in session.execute(
            select(PersonLink).where(PersonLink.org_id == ctx.org_id)
        ).scalars()
    }

    proposed = matched = unmatched = 0
    for entity in people:
        existing = decided.get(entity.id)
        if existing is not None and existing.status is not PersonLinkStatus.PROPOSED:
            continue

        match = _match_one(entity.canonical_name, users)
        if match.user is not None:
            matched += 1
        else:
            unmatched += 1

        if existing is None:
            existing = PersonLink(
                org_id=ctx.org_id,
                entity_id=entity.id,
                status=PersonLinkStatus.PROPOSED,
                method=match.method or "no_match",
            )
            session.add(existing)
            proposed += 1
        existing.proposed_user_id = match.user.id if match.user else None
        existing.user_id = None
        existing.method = match.method or "no_match"
        existing.confidence = match.confidence
        existing.note = match.note
    session.flush()

    summary = {
        "people": len(people),
        "accounts": len(users),
        "proposed": proposed,
        "with_a_match": matched,
        "needing_a_choice": unmatched,
    }
    log.info("person_links_proposed", **summary)
    return summary


def list_links(session: Session, ctx: ExecutionContext) -> list[dict]:
    """The review list: one row per extracted person, with its suggestion."""
    rows = list(
        session.execute(
            select(PersonLink, KnowledgeEntity)
            .join(KnowledgeEntity, KnowledgeEntity.id == PersonLink.entity_id)
            .where(PersonLink.org_id == ctx.org_id)
            .order_by(KnowledgeEntity.canonical_name)
        ).all()
    )
    users = {
        user.id: user
        for user in session.execute(select(User).where(User.org_id == ctx.org_id)).scalars()
    }

    out = []
    for link, entity in rows:
        chosen = users.get(link.user_id) if link.user_id else None
        suggested = users.get(link.proposed_user_id) if link.proposed_user_id else None
        shown = chosen or suggested
        out.append(
            {
                "link_id": str(link.id),
                "entity_id": str(entity.id),
                "person": entity.canonical_name,
                "status": link.status.value,
                "method": link.method,
                "confidence": float(link.confidence) if link.confidence is not None else None,
                "note": link.note,
                "account_id": str(shown.id) if shown else None,
                "account_email": shown.email if shown else None,
                "account_name": shown.display_name if shown else None,
            }
        )
    return out


def decide(
    session: Session,
    ctx: ExecutionContext,
    *,
    link_id: uuid.UUID,
    confirm: bool,
    user_id: uuid.UUID | None = None,
) -> PersonLink:
    """Confirm the suggestion, confirm a different account, or reject outright.

    `user_id` overrides whatever was proposed, which is the "or change it" case
    and the reason the matcher is allowed to be imperfect.
    """
    link = session.get(PersonLink, link_id)
    if link is None or link.org_id != ctx.org_id:
        raise NotFound("Person link not found.")

    if confirm:
        chosen = user_id or link.proposed_user_id
        if chosen is None:
            raise ValidationError(
                "There is no account to confirm. Choose one, or reject this suggestion."
            )
        user = session.get(User, chosen)
        if user is None or user.org_id != ctx.org_id:
            raise NotFound("Account not found.")
        link.user_id = user.id
        link.status = PersonLinkStatus.CONFIRMED
        if user_id is not None and user_id != link.proposed_user_id:
            link.method = HUMAN
            link.confidence = None
    else:
        link.user_id = None
        link.status = PersonLinkStatus.REJECTED

    link.decided_by = ctx.actor_user_id
    link.decided_at = datetime.now(timezone.utc)
    session.flush()

    audit.record(
        session,
        ctx,
        action="knowledge.person_link_decided",
        entity_type="person_link",
        entity_id=link.id,
        after={
            "status": link.status.value,
            "user_id": str(link.user_id) if link.user_id else None,
            "method": link.method,
        },
    )
    return link


def confirm_all(session: Session, ctx: ExecutionContext, *, min_confidence: float) -> int:
    """Confirm every proposal at or above a confidence, in one act.

    Bulk approval is for the rows that are not really decisions — an exact name
    match against a nine-person organization is not something a reviewer learns
    anything by clicking. It is deliberately bounded by confidence rather than
    offered as "approve everything": a surname-and-initial guess is exactly the
    kind of match a person should look at, and sweeping it up with the certain
    ones is how a review becomes a rubber stamp.
    """
    if min_confidence <= 0:
        raise ValidationError(
            "Bulk confirmation needs a confidence floor. Confirm uncertain matches one at a time."
        )
    links = list(
        session.execute(
            select(PersonLink).where(
                PersonLink.org_id == ctx.org_id,
                PersonLink.status == PersonLinkStatus.PROPOSED,
                PersonLink.proposed_user_id.is_not(None),
                PersonLink.confidence >= min_confidence,
            )
        ).scalars()
    )
    now = datetime.now(timezone.utc)
    for link in links:
        link.user_id = link.proposed_user_id
        link.status = PersonLinkStatus.CONFIRMED
        link.decided_by = ctx.actor_user_id
        link.decided_at = now
    session.flush()

    if links:
        audit.record(
            session,
            ctx,
            action="knowledge.person_links_bulk_confirmed",
            entity_type="organization",
            entity_id=ctx.org_id,
            after={"confirmed": len(links), "min_confidence": min_confidence},
        )
    log.info("person_links_bulk_confirmed", count=len(links), min_confidence=min_confidence)
    return len(links)


def confirmed_account_for(
    session: Session, ctx: ExecutionContext, *, entity_id: uuid.UUID
) -> User | None:
    """The account a confirmed link points at, or None.

    The only read path other code should use. A PROPOSED row is a suggestion
    and is invisible here on purpose: nothing downstream may act on a match
    nobody has confirmed.
    """
    link = session.execute(
        select(PersonLink).where(
            PersonLink.org_id == ctx.org_id,
            PersonLink.entity_id == entity_id,
            PersonLink.status == PersonLinkStatus.CONFIRMED,
        )
    ).scalar_one_or_none()
    if link is None or link.user_id is None:
        return None
    return session.get(User, link.user_id)


__all__ = [
    "confirm_all",
    "confirmed_account_for",
    "decide",
    "list_links",
    "normalise",
    "propose_links",
]
