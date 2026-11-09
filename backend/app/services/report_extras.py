"""The parts of a proposal that no single answer writes.

The joining pass rewrites answers that already exist. A tender also asks for
things that are assembled from many answers: a two-page financial proposal with
one fixed price, and a matrix of the customer's evaluation factors against the
proposal. Those are drafted here.

The model proposes; code decides. For the price the model chooses the effort
(roles from the company's rate card, a number of days, a basis in the approved
scope); the code looks up every rate, multiplies, adds up, and splits the total
by the payment stages the company already approved. The model never writes a
figure of money. For the matrix the factors are the customer's own words, the
chapters must be real chapter titles, and the response may not add a fact.

Rate cards marked as demonstration values are used, because that is what the
company has loaded, but the result says so, and release validation refuses the
package until someone in Commercial has approved the price.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.models import (
    Document,
    DocumentBlock,
    DocumentVersion,
    GeneratedDocument,
)
from app.models.enums import DocumentSide, DocumentStream, DocumentVersionState, OutputMode
from app.schemas.report_extras import EvaluationFactors, EvaluationMatrix, FinancialDraft
from app.services import report_edit
from app.services.storage import ObjectStorage, get_storage

MAX_ATTEMPTS = 5
NUMBER = re.compile(r"\d[\d,.]*")
STAGE = re.compile(
    r"(\d{1,3})\s*(?:%|percent)\s+(?:on|after|at)\s+([^,.;]+?)(?=,|;|\.|\s+and\s+\d+\s*(?:%"
    r"|percent)|$)"
)


def _key(ctx: ExecutionContext, bid_id: uuid.UUID, name: str) -> str:
    return ObjectStorage.tenant_key(ctx.org_id, "report-extras-r1", str(bid_id), name)


def _nums(text: str) -> set[str]:
    return {n.strip(".,") for n in NUMBER.findall(text) if n.strip(".,")}


# --- reading what the company has loaded ---------------------------------------


def _split_markdown_tables(blocks) -> list:
    """Blocks as the other code reads them: a table typed as markdown text in a plain
    document (rather than a real table) becomes one row block per row, "Header: value | ...",
    exactly as a real table's rows are stored."""
    from types import SimpleNamespace

    out: list = []
    people = re.compile(
        r"^\s*[-*]\s*([^:\n]+):\s*([^.\n]+?)\.\s*May sign ([^\n]*?)"
        r"(?:\s+up to (?:[A-Z]{3}\s*)?([\d,]+))?\.(?:\s|$)",
        re.I | re.M,
    )
    for b in blocks:
        if b.table_name is None:
            found = people.findall(b.text or "")
            for title, name, what, limit in found:
                authority = what + (
                    " proposal" if re.search(r"tender|quotation|bid|proposal", what, re.I) else ""
                )
                out.append(
                    SimpleNamespace(
                        text=f"Name: {name.strip()} | Title: {title.strip()} | "
                        f"Authority: {authority} | Limit: {limit or 'none'}",
                        table_name="prose",
                        row_index=len(out),
                        ordinal=b.ordinal,
                    )
                )
        lines = (b.text or "").split("\n")
        piped = [i for i, ln in enumerate(lines) if ln.strip().startswith("|")]
        if b.table_name is not None or len(piped) < 2:
            out.append(b)
            continue
        first, last = piped[0], piped[-1]
        rows = [
            [c.strip() for c in lines[i].strip().strip("|").split("|")]
            for i in range(first, last + 1)
        ]
        header = rows[0]
        body = [r for r in rows[1:] if not all(re.fullmatch(r":?-{2,}:?", c) for c in r if c)]
        rest = "\n".join(lines[:first] + lines[last + 1 :]).strip()
        if rest:
            out.append(
                SimpleNamespace(text=rest, table_name=None, row_index=None, ordinal=b.ordinal)
            )
        for n, r in enumerate(body):
            text = " | ".join(f"{h}: {v}" for h, v in zip(header, r, strict=False))
            out.append(
                SimpleNamespace(text=text, table_name="markdown", row_index=n, ordinal=b.ordinal)
            )
    return out


