"""Letters the tender prescribes, written from a fixed template and recorded facts.

The drafting model answers a requirement one statement at a time. Asked for a letter of
transmittal it describes the clause ("Clause 5.15 requires ...") instead of writing the
letter. A letter is a form: its wording is fixed, and only the facts in it change. So
code fills the form from the opportunity and the company's profile, and a person approves
the result like any other draft.

Letters with a known layout (a transmittal letter) are filled from a template in code.
Any other prescribed document, and the list of expectations a bidder has of the customer,
is written by the model (`form_document_generation`) from the tender's own wording and the
recorded facts, then held to the same rules in code: no figure that is not in the supplied
material, no paragraph that merely describes a clause, and every promise routed to an
approver. A new kind of tender therefore needs no new template.

Nothing is invented. A fact that is not recorded (the nominee's name, a signatory, a
date) stays as a bracketed placeholder for the person who signs. Anything the letter
promises is saved as a commitment, so it needs an approver before it can be released.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.context import ExecutionContext

FORM_VERSION = "v2"
TRANSMITTAL_TITLE = re.compile(
    r"letter\s+of\s+transmittal|transmittal\s+letter|cover(?:ing)?\s+letter", re.IGNORECASE
)
TRANSMITTAL_TEXT = re.compile(r"letter\s+of\s+transmittal|transmittal\s+letter", re.IGNORECASE)
# A work package that covers a handful of requirements and names a letter is that letter.
# A broad package that merely mentions one among thirty requirements is not.
MAX_REQUIREMENTS = 4
APPROVER_ROLE = "EXECUTIVE_APPROVER"


@dataclass(frozen=True)
class Facts:
    customer: str
    tender: str
    reference: str | None
    bidder: str | None
    validity_days: str | None


FORM_CUE = re.compile(
    r"\b(?:letter|declaration|undertaking|certificate|affidavit|nomination|authori[sz]ation|"
    r"power\s+of\s+attorney|attestation|acknowledg\w+|conflicts?\s+of\s+interest|"
    r"non[\s-]?collusion|integrity\s+pact|anti[\s-]?bribery|statement\s+of\s+\w+|"
    r"form\s+of\s+\w+)\b",
    re.IGNORECASE,
)
EXPECTATIONS_CUE = re.compile(
    r"\bexpectations?\b.*\b(?:from|of|on)\b|\bwhat\s+we\s+expect\b|\bcustomer\s+dependenc",
    re.IGNORECASE,
)


def detect(task_title: str, tender_sources: dict[str, dict]) -> str | None:
    """Which form this task asks for, or None when it is an ordinary answer.

    Only a small work package qualifies: a broad one that merely mentions a letter among
    thirty requirements is an ordinary answer.
    """
    title = task_title or ""
    count = len(tender_sources)
    if TRANSMITTAL_TITLE.search(title):
        return "transmittal"
    if 0 < count <= MAX_REQUIREMENTS and any(
        TRANSMITTAL_TEXT.search(s.get("text") or "") for s in tender_sources.values()
    ):
        return "transmittal"
    if count > MAX_REQUIREMENTS or count == 0:
        return None
    if EXPECTATIONS_CUE.search(title):
        return "expectations"
    if FORM_CUE.search(title):
        return "form"
    return None


def transmittal(facts: Facts) -> list[tuple[str, str]]:
    """The letter as (kind, paragraph); kind 'commitment' marks a promise."""
    bidder = facts.bidder or "[bidder legal name]"
    subject = f"Submission of proposal in response to {facts.tender}"
    if facts.reference:
        subject += f" (reference {facts.reference})"
    lines: list[tuple[str, str]] = [
        ("text", f"To: {facts.customer}"),
        ("text", f"Subject: {subject}"),
        (
            "text",
            f"On behalf of {bidder}, we submit the enclosed technical and commercial "
            "proposals in response to the above tender.",
        ),
        (
            "commitment",
            "We confirm that this proposal has been prepared in accordance with the "
            "instructions given in the tender document.",
        ),
    ]
    if facts.validity_days:
        lines.append(
            (
                "commitment",
                f"This proposal remains valid for {facts.validity_days} days, as the tender "
                f"requires.",
            )
        )
    lines += [
        (
            "text",
            f"We nominate [name of nominee], [designation], {bidder}, whose specimen "
            "signature is placed below, as our authorised representative for this tender.",
        ),
        ("text", "Specimen signature of the nominee: [signature]"),
        (
            "text",
            f"Signed for and on behalf of {bidder} by [name and designation of the "
            "authorised signatory]. Date: [date].",
        ),
    ]
    return lines


def _facts(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> Facts:
    from app.models import Bid, Opportunity
    from app.services import report_extras

    bid = session.get(Bid, bid_id)
    opportunity = session.get(Opportunity, bid.opportunity_id) if bid else None
    return Facts(
        customer=(opportunity.customer_name if opportunity else None) or "[customer name]",
        tender=(opportunity.tender_name if opportunity else None) or "[tender title]",
        reference=opportunity.tender_reference if opportunity else None,
        bidder=report_extras.bidder_name(session, ctx),
        validity_days=report_extras._validity_days(session, ctx, bid_id),
    )


_INTERNAL = re.compile(r"\bR-\d{2,4}\b|\bclauses?\s+\d", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,.]*\d|\d")
_DESCRIBES_CLAUSE = re.compile(
    r"^\s*(?:clause|section|article|paragraph)\s+[\d.]+\s+(?:of\s+the\s+\w+\s+)?"
    r"(?:requires|states|invites|sets|specifies|asks|provides)",
    re.IGNORECASE,
)


def _problems(paragraphs, material: str) -> list[str]:
    """What is wrong with a model-written form, found by code."""
    seen = {n.strip(".,") for n in _NUMBER.findall(material)}
    out: list[str] = []
    for p in paragraphs:
        if _DESCRIBES_CLAUSE.search(p.text):
            out.append(f"Describes a clause instead of being the document: {p.text[:80]!r}")
        if _INTERNAL.search(p.text):
            out.append(
                f"Mentions an internal reference (requirement code or clause): {p.text[:80]!r}"
            )
        for n in _NUMBER.findall(p.text):
            if n.strip(".,") not in seen:
                out.append(f"The figure {n!r} is not in the supplied material: {p.text[:80]!r}")
    return out


def _recorded_items(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> list[str]:
    """What the bidder has recorded it depends on, customer-facing items first.

    When the assumptions register has been built, only its customer dependencies and
    commercial items are used: those are the ones the customer needs to see. Before that,
    every recorded assumption is offered and the model is asked to keep the relevant ones.
    """
    import json

    from sqlalchemy import select

    from app.models import Assumption

    try:
        from app.services import report_consolidate
        from app.services.storage import get_storage

        storage = get_storage()
        key = report_consolidate._key(ctx, bid_id, "register.json")
        if storage.exists(key):
            record = json.loads(storage.get(key))
            items = record.get("items") or []
            cats = record.get("categories") or []
            kept = [
                " ".join(t.split())
                for t, c in zip(items, cats, strict=False)
                if c == "CUSTOMER_DEPENDENCY"
            ]
            if kept:
                return kept
    except Exception:  # noqa: BLE001, S110 - fall back to the raw records
        pass
    rows = session.execute(
        select(Assumption.text).where(Assumption.org_id == ctx.org_id, Assumption.bid_id == bid_id)
    ).scalars()
    return list(dict.fromkeys(" ".join((t or "").split()) for t in rows if (t or "").strip()))


def _written(
    session: Session,
    ctx: ExecutionContext,
    *,
    kind: str,
    title: str,
    bid_id: uuid.UUID,
    tender_sources: dict[str, dict],
    gateway,
) -> list[tuple[str, str, str | None]]:
    """(style, text, approver) for each paragraph the model wrote, after code has checked it."""
    import json

    from app.core.errors import ValidationError
    from app.core.permissions import COMMITMENT_APPROVER_ROLES
    from app.gateway.gateway import build_gateway
    from app.gateway.registry import wrap_untrusted
    from app.schemas.drafting import FormDocumentResult

    facts = _facts(session, ctx, bid_id)
    payload: dict = {
        "form_title": title,
        "facts": {
            "customer": facts.customer,
            "tender": facts.tender,
            "reference": facts.reference,
            "bidder": facts.bidder,
            "bid_validity_days": facts.validity_days,
        },
        "tender_requirements": [
            {"code": s.get("code"), "text": s.get("text")} for s in tender_sources.values()
        ],
    }
    if kind == "expectations":
        payload["recorded_items"] = _recorded_items(session, ctx, bid_id)
        if not payload["recorded_items"]:
            raise ValidationError(
                "No assumptions or dependencies are recorded for this bid, so there is "
                "nothing to list. Record them first."
            )
    material = json.dumps(payload, ensure_ascii=False, default=str)
    model = gateway or build_gateway()
    last: list[str] = []
    for _attempt in range(2):
        if last:
            payload["fix_these"] = last
        result = model.generate_structured(
            "form_document_generation",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False, default=str)),
            ctx=ctx,
            session=session,
        )
        if not isinstance(result, FormDocumentResult):
            raise ValidationError("form_document_generation returned an unexpected schema.")
        last = _problems(result.paragraphs, material)
        if not last:
            roles = {str(getattr(r, "value", r)) for r in COMMITMENT_APPROVER_ROLES}
            out: list[tuple[str, str, str | None]] = []
            for p in result.paragraphs:
                if p.commitment:
                    role = (
                        p.required_approver_role
                        if p.required_approver_role in roles
                        else APPROVER_ROLE
                    )
                    out.append(("commitment", p.text.strip(), role))
                else:
                    out.append(("text", p.text.strip(), None))
            return out
    raise ValidationError(
        "The generated document did not pass the checks: " + "; ".join(last[:4]) + ". Try again."
    )


def build(
    session: Session,
    ctx: ExecutionContext,
    *,
    kind: str,
    bid_id: uuid.UUID,
    tender_sources: dict[str, dict],
    task_title: str = "",
    gateway=None,
) -> list[dict]:
    """Statements the draft path accepts, one per paragraph of the form."""
    codes = [s["code"] for s in tender_sources.values() if s.get("code")]
    if kind == "transmittal":
        paragraphs = [
            (a, b, APPROVER_ROLE if a == "commitment" else None)
            for a, b in transmittal(_facts(session, ctx, bid_id))
        ]
    elif kind in ("form", "expectations"):
        paragraphs = _written(
            session,
            ctx,
            kind=kind,
            title=task_title,
            bid_id=bid_id,
            tender_sources=tender_sources,
            gateway=gateway,
        )
    else:
        raise ValueError(f"Unknown form {kind!r}")
    rows: list[dict] = []
    for number, (style, text, role) in enumerate(paragraphs, start=1):
        row: dict = {"text": text, "paragraph_no": number}
        if kind == "expectations" and re.search(r"\bexpect", text, re.IGNORECASE):
            # Only a real expectation is an assumption on record; headings and the
            # closing line are plain text and must not enter the assumptions register.
            row["statement_class"] = "ASSUMPTION"
        elif style == "commitment":
            row["statement_class"] = "PROPOSED_COMMITMENT"
            row["required_approver_role"] = role or APPROVER_ROLE
            row["commitment_owner_user_id"] = ctx.actor_user_id
        else:
            row["statement_class"] = "HUMAN_PROVIDED_INPUT"
        if codes:
            row["requirement_codes"] = codes
        rows.append(row)
    return rows