def _record_field(
    session: Session, ctx: ExecutionContext, title_like: str, label: str
) -> str | None:
    """A 'Label: value' line from the company document whose title contains title_like."""
    for block in _company_block_rows(session, ctx, title_like):
        for line in (block.text or "").split("\n"):
            m = re.match(rf"^[\s#*\-]*{re.escape(label)}\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
            if m:
                return m.group(1).strip()
    return None


def _record_text(session: Session, ctx: ExecutionContext, title_like: str) -> str:
    return "\n".join((b.text or "") for b in _company_block_rows(session, ctx, title_like))


def _usable_for(session: Session, ctx: ExecutionContext, bid_id, title_like: str) -> bool:
    """False when the company record was written for another customer's bid."""
    if bid_id is None:
        return True
    from app.services import package_lint

    return not package_lint.written_for_another_bid(
        session, ctx, bid_id, _record_text(session, ctx, title_like)
    )


def proposal_reference(session: Session, ctx: ExecutionContext, bid_id=None) -> str | None:
    """The company's own reference for this proposal, when the tender carries none."""
    if not _usable_for(session, ctx, bid_id, "proposal reference"):
        return None
    return _record_field(session, ctx, "proposal reference", "Reference")


def _signing(session: Session, ctx: ExecutionContext, name: str, bid_id=None) -> tuple[str, str]:
    """The signature and date recorded for this signatory, if the company holds a record."""
    if not _usable_for(session, ctx, bid_id, "signing record"):
        return "", ""
    if (
        _record_field(session, ctx, "signing record", "Signed by") or ""
    ).casefold() != name.casefold():
        return "", ""
    return (
        _record_field(session, ctx, "signing record", "Signature") or "",
        _record_field(session, ctx, "signing record", "Date") or "",
    )


def _company_blocks(session: Session, ctx: ExecutionContext, title_like: str):
    return _split_markdown_tables(_company_block_rows(session, ctx, title_like))


def _company_block_rows(session: Session, ctx: ExecutionContext, title_like: str):
    return list(
        session.execute(
            select(DocumentBlock)
            .join(DocumentVersion, DocumentVersion.id == DocumentBlock.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentBlock.org_id == ctx.org_id,
                Document.side == DocumentSide.COMPANY,
                Document.withdrawn_at.is_(None),
                # Only the version the company currently stands behind: an older
                # version of a rate card or corporate record has been replaced.
                Document.current_version_id == DocumentVersion.id,
                func.replace(Document.title, "_", " ").ilike(f"%{title_like}%"),
                DocumentVersion.state.in_(
                    [DocumentVersionState.INDEXED, DocumentVersionState.ACTIVE]
                ),
            )
            .order_by(DocumentBlock.ordinal)
        )
        .scalars()
        .all()
    )


def rate_card(session: Session, ctx: ExecutionContext) -> dict:
    """Day rates by role from the company's rate card, and whether they are demonstration values."""
    blocks = _company_blocks(session, ctx, "rate card")
    rates: dict[str, int] = {}
    for b in blocks:
        if b.table_name is None or b.row_index is None:
            continue
        pairs = [p.split(": ", 1) for p in b.text.split(" | ")]
        if len(pairs) < 2 or any(len(p) != 2 for p in pairs):
            continue
        if pairs[0][0].strip().casefold() != "role" or "day rate" not in pairs[1][0].casefold():
            continue
        value = pairs[1][1].strip()
        if pairs[0][1].strip() and re.fullmatch(r"[\d,]+", value):
            rates[pairs[0][1].strip()] = int(value.replace(",", ""))
    text = " ".join(b.text for b in blocks)
    synthetic = bool(re.search(r"SYNTHETIC|REPLACE BEFORE USE|demonstration", text, re.IGNORECASE))
    return {"rates": rates, "synthetic": synthetic, "currency": "AED"}


def _standard_assumptions(session: Session, ctx: ExecutionContext) -> list[str]:
    out: list[str] = []
    for b in _company_blocks(session, ctx, "commercial and financial"):
        if b.table_name is not None:
            continue
        for line in b.text.split("\n"):
            line = re.sub(r"^\s*[-*]\s*", "", line).strip()
            if re.match(
                # a bank-guarantee line is not volunteered unless the tender asks for one
                r"(Prices|Currency|Customer|Third-party|Changes|Payment|Warranty)",
                line,
            ):
                out.append(line)
    return out[:12]


def _evaluation_factors(
    session: Session,
    ctx: ExecutionContext,
    bid_id: uuid.UUID,
    gateway=None,
    keep_matrix: bool = False,
) -> list[str]:
    from app.services.tender_analysis import tender_blocks

    blocks = tender_blocks(session, ctx, bid_id=bid_id)
    # The table of contents repeats the heading, so anchor on the sentence that
    # introduces the list. A parsed PDF may hold the whole list as one paragraph
    # (items separated by runs of spaces) or as one block per item.
    lead = re.compile(
        r"\b(?:will|shall)\s+(?:rate|evaluate|assess|score|rank|judge)\s+(?:the\s+|all\s+)?"
        r"(?:proposals|bids|tenders|offers|submissions|responses)[^:]*:",
        re.IGNORECASE,
    )
    factors: list[str] = []
    for i, b in enumerate(blocks):
        m = lead.search(b.text)
        if m is None:
            continue
        tail = re.split(
            r"(?:(?<=\.)\s+|\s{2,})[A-Z][\w&-]*(?:\s+[A-Z][\w&-]*){0,3}\s+reserves the right",
            b.text[m.end() :],
        )[0]
        pieces = re.split(r"\s{2,}|(?<=criteria)\s+(?=[A-Z])", tail.strip())
        if len([p for p in pieces if p.strip()]) < 2:
            pieces = []
            for nxt in blocks[i + 1 : i + 16]:
                text = " ".join(nxt.text.split())
                if not text or len(text) > 120 or "reserves the right" in text.casefold():
                    break
                pieces.append(text)
        clean = [" ".join(p.split()).lstrip("-*\u2022 ").rstrip(". ").strip() for p in pieces]
        if len(clean) >= 3 and all(0 < len(c) <= 120 for c in clean):
            factors = clean
        elif gateway is not None:
            factors = _factors_by_model(gateway, ctx, session, tail)
        break
    return [f for f in factors if keep_matrix or "matrix" not in f.casefold()]


def _factors_by_model(gateway, ctx, session, paragraph: str) -> list[str]:
    """When the list has lost its separators, a model splits it; code checks it copied."""
    from app.gateway.registry import wrap_untrusted

    flat = " ".join(paragraph.split())
    result = gateway.generate_structured(
        "evaluation_factors", wrap_untrusted(flat), ctx=ctx, session=session
    )
    if not isinstance(result, EvaluationFactors):
        return []
    out = []
    for item in result.factors:
        text = " ".join(item.split()).rstrip(". ")
        if text and text.casefold() in flat.casefold():
            out.append(text)
    return out


# --- what has been approved ----------------------------------------------------


def _rows(session, ctx, bid_id):
    from app.services.release import structure_for_bid

    structure = structure_for_bid(session, ctx, bid_id=bid_id)
    return report_edit._eligible(session, ctx, structure_id=structure.id)


def _answers(session, ctx, bid_id):
    """(section title, stream, artifact title, statements) for each approved answer."""
    from app.models import ArtifactVersion

    out = []
    for section, _map, artifact in _rows(session, ctx, bid_id):
        version = session.get(ArtifactVersion, artifact.approved_version_id)
        document = session.get(GeneratedDocument, section.target_document_id)
        out.append(
            (
                section.title,
                document.stream if document else DocumentStream.TECHNICAL,
                artifact.title,
                report_edit._statement_texts(session, ctx, version),
            )
        )
    return out


def _standard_payment_texts(session: Session, ctx: ExecutionContext) -> list[str]:
    """Lines of the company's commercial pack that set out a payment schedule."""
    return [
        b.text
        for b in _company_blocks(session, ctx, "commercial and financial")
        if re.search(r"payment", b.text, re.IGNORECASE)
    ]


def parse_payment_stages(texts: list[str]) -> list[tuple[int, str]]:
    """Payment stages like '20% on contract sign-off, 30% on build complete' as (share, event)."""
    best: list[tuple[int, str]] = []
    for text in texts:
        found = [
            (int(p), what.strip())
            for p, what in re.findall(
                r"(\d{1,3})\s*(?:%|percent)\s+(?:on|after|at)\s+([^,.;]+?)(?=,|;|\."
                r"|\s+and\s+\d+\s*(?:%|percent)|$)",
                text,
            )
        ]
        if found and sum(p for p, _ in found) == 100 and len(found) > len(best):
            best = found
    return best


# --- the financial proposal ----------------------------------------------------


def money(value: int) -> str:
    return f"{value:,}"


def build_price_text(
    lines: list[dict],
    rates: dict[str, int],
    stages: list[tuple[int, str]],
    assumptions: list[str],
    exclusions: list[str],
    *,
    currency: str,
    separate: list[dict] | None = None,
) -> tuple[str, int]:
    """The price schedule as text, with every figure calculated here. Returns (text, total)."""
    rows = []
    total = 0
    days_total = 0
    for line in lines:
        rate = rates[line["role"]]
        amount = rate * line["days"]
        total += amount
        days_total += line["days"]
        cells = [line["phase"], line["role"], str(line["days"]), money(rate), money(amount)]
        rows.append("| " + " | ".join(cells) + " |")
    table = "\n".join(
        [
            f"| Phase | Role | Days | Day rate ({currency}) | Amount ({currency}) |",
            *rows,
            f"| Total fixed price, excluding VAT | | {days_total} | | {money(total)} |",
        ]
    )
    separate = separate or []
    separate_parts: list[str] = []
    if separate:
        grouped: dict[str, list[dict]] = {}
        for line in separate:
            grouped.setdefault(line["ref"], []).append(line)
        srows = []
        for ref, group in grouped.items():
            amount_ref = sum(rates[x["role"]] * x["days"] for x in group)
            days_ref = sum(x["days"] for x in group)
            srows.append(f"| {group[0]['item']} ({ref}) | {days_ref} | {money(amount_ref)} |")
        separate_parts = [
            "### Items quoted separately",
            "As the tender asks, the following items are quoted on their own lines. "
            "They are not included in the fixed price above, and each is calculated "
            "as the effort of its roles at the same day rates.",
            "\n".join([f"| Item | Days | Amount ({currency}) |", *srows]),
        ]
    parts = [
        "### Fixed price",
        f"The overall fixed price for the project is {currency} {money(total)}, excluding VAT, "
        "covering the scope described in our technical response"
        + (", other than the items quoted separately below" if separate else "")
        + ". It is calculated as the effort "
        "of each role in each delivery phase at the company's day rates, and it has no variable or "
        "time-and-materials element.",
        table,
        *separate_parts,
        "### Payment on milestones",
    ]
    if stages:
        stage_rows = []
        allocated = 0
        for index, (share, what) in enumerate(stages):
            amount = total * share // 100 if index < len(stages) - 1 else total - allocated
            allocated += amount
            stage_rows.append(f"| {what[:1].upper() + what[1:]} | {share}% | {money(amount)} |")
        parts.append(
            "Payment falls due net 30 days from a correct invoice, against the following "
            "milestones, each tied to a defined deliverable and its acceptance."
        )
        parts.append("\n".join([f"| Milestone | Share | Amount ({currency}) |", *stage_rows]))
    else:
        parts.append(
            "Payment falls due net 30 days from a correct invoice, against milestones tied to "
            "defined deliverables and their acceptance."
        )
    if assumptions:
        parts.append("### Assumptions")
        parts.extend(assumptions)
    if exclusions:
        parts.append("### Exclusions")
        parts.extend(exclusions)
    return "\n\n".join(parts), total


def check_financial(draft: FinancialDraft, rates: dict[str, int], sources_text: str) -> list[str]:
    problems: list[str] = []
    known = {r.casefold(): r for r in rates}
    allowed_nums = _nums(sources_text)
    for i, line in enumerate(draft.lines, start=1):
        if line.role.casefold() not in known:
            problems.append(f"line {i} uses the role '{line.role}', which is not in the rate card")
        extra = _nums(line.basis) - allowed_nums - {str(line.days)}
        if extra:
            problems.append(
                f"line {i} basis has figures not in the approved scope: {sorted(extra)}"
            )
    for label, items in (("assumption", draft.assumptions), ("exclusion", draft.exclusions)):
        for i, text in enumerate(items, start=1):
            extra = _nums(text) - allowed_nums
            if extra:
                problems.append(
                    f"{label} {i} has figures not in the approved scope: {sorted(extra)}"
                )
    if len({line.phase for line in draft.lines}) > 8:
        problems.append("too many phases")
    return problems


def _separately_quoted(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> list[dict]:
    """The requirements the tender asks to have quoted on their own line."""
    from app.models import Requirement, RequirementVersion
    from app.models.enums import RequirementApplicability
    from app.services.requirement_scope import LIVE_REQUIREMENT

    out = []
    for requirement in session.execute(
        select(Requirement)
        .where(
            Requirement.org_id == ctx.org_id,
            Requirement.bid_id == bid_id,
            Requirement.priced_separately.is_(True),
            LIVE_REQUIREMENT,
            Requirement.applicability != RequirementApplicability.NOT_APPLICABLE,
        )
        .order_by(Requirement.code)
    ).scalars():
        version = session.execute(
            select(RequirementVersion).where(
                RequirementVersion.requirement_id == requirement.id,
                RequirementVersion.superseded_at.is_(None),
            )
        ).scalar_one_or_none()
        if version is not None:
            out.append(
                {"ref": requirement.code.upper(), "requirement": version.requirement_text[:500]}
            )
    return out


def check_separate(draft: FinancialDraft, rates: dict[str, int], refs: set[str]) -> list[str]:
    """Separate lines cover exactly the references the tender asked for, at rate-card roles."""
    problems: list[str] = []
    known = {r.casefold() for r in rates}
    given = {x.ref.strip().upper() for x in draft.separate}
    if refs - given:
        problems.append(f"no separate line for {sorted(refs - given)}")
    if given - refs:
        problems.append(
            f"separate lines for references the tender did not ask for: {sorted(given - refs)}"
        )
    for x in draft.separate:
        if x.role.casefold() not in known:
            problems.append(f"separate line {x.ref} uses the role '{x.role}', not in the rate card")
    return problems


_PAYMENT_WORDS = re.compile(
    r"payment|invoice|retention|performance (security|guarantee|bond)|bank guarantee|net \d+",
    re.IGNORECASE,
)
_PRICE_EXCLUSION = re.compile(
    r"licen[cs]|hosting|travel|vat|tax|third[- ]party|change|out of scope|additional scope|"
    r"support after|maintenance|translation|customer[- ]supplied|infrastructure",
    re.IGNORECASE,
)


def _terms_of_price(
    session: Session,
    ctx: ExecutionContext,
    assumptions: list[str],
    exclusions: list[str],
    separate_lines: list[dict],
) -> tuple[list[str], list[str]]:
    """What the price assumes and leaves out, kept to what is about the price.

    Payment terms are the company's approved schedule, so a drafted assumption that
    reopens them contradicts the page. An exclusion is something the price does not
    cover; the items quoted separately are the first of those, from the record.
    """
    standard = _standard_assumptions(session, ctx)
    kept = [a for a in assumptions if not _PAYMENT_WORDS.search(a)]
    for line in standard:
        if not any(line.casefold() == k.casefold() for k in kept):
            kept.append(line)
    out: list[str] = []
    seen: set[str] = set()
    named: list[str] = []
    for x in separate_lines:
        if x["ref"] in seen:
            continue
        seen.add(x["ref"])
        named.append(f"{x['item']} ({x['ref']})")
    if len(named) == 1:
        out.append(f"{named[0]} is not part of the fixed price; it is quoted separately above.")
    elif named:
        out.append(
            "The following are not part of the fixed price and are quoted separately above: "
            + "; ".join(named)
            + "."
        )
    out.extend(e for e in exclusions if _PRICE_EXCLUSION.search(e) and not _PAYMENT_WORDS.search(e))
    out.append("Work outside the scope described in our technical proposal is priced separately.")
    return kept, out


def draft_financial(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway,
    storage: ObjectStorage,
    force: bool = False,
) -> str:
    from app.gateway.registry import wrap_untrusted

    key = _key(ctx, bid_id, "financial.json")
    attempts = 0
    if storage.exists(key) and not force:
        old = json.loads(storage.get(key))
        attempts = old.get("attempts", 1)
        if "text" in old:
            return "done"
        if attempts >= MAX_ATTEMPTS:
            return "failed"
    card = rate_card(session, ctx)
    if not card["rates"]:
        storage.put(
            key,
            json.dumps({"failed": ["no rate card is loaded"], "attempts": attempts + 1}).encode(),
            content_type="application/json",
        )
        return "failed"
    answers = _answers(session, ctx, bid_id)
    pick = lambda words: [  # noqa: E731
        s
        for sec, _st, art, sts in answers
        if any(w in (sec + " " + art).casefold() for w in words)
        for s in sts
    ]
    commercial = [
        s for _sec, st, _art, sts in answers if st is DocumentStream.COMMERCIAL for s in sts
    ]
    separately = _separately_quoted(session, ctx, bid_id)
    plan_effort = [
        b.text
        for b in _company_blocks(session, ctx, "work breakdown")
        if "Role:" in b.text and "Days:" in b.text
    ]
    scope = {
        "separately_quoted": separately,
        "schedule": pick(["schedule", "milestone"])[:30],
        "team": pick(["personnel", "team", "key"])[:30],
        "approved_commercial_statements": commercial[:40],
        "standard_commercial_assumptions": _standard_assumptions(session, ctx),
        "rate_card_roles": sorted(card["rates"]),
        "chapters": sorted({sec for sec, *_ in answers}),
        "delivery_plan_effort": plan_effort,
        "how_to_phase": (
            "Phases are stages of delivering the project (for example analysis and design, "
            "build, testing, go-live and support) in the order the work happens; they are not "
            "the chapters of the tender. Size the days for a real delivery of that scope, "
            "with every role the approved answers say will work on it. When "
            "delivery_plan_effort is given it is the delivery lead's own work breakdown: use "
            "its phases, roles and days exactly, and size only the items quoted separately."
        ),
    }
    sources_text = json.dumps(scope, ensure_ascii=False)
    feedback: list[str] = []
    draft = None
    problems: list[str] = []
    for _ in range(2):
        body = dict(scope)
        if feedback:
            body["fix_these_problems"] = feedback
        result = gateway.generate_structured(
            "financial_draft",
            wrap_untrusted(json.dumps(body, ensure_ascii=False)),
            ctx=ctx,
            session=session,
            cache=not feedback,
        )
        if not isinstance(result, FinancialDraft):
            problems = ["the model returned nothing usable"]
            continue
        problems = check_financial(result, card["rates"], sources_text)
        problems += check_separate(result, card["rates"], {x["ref"] for x in separately})
        if not problems:
            draft = result
            break
        feedback = problems
    if draft is None:
        storage.put(
            key,
            json.dumps({"failed": problems, "attempts": attempts + 1}).encode(),
            content_type="application/json",
        )
        return "failed"
    canonical = {r.casefold(): r for r in card["rates"]}
    lines = [
        {"phase": line.phase, "role": canonical[line.role.casefold()], "days": line.days}
        for line in draft.lines
    ]
    stages = parse_payment_stages(_standard_payment_texts(session, ctx)) or parse_payment_stages(
        commercial
    )
    separate_lines = [
        {
            "ref": x.ref.strip().upper(),
            "item": x.item.strip(),
            "role": canonical[x.role.casefold()],
            "days": x.days,
        }
        for x in draft.separate
    ]
    assumptions, exclusions = _terms_of_price(
        session, ctx, draft.assumptions, draft.exclusions, separate_lines
    )
    text, total = build_price_text(
        lines,
        card["rates"],
        stages,
        assumptions,
        exclusions,
        currency=card["currency"],
        separate=separate_lines,
    )
    record = {
        "text": text,
        "total": total,
        "currency": card["currency"],
        "synthetic": card["synthetic"],
        "plan_based": bool(plan_effort),
        "lines": lines,
        "separate": {
            ref: sum(
                card["rates"][x["role"]] * x["days"] for x in separate_lines if x["ref"] == ref
            )
            for ref in {x["ref"] for x in separate_lines}
        },
    }
    storage.put(key, json.dumps(record).encode(), content_type="application/json")
    return "done"


def financial_record(ctx, *, bid_id, storage) -> dict | None:
    key = _key(ctx, bid_id, "financial.json")
    if not storage.exists(key):
        return None
    record = json.loads(storage.get(key))
    return record if "text" in record else None


PLAN_NOTICE = (
    "[DRAFT PRICE. The rates are the company's approved rates and the days of effort are the "
    "delivery lead's work breakdown. Commercial must approve the figure and mark the financial "
    "proposal ready before this proposal is submitted.]"
)

EFFORT_NOTICE = (
    "[DRAFT PRICE. The rates are the company's approved rates, but the days of effort were "
    "estimated by the system from the proposed scope. The delivery lead must confirm the work "
    "breakdown and Commercial must approve the figure before this proposal is submitted.]"
)

DRAFT_PRICE_NOTICE = (
    "[DRAFT PRICE. It is calculated with the company's demonstration rate card, which is marked "
    "as not an approved rate card. The Commercial lead must replace the rates and approve the "
    "figure before this proposal is submitted.]"
)


def _notice(record: dict) -> str:
    if record.get("synthetic"):
        return DRAFT_PRICE_NOTICE
    return PLAN_NOTICE if record.get("plan_based") else EFFORT_NOTICE


def financial_text(ctx, *, bid_id, storage, output_mode: OutputMode) -> str | None:
    record = financial_record(ctx, bid_id=bid_id, storage=storage)
    if record is None:
        return None
    if output_mode is OutputMode.REVIEW:
        notice = _notice(record)
        return notice + "\n\n" + record["text"]
    return record["text"]


def signatory_names(session: Session, ctx: ExecutionContext) -> list[str]:
    """Everyone the company's records authorise to sign a proposal."""
    names: list[str] = []
    for b in _company_blocks(session, ctx, "corporate records"):
        pairs = dict(p.split(": ", 1) for p in b.text.split(" | ") if ": " in p)
        if {"Name", "Authority"} <= set(pairs) and "proposal" in pairs["Authority"].casefold():
            names.append(pairs["Name"].strip())
    return names


def signers_named(text: str, names: list[str]) -> set[str]:
    """Authorised signatories a document names close to the word 'sign'."""
    found = set()
    for name in names:
        for m in re.finditer(re.escape(name), text):
            window = text[max(0, m.start() - 400) : m.end() + 400]
            if re.search(r"sign", window, re.IGNORECASE):
                found.add(name)
                break
    return found


def signer_conflict(per_document: dict[str, set[str]]) -> bool:
    """True when the documents that name a signer have no signer in common."""
    named = [names for names in per_document.values() if names]
    return len(named) > 1 and not set.intersection(*named)


def _signatory(session: Session, ctx: ExecutionContext, total: int) -> tuple[str, str]:
    """The authorised signatory with the lowest authority that still covers the price."""
    found: list[tuple[float, str, str]] = []
    everyone: list[tuple[float, str, str]] = []
    for b in _company_blocks(session, ctx, "corporate records"):
        pairs = dict(p.split(": ", 1) for p in b.text.split(" | ") if ": " in p)
        if not {"Name", "Title", "Authority", "Limit"} <= set(pairs):
            continue
        if "proposal" not in pairs["Authority"].casefold():
            continue
        limit_text = pairs["Limit"].casefold()
        if "unlimited" in limit_text:
            limit = float("inf")
        else:
            digits = re.sub(r"[^\d]", "", limit_text)
            limit = float(digits) if digits else 0.0
        everyone.append((limit, pairs["Name"].strip(), pairs["Title"].strip()))
        if limit >= total:
            found.append((limit, pairs["Name"].strip(), pairs["Title"].strip()))
    if not found:
        if not everyone:
            return "", ""
        # The price is above every individual limit: the highest signer, and the page
        # says that a board resolution is what authorises it.
        _limit, name, title = max(everyone)
        return name, f"{title} (board resolution required for a price above AED {int(_limit):,})"
    _limit, name, title = min(found)
    return name, title


def _tender_text(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> str:
    from app.services.tender_analysis import tender_blocks

    return " ".join(" ".join(b.text.split()) for b in tender_blocks(session, ctx, bid_id=bid_id))


def _appears_in(name: str, text: str, share: float) -> bool:
    name = " ".join((name or "").split()).casefold()
    if not name:
        return False
    if name in text:
        return True
    words = set(re.findall(r"[a-z]{4,}", name))
    return bool(words) and len([w for w in words if w in text]) / len(words) >= share


def tender_details(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> dict:
    """The customer, tender name and reference as recorded on the opportunity, and
    whether the tender documents themselves bear them out."""
    from app.models import Bid, Opportunity

    bid = session.get(Bid, bid_id)
    opportunity = session.get(Opportunity, bid.opportunity_id) if bid else None
    if opportunity is None:
        return {"customer": "", "tender": "", "reference": None, "verified": False}
    text = _tender_text(session, ctx, bid_id).casefold()
    # Nothing to check against when no tender text is indexed.
    # Two different questions: who the customer is, and whether the tender's formal title
    # matches. A tender can name its customer on every page and still carry a blank title.
    customer_ok = not text.strip() or _appears_in(opportunity.customer_name, text, 0.5)
    title_ok = not text.strip() or _appears_in(opportunity.tender_name, text, 0.6)
    return {
        "customer": opportunity.customer_name,
        "tender": opportunity.tender_name,
        "reference": opportunity.tender_reference,
        "customer_verified": customer_ok,
        "title_verified": title_ok,
        "reference_missing": not (opportunity.tender_reference or "").strip(),
        "verified": customer_ok and title_ok,
    }


# --- the two-page financial proposal -------------------------------------------

_NOT_FOR_THE_CUSTOMER = re.compile(
    r"\bCVs?\b|curricul|licen[cs]|finalised once|separate financial proposal|"
    r"No variable|time-and-materials|Payment is on|GPL|Any scope, start date|bill of materials|"
    r"available in the RFP",
    re.IGNORECASE,
)


def _same_point(a: str, b: str) -> bool:
    wa = set(re.findall(r"[a-z]{4,}", a.casefold()))
    wb = set(re.findall(r"[a-z]{4,}", b.casefold()))
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) > 0.25


def _pick(items: list[str], limit: int) -> list[str]:
    chosen: list[str] = []
    for item in items:
        if _NOT_FOR_THE_CUSTOMER.search(item) or any(_same_point(item, c) for c in chosen):
            continue
        chosen.append(item)
        if len(chosen) == limit:
            break
    return chosen


def _table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.split("\n"):
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def bidder_name(session: Session, ctx: ExecutionContext) -> str | None:
    for b in _company_blocks(session, ctx, "company profile"):
        m = re.match(r"Attribute:\s*Legal name\s*\|\s*Value:\s*(.+)$", b.text.strip())
        if m:
            return m.group(1).strip()
        m = re.search(r"^\s*(?:[-*]\s*)?Legal name:\s*(.+?)\s*$", b.text, re.M | re.I)
        if m:
            return m.group(1).strip()
    return None


def _validity_days(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> str | None:
    from app.services.tender_analysis import tender_blocks

    for b in tender_blocks(session, ctx, bid_id=bid_id):
        m = re.search(r"\b(\d{2,3})[- ]day", b.text)
        if m and "valid" in b.text.casefold():
            return m.group(1)
    return None


def proposal_page(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    storage: ObjectStorage,
    output_mode: OutputMode,
) -> str | None:
    """The financial proposal as the customer reads it: two pages, one price.

    Everything is taken from the drafted record (every figure there was
    calculated in code); nothing new is written. The day-rate costing that
    produced the price is for the people who approve it, so it follows the two
    pages as an internal annex in REVIEW copies and is left out of a submission.
    """
    record = financial_record(ctx, bid_id=bid_id, storage=storage)
    if record is None:
        return None
    text = record["text"]
    currency = record["currency"]
    total = record["total"]
    costing, _, rest = text.partition("### Payment on milestones")
    costing, _, separate_text = costing.partition("### Items quoted separately")
    separate_text = separate_text.strip()
    milestones, _, tail = rest.partition("### Assumptions")
    assumptions_text, _, exclusions_text = tail.partition("### Exclusions")
    cost_rows = [r for r in _table_rows(costing) if len(r) == 5][1:-1]
    phases: dict[str, int] = {}
    for phase, _role, _days, _rate, amount in cost_rows:
        phases[phase] = phases.get(phase, 0) + int(amount.replace(",", ""))
    stage_rows = _table_rows(milestones)
    signer = _signatory(session, ctx, total)
    if output_mode is not OutputMode.REVIEW:
        # The route to a board resolution is an internal matter, not a line for the customer.
        signer = (signer[0], re.sub(r"\s*\(board resolution[^)]*\)\s*$", "", signer[1]))
    details = tender_details(session, ctx, bid_id)
    unknown = "[to be confirmed against the tender]"
    days = _validity_days(session, ctx, bid_id)
    assumptions = _pick([p for p in assumptions_text.split("\n\n") if p.strip()], 5)
    exclusions = _pick([p for p in exclusions_text.split("\n\n") if p.strip()], 5)

    separate_total = sum(int(v) for v in (record.get("separate") or {}).values())
    # The cover page already names the bidder, tender and customer; the customer's copy of
    # this page keeps to the price so it stays within the tender's page limit.
    identity = [
        f"| Bidder | {bidder_name(session, ctx) or '[legal name to be confirmed]'} |",
        "| Tender | "
        + (details["tender"] if details["verified"] else unknown)
        + (f" ({details['reference']})" if details["verified"] and details["reference"] else "")
        + " |",
        f"| Customer | {details['customer'] if details['verified'] else unknown} |",
    ]
    facts = [
        "| Item | Detail |",
        *(identity if output_mode is OutputMode.REVIEW else []),
        f"| {'Fixed price for the base scope' if separate_text else 'Overall fixed price'} "
        f"| {currency} {money(total)}, excluding VAT |",
        *(
            [
                f"| Items quoted separately | {currency} {money(separate_total)}, excluding VAT |",
                f"| Total if every item is awarded | {currency} "
                f"{money(total + separate_total)}, excluding VAT |",
            ]
            if separate_text
            else []
        ),
        "| Scope | The scope described in our technical proposal |",
        "| Payment terms | Net 30 days from a correct invoice, on the milestones below |",
    ]
    if days:
        facts.append(f"| Validity | {days} days from the submission date |")
    parts = [
        "### Overall fixed price",
        "We offer the scope described in our technical proposal for a fixed price of "
        f"{currency} {money(total)}, excluding VAT. There is no variable element in it."
        + (
            " The items the tender asks to have quoted separately are priced below and are "
            "not included in this price."
            if separate_text
            else ""
        ),
        "\n".join(facts),
        "### Price by delivery phase",
        "\n".join(
            [
                f"| Phase | Amount ({currency}) |",
                *[f"| {name} | {money(amount)} |" for name, amount in phases.items()],
                f"| Total, excluding VAT | {money(total)} |",
            ]
        ),
        *(["### Items quoted separately", separate_text] if separate_text else []),
        "### Payment on milestones",
        "Each milestone is payable on the customer's acceptance of the deliverable it names."
        + (
            " The milestones apply to the base scope. Each separately quoted item is invoiced "
            "once, on the customer's acceptance of that item."
            if separate_text
            else ""
        ),
        "\n".join("| " + " | ".join(r) + " |" for r in stage_rows),
    ]
    if assumptions:
        parts += ["### Assumptions", *assumptions]
    if exclusions:
        parts += ["### Exclusions", *exclusions]
    parts += [
        "### Authorised signatory",
        "\n".join(
            [
                "| Name | Title | Signature | Date |",
                f"| {signer[0]} | {signer[1]} | "
                + " | ".join(_signing(session, ctx, signer[0], bid_id))
                + " |",
            ]
        ),
    ]
    page = "\n\n".join(parts)
    if output_mode is OutputMode.REVIEW:
        page = _notice(record) + "\n\n" + page
        page += (
            "\n\n### Annex (internal, remove before submission): how the price was calculated\n\n"
            + costing.replace("### Fixed price", "").strip()
            + ("\n\n### Items quoted separately\n\n" + separate_text if separate_text else "")
        )
    return page


# --- the evaluation matrix -----------------------------------------------------


def check_matrix(
    rows: list,
    factors: dict[str, str],
    chapters: dict[str, str],
) -> list[str]:
    problems: list[str] = []
    known = {t.casefold(): t for t in chapters}
    blob = " ".join(chapters.values())
    allowed = _nums(blob)
    names = set(re.findall(r"[A-Z][A-Za-z0-9\-]{2,}", blob + " " + " ".join(chapters)))
    for row in rows:
        ref = row.factor_ref.strip().upper()
        if ref not in factors:
            problems.append(f"{row.factor_ref} is not one of the factors")
            continue
        bad = [c for c in row.chapters if c.casefold() not in known]
        if bad:
            problems.append(f"{ref} names chapters that do not exist: {bad[:3]}")
        extra = _nums(row.response) - allowed
        if extra:
            problems.append(f"{ref} response has figures not in the chapters: {sorted(extra)}")
        for name in re.findall(r"(?<=[a-z0-9,;)] )[A-Z][A-Za-z0-9\-]{2,}", row.response):
            if name not in names:
                problems.append(f"{ref} response introduces '{name}'")
    answered = {r.factor_ref.strip().upper() for r in rows}
    for ref in factors:
        if ref not in answered:
            problems.append(f"{ref} was left out")
    return problems


def build_matrix_text(rows: list, factors: dict[str, str], chapters: dict[str, str]) -> str:
    known = {t.casefold(): t for t in chapters}
    lines = ["| Evaluation factor | Where we answer it | How we respond |"]
    by_ref = {r.factor_ref.strip().upper(): r for r in rows}
    for ref, factor in factors.items():
        if re.search(r"sample", factor, re.IGNORECASE):
            # Samples are existing work. What the bidder will produce later is not a
            # sample, whatever the model says. No sample of work is held or attached by
            # the system, so the row says that rather than claiming one is supplied.
            lines.append(
                f"| {factor} | Samples of work (not yet attached) | No sample of work is "
                "attached to this copy. Approved samples must be supplied by the company "
                "before submission. |"
            )
            continue
        row = by_ref[ref]
        where = "; ".join(known[c.casefold()] for c in row.chapters if c.casefold() in known)
        lines.append(f"| {factor} | {where} | {' '.join(row.response.split())} |")
    return "\n".join(lines)


def draft_matrix(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway,
    storage: ObjectStorage,
    force: bool = False,
) -> str:
    from app.gateway.registry import wrap_untrusted

    key = _key(ctx, bid_id, "matrix-r4.json")
    attempts = 0
    if storage.exists(key) and not force:
        old = json.loads(storage.get(key))
        attempts = old.get("attempts", 1)
        if "text" in old:
            return "done"
        if attempts >= MAX_ATTEMPTS:
            return "failed"
    factor_list = _evaluation_factors(session, ctx, bid_id, gateway)
    if not factor_list:
        return "none"
    factors = {f"F{i}": f for i, f in enumerate(factor_list, start=1)}
    chapters: dict[str, str] = {}
    for section, _map, artifact in _rows(session, ctx, bid_id):
        path = _key_for_artifact(ctx, bid_id, artifact)
        if not storage.exists(path):
            continue
        record = json.loads(storage.get(path))
        body = [p["text"] for p in record.get("paragraphs", []) if p.get("kind") == "BODY"][:4]
        if body:
            chapters[f"{section.title} / {artifact.title}"] = " ".join(t[:260] for t in body)
    # The company's own project references, as they stand now: past performance is
    # shown from these records, and the tender's chapters alone cannot show it.
    for ref_title, label in (
        ("project reference", "Company project reference"),
        ("total cost of ownership", "Company total cost of ownership schedule"),
        ("work sample", "Company work samples"),
    ):
        grouped: dict[str, list[str]] = {}
        for b in _company_blocks(session, ctx, ref_title):
            grouped.setdefault(str(getattr(b, "document_version_id", "") or "refs"), []).append(
                b.text.strip()
            )
        for n, texts in enumerate(grouped.values(), start=1):
            chapters[f"{label} {n}"] = " ".join(texts)[:700]
    if not chapters:
        return "none"
    payload = {
        "factors": factors,
        "chapters": [{"title": t, "excerpt": x} for t, x in chapters.items()],
    }
    feedback: list[str] = []
    problems: list[str] = []
    for _ in range(2):
        request: dict[str, Any] = dict(payload)
        if feedback:
            request["fix_these_problems"] = feedback
        result = gateway.generate_structured(
            "evaluation_matrix",
            wrap_untrusted(json.dumps(request, ensure_ascii=False)),
            ctx=ctx,
            session=session,
            cache=not feedback,
        )
        if not isinstance(result, EvaluationMatrix):
            problems = ["the model returned nothing usable"]
            continue
        problems = check_matrix(result.rows, factors, chapters)
        if not problems:
            text = build_matrix_text(result.rows, factors, chapters)
            storage.put(key, json.dumps({"text": text}).encode(), content_type="application/json")
            return "done"
        feedback = problems
    storage.put(
        key,
        json.dumps({"failed": problems, "attempts": attempts + 1}).encode(),
        content_type="application/json",
    )
    return "failed"


def _key_for_artifact(ctx, bid_id, artifact) -> str:
    return report_edit._key(ctx, bid_id, f"{artifact.approved_version_id}.json")


def complete_matrix(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID, text: str) -> str:
    """Add a row for every evaluation factor the tender lists that the table lacks.

    The drafted table leaves out the comparison matrix (a separate deliverable) and
    can lose a factor the tender writes as a loose last line. Rows are fixed wording,
    nothing a model wrote, and say plainly what is still to come.
    """
    from app.services import report_attachments
    from app.services.tender_analysis import tender_blocks

    # A matrix drafted earlier is cached with its old sample row, so that row is always
    # rewritten here, from what is actually attached: fixed wording, nothing a model wrote.
    def _cleared(group: str) -> list[str]:
        return [
            a.title
            for a in report_attachments.selected(session, ctx, bid_id)
            if a.group == group and a.cleared
        ]

    samples = _cleared("Work samples")
    if samples:
        sample_row = (
            f"| Samples of work | {samples[0]} (attached) | The work samples portfolio is "
            "attached. It presents extracts from delivered projects (screens, workflow design, "
            "test cases, load test summary and administration screens), with customer names "
            "and personal data removed. |"
        )
    else:
        sample_row = (
            "| Samples of work | Samples of work (not yet attached) | No sample of work is "
            "attached to this copy. Approved samples must be supplied by the company "
            "before submission. |"
        )
    tco = _cleared("Total cost of ownership schedule")
    out_lines = []
    for line in text.split("\n"):
        if re.match(r"\|\s*samples of work\s*\|", line, re.IGNORECASE):
            line = sample_row
        elif (
            "Separate deliverable: comparison matrix" in line
            or "This table is our fair comparison" in line
        ) and line.startswith("|"):
            factor_cell = line.split("|")[1].strip()
            line = (
                f"| {factor_cell} | This table | This table is our fair comparison "
                "matrix. It sets each Jadwa evaluation factor against the part of this "
                "proposal that answers it and how we respond, and the attachments listed in "
                "the Attachments appendix carry the evidence. |"
            )
        elif (
            tco
            and re.search(r"total cost of ownership", line, re.IGNORECASE)
            and line.startswith("|")
        ):
            cells = line.split("|")
            if len(cells) >= 5:
                reply = cells[-2]
                reply = re.sub(
                    r"No price, rate or cost figure is evidenced[^.;]*[.,;]?\s*", "", reply
                )
                reply = re.sub(
                    r"[,;]?\s*(?:and\s+)?the proposal does not set out a total cost of ownership "
                    r"assessment\.?",
                    "",
                    reply,
                    flags=re.IGNORECASE,
                )
                reply = (
                    reply.rstrip(" .")
                    + f". A five-year total cost of ownership schedule is attached ({tco[0]}). "
                )
                cells[-2] = reply
                line = "|".join(cells)
        out_lines.append(line)
    text = "\n".join(out_lines)
    factors = _evaluation_factors(session, ctx, bid_id, keep_matrix=True)
    blob = " ".join(b.text for b in tender_blocks(session, ctx, bid_id=bid_id))
    if not any("expertise" in f.casefold() for f in factors) and re.search(
        r"technical expertise\s*/\s*experience of bidder", blob, re.IGNORECASE
    ):
        factors.append("Technical expertise/experience of bidder")
    if not any("matrix" in f.casefold() for f in factors) and re.search(
        r"fair compar\w*\s+matrix", blob, re.IGNORECASE
    ):
        factors.append("Fair comparison matrix on the customer's criteria")
    have = text.casefold()
    rows = []
    for factor in factors:
        if factor.casefold()[:30] in have:
            continue
        if "matrix" in factor.casefold():
            rows.append(
                f"| {factor} | This table | This table is our fair comparison "
                "matrix. It sets each Jadwa evaluation factor against the part of this "
                "proposal that answers it and how we respond, and the attachments listed in "
                "the Attachments appendix carry the evidence. |"
            )
        elif "expertise" in factor.casefold() or "experience" in factor.casefold():
            rows.append(
                f"| {factor} | Experience evidence still to be supplied | Approved CVs and "
                "supporting experience evidence are required before submission. |"
                if report_attachments.fixture_titles(session, ctx, bid_id)
                else f"| {factor} | Comparable delivery and key-personnel chapters | Answered "
                "in the comparable-delivery and key-personnel sections; the CVs and "
                "supporting evidence are separate attachments. |"
            )
    return text if not rows else text + "\n" + "\n".join(rows)


def matrix_text(ctx, *, bid_id, storage) -> str | None:
    key = _key(ctx, bid_id, "matrix-r4.json")
    if not storage.exists(key):
        return None
    record = json.loads(storage.get(key))
    return record.get("text")


def draft_extras(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    force: bool = False,
    gateway=None,
    storage: ObjectStorage | None = None,
) -> dict:
    from app.gateway.gateway import build_gateway

    storage = storage or get_storage()
    gateway = gateway or build_gateway()
    financial = draft_financial(
        session, ctx, bid_id=bid_id, gateway=gateway, storage=storage, force=force
    )
    matrix = draft_matrix(
        session, ctx, bid_id=bid_id, gateway=gateway, storage=storage, force=force
    )
    reasons = []
    for name, status in (("financial", financial), ("matrix", matrix)):
        if status == "failed":
            path = _key(ctx, bid_id, "financial.json" if name == "financial" else "matrix-r4.json")
            try:
                reasons.append(
                    f"{name}: " + "; ".join(json.loads(storage.get(path)).get("failed", [])[:4])
                )
            except Exception:  # noqa: BLE001
                reasons.append(f"{name}: failed")
    return {"financial": financial, "matrix": matrix, "reasons": reasons}
