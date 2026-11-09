"""The compliance matrix: every requirement the bidder owns, and where it is answered.

Built from records, not written by a model. A requirement is listed when it is
live, the bidder's to answer and applicable. "Answered" means an approved answer is
linked to it and the chapter holding it is named; that says text exists, not that
evidence or an attachment does, so the status also says what is still outstanding.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.models import (
    ArtifactRequirement,
    ArtifactStatement,
    ProposalSection,
    Requirement,
    RequirementAssessment,
    RequirementVersion,
    ResponseArtifact,
    SectionArtifactMap,
)
from app.models.enums import (
    ArtifactState,
    BidReadiness,
    ObligationOwner,
    RequirementApplicability,
    StatementClass,
)
from app.services.requirement_scope import LIVE_REQUIREMENT

TEXT_LIMIT = 190
# A clause that asks the bidder to PROVIDE something a reader can check: a reference, a
# CV, a certificate, a plan, a price. A promise to provide it is not the thing itself.
EVIDENCE_DEMAND = re.compile(
    r"\b(?:references?|testimonials?|letters?\s+of|r[eé]sum[eé]s?|CVs?|curricul\w+|certificat\w+|"
    r"certified|visa|org(?:ani[sz]ation(?:al)?)?\s+chart|project\s+plan|work\s+plan|schedule|"
    r"milestones?|price|pricing|BOQ|bill\s+of\s+quantit\w+|cost\s+breakdown|brochures?|"
    r"specifications?|licen[cs]\w+|hardware|software|screening|experience|track\s+record|"
    r"similar\s+(?:assignment|project)s?|(?:recent|regional|past|previous|related"
    r"|completed)\s+projects?|"
    r"projects?\s+(?:completed|delivered|executed)|list\s+of|contact\s+details|out-of-pocket"
    r"|expenses)\b",
    re.IGNORECASE,
)


def _stage(text: str) -> str:
    from app.services.package_lint import stage_of

    stage = stage_of(text)
    if stage == "BID" and POST_AWARD_CLAUSE.search(text) and not BID_SUBMIT.search(text):
        return "POST_AWARD"
    return stage


BID_SUBMIT = re.compile(r"\bsubmit\b|\bwell[\s-]organi[sz]ed\b|\bplan\b", re.IGNORECASE)
POST_AWARD_CLAUSE = re.compile(
    r"\b(?:after|upon|following)\s+(?:the\s+)?(?:contract\s+)?(?:award\w*|signing|signature"
    r"|execution)\b|\bpost[\s-]*award\b|"
    r"\b(?:after|upon)\s+(?:project\s+)?(?:completion|go[\s-]*live)\b",
    re.IGNORECASE,
)
BLANK_CLAUSE = re.compile(r"\.{6,}|…{2,}|_{5,}")
SIGNING = re.compile(r"\b(sign(?:ed|ature|ing)?|nda|non-disclosure|authori[sz]ed)\b", re.IGNORECASE)


def _short(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= TEXT_LIMIT:
        return text
    return text[:TEXT_LIMIT].rsplit(" ", 1)[0] + "..."


def _has_evidence_answer(session: Session, ctx: ExecutionContext, requirement) -> bool:
    """Does an approved answer to this requirement include a fact that rests on evidence?"""
    rows = session.execute(
        select(ArtifactStatement.requirement_codes)
        .join(
            ResponseArtifact,
            ResponseArtifact.approved_version_id == ArtifactStatement.artifact_version_id,
        )
        .join(ArtifactRequirement, ArtifactRequirement.artifact_id == ResponseArtifact.id)
        .where(
            ArtifactRequirement.requirement_id == requirement.id,
            ResponseArtifact.state == ArtifactState.APPROVED,
            ArtifactStatement.statement_class == StatementClass.EVIDENCE_BACKED_FACT,
        )
    ).all()
    for (codes,) in rows:
        if not codes or requirement.code in codes:
            return True
    return False


ASKS_FOR_DOCUMENT = re.compile(
    r"\b(?:provide|attach|submit|include|enclose|furnish|supply|contain|list\s+of|copies"
    r"|copy\s+of|should\s+have)\b",
    re.IGNORECASE,
)
# A clause that only has the bidder accept a tender term asks for no document.
ACCEPTANCE_ONLY = re.compile(
    r"\b(?:accept(?:s|ed|ance)?|agree[sd]?|acknowledge[sd]?|bound\s+by|binding)\b", re.IGNORECASE
)

CUSTOMER_TERM_IMPACT = re.compile(
    r"\b(?:costs?|price|prices|pay|paid|payment|payable|liab\w+|damages|compensat\w+|cancel\w*"
    r"|terminat\w+|"
    r"arbitrat\w+|disput\w+|incomplete|disqualif\w+|reject\w*|grand\s+total|penalt\w+|failure\s+to|"
    r"governing\s+law|jurisdiction)\b",
    re.IGNORECASE,
)
_LINK_GENERIC = {
    "shall",
    "which",
    "their",
    "there",
    "other",
    "these",
    "those",
    "would",
    "should",
    "bidder",
    "tender",
    "customer",
    "document",
    "proposal",
    "provide",
    "services",
    "service",
    "within",
    "under",
    "after",
    "before",
    "total",
    "project",
    "order",
    "value",
    "with",
    "from",
    "that",
    "vendor",
    "supplier",
    "contractor",
    "firm",
    "tenderer",
}


def _link_stems(text: str) -> set[str]:
    return {w[:4] for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _LINK_GENERIC}


def post_award_links(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> dict[str, str]:
    """{requirement code: deliverable name} for requirements that ARE a post-award deliverable
    (the performance bond clause, the vendor-representative clause), so the matrix and the
    deliverable register give the same answer for the same obligation."""
    from app.models import Deliverable
    from app.services import package_lint

    deliverables = [
        d.name
        for d in session.execute(
            select(Deliverable).where(
                Deliverable.org_id == ctx.org_id,
                Deliverable.bid_id == bid_id,
                Deliverable.is_mandatory.is_(True),
                Deliverable.duplicate_of_deliverable_id.is_(None),
            )
        ).scalars()
        if package_lint.deliverable_stage(d.name) == "POST_AWARD"
    ]
    if not deliverables:
        return {}
    out: dict[str, str] = {}
    for r in session.execute(
        select(Requirement).where(
            Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id, LIVE_REQUIREMENT
        )
    ).scalars():
        v = session.get(RequirementVersion, r.current_version_id) if r.current_version_id else None
        text = (v.requirement_text if v else "") or ""
        from app.services.package_lint import STRONG_BID_CUE

        if STRONG_BID_CUE.search(text):
            continue
        have = _link_stems(text)
        # What the requirement is ABOUT is said in its first words; a bond mentioned later
        # in a clause about who may sign is not the bond clause.
        subject = _link_stems(" ".join(text.split()[:14]))
        best, best_score = None, 0.0
        for name in deliverables:
            head = re.split(r"\s+(?:with|including|containing|to\s+be)\b|[(,:;]", name)[0]
            want = _link_stems(head)
            if len(want) < 2:
                continue
            score = 0.0
            shared = subject & want
            if len(shared) >= 2 and len(shared) / len(want) >= 0.66:
                score = 1.5 + len(shared) / len(want) + 0.01 * len(shared)
            else:
                shared = have & want
                if len(shared) >= 2 and len(shared) / len(want) >= 0.75:
                    score = len(shared) / len(want) + 0.01 * len(shared)
            if score > best_score:
                best, best_score = name, score
        if best:
            out[r.code] = best
    return out


def _deviation_quotes(
    session: Session, ctx: ExecutionContext, bid_id: uuid.UUID, code: str = "TERMS_DEVIATION"
) -> list[str]:
    """The tender wording that our own terms deviate from (or may deviate from, for
    TERMS_AMBIGUITY), as last reported by validation."""
    try:
        from app.models import Package as ReleasePackage
        from app.models import ValidationRun
        from app.services import release as release_service

        # Only the latest validated package speaks: earlier packages may have classified a
        # clause differently, and their findings must not label today's matrix.
        out = []
        for pkg_id in session.execute(
            select(ReleasePackage.id)
            .join(ValidationRun, ValidationRun.package_id == ReleasePackage.id)
            .where(ReleasePackage.org_id == ctx.org_id, ReleasePackage.bid_id == bid_id)
            .order_by(ReleasePackage.version_no.desc())
            .limit(1)
        ).scalars():
            for f in release_service.validation_findings(session, ctx, package_id=pkg_id):
                d = f.detail
                if f.check_code != code:
                    continue
                q = (
                    " ".join(
                        re.sub(r"[‘’“”\"'`]", "", (d or {}).get("second_quote", ""))
                        .casefold()
                        .split()
                    )
                    if isinstance(d, dict)
                    else ""
                )
                if len(q) >= 20:
                    out.append(q[:70])
        return list(dict.fromkeys(out))
    except Exception:  # noqa: BLE001
        return []


def matrix_rows(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    quoted: frozenset[str] = frozenset(),
) -> list[tuple[str, str, str, str]]:
    """(reference, requirement, status, where answered) for each requirement."""
    requirements = [
        r
        for r in session.execute(
            select(Requirement)
            .where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                LIVE_REQUIREMENT,
                Requirement.applicability != RequirementApplicability.NOT_APPLICABLE,
            )
            .order_by(Requirement.code)
        ).scalars()
    ]
    links = post_award_links(session, ctx, bid_id)
    deviations = _deviation_quotes(session, ctx, bid_id)
    ambiguities = _deviation_quotes(session, ctx, bid_id, "TERMS_AMBIGUITY")

    def _norm_text(text: str) -> str:
        return " ".join(re.sub(r"[‘’“”\"'`]", "", text or "").casefold().split())

    def _deviates(text: str) -> bool:
        t = _norm_text(text)
        return bool(t) and any(q in t or t[:70] in q for q in deviations)

    def _may_deviate(text: str) -> bool:
        t = _norm_text(text)
        return bool(t) and any(q in t or t[:70] in q for q in ambiguities)

    DEVIATION = "DEVIATION: legal decision required (our terms differ from this clause)"
    POSSIBLE = "POSSIBLE DEVIATION: legal review required (the tender wording is general)"

    rows = []
    # Company documents that are attached to this bid: a requirement whose wording matches one
    # is shown with that evidence and what is wrong with it, not as a bare gap.
    try:
        from app.services import report_attachments as _ra

        _evidence = _ra.selected(session, ctx, bid_id)
        _ref_gaps = any(_ra.reference_gaps(session, ctx, bid_id).values())
        _withheld = _ra.withheld_documents(session, ctx, bid_id)
        _group_kind = {title: kind for title, kind in _ra.GROUPS}
    except Exception:  # noqa: BLE001 - the matrix still stands without the cross-reference
        _evidence, _ref_gaps, _withheld, _group_kind, _ra = [], False, [], {}, None  # type: ignore[assignment]

    def _withheld_reason(text: str) -> str:
        """Why a requirement has no attached document although the company holds one."""
        if _ra is None or not ASKS_FOR_DOCUMENT.search(text or ""):
            return ""
        for _title, group, reason in _withheld:
            kind = _group_kind.get(group)
            pattern = _ra.ASKED_FOR.get(kind) if kind else None
            if pattern is not None and pattern.search(text or ""):
                return f"the only {group.lower()} held was withheld ({reason})"
        return ""

    try:
        _mapping = _ra.evidence_mapping(session, ctx, bid_id) if _ra is not None else {}
    except Exception:  # noqa: BLE001
        _mapping = {}

    def _evidence_status(code: str):
        """(status, attached titles) from the one canonical mapping; (None, []) when none."""
        hits = _mapping.get(code) or []
        if not hits:
            return None, []
        titles = list(dict.fromkeys(h["title"] for h in hits))
        if all(h["role"] == "not_applicable" for h in hits):
            fixture = any(h["submission_status"] == "BLOCKED_FIXTURE" for h in hits)
            return (
                "Not applicable while no sub-contracting is proposed"
                + ("; the declaration is test data and must be replaced" if fixture else ""),
                titles,
            )
        hits = [h for h in hits if h["role"] != "not_applicable"]
        problems = []
        shortfalls = list(dict.fromkeys(h["shortfall"] for h in hits if h.get("shortfall")))
        if shortfalls:
            problems.append("; ".join(shortfalls))
        if any(h["submission_status"] == "BLOCKED_FIXTURE" for h in hits):
            problems.append("it is test data")
        if any(h["submission_status"] == "BLOCKED_PROVENANCE_UNVERIFIED" for h in hits):
            problems.append("nobody has attested it is genuine")
        if any(h["incomplete"] for h in hits):
            problems.append("it is incomplete")
        if problems:
            return "Evidence attached, but " + " and ".join(
                problems
            ) + "; it cannot satisfy a submission", titles
        return "Evidence attached; sufficiency not verified", titles

    for requirement in requirements:
        owner = getattr(requirement, "obligation_owner", None) or ObligationOwner.BIDDER
        version = (
            session.execute(
                select(RequirementVersion)
                .where(
                    RequirementVersion.requirement_id == requirement.id,
                    RequirementVersion.superseded_at.is_(None),
                )
                .order_by(RequirementVersion.version_no.desc())
            )
            .scalars()
            .first()
        )
        sections = (
            session.execute(
                select(ProposalSection.title)
                .join(SectionArtifactMap, SectionArtifactMap.section_id == ProposalSection.id)
                .join(ResponseArtifact, ResponseArtifact.id == SectionArtifactMap.artifact_id)
                .join(ArtifactRequirement, ArtifactRequirement.artifact_id == ResponseArtifact.id)
                .where(
                    ArtifactRequirement.requirement_id == requirement.id,
                    ResponseArtifact.state == ArtifactState.APPROVED,
                )
                .order_by(ProposalSection.order_index)
            )
            .scalars()
            .all()
        )
        titles = list(dict.fromkeys(sections))
        if owner is not ObligationOwner.BIDDER:
            # A customer right or term is out of the matrix unless it changes what we offer or
            # accept (costs, payment, liability, cancellation, disputes, completeness).
            if version is None:
                continue
            binding = bool(CUSTOMER_TERM_IMPACT.search(version.requirement_text or ""))
            rows.append(
                (
                    requirement.code + (f" ({version.clause_ref})" if version.clause_ref else ""),
                    _short(version.requirement_text or ""),
                    DEVIATION
                    if _deviates(version.requirement_text or "")
                    else POSSIBLE
                    if _may_deviate(version.requirement_text or "")
                    else "Acknowledged: tender term that binds our offer (no deliverable)"
                    if binding
                    else "Noted: customer right or obligation (no bidder deliverable)",
                    "Accepted by bidding; deviations are listed in the assumptions",
                )
            )
            continue
        readiness = session.execute(
            select(RequirementAssessment.bid_readiness).where(
                RequirementAssessment.requirement_id == requirement.id,
                RequirementAssessment.is_current.is_(True),
            )
        ).scalar()
        if not titles:
            status = "Open"
        elif version is not None and BLANK_CLAUSE.search(version.requirement_text or ""):
            # The tender's own wording is an unfinished blank ("a complete solution to ......").
            # Nothing can be complied with until the customer completes it.
            status = "Clarification required"
        elif (
            version is not None
            and not requirement.priced_separately
            and (
                _stage(version.requirement_text or "") == "POST_AWARD" or requirement.code in links
            )
        ):
            # Asked for after award; the bid answers it with a commitment, which the text carries.
            status = "Post-award obligation (commitment in text)"
        elif (
            version is not None
            and ACCEPTANCE_ONLY.search(version.requirement_text or "")
            and not ASKS_FOR_DOCUMENT.search(version.requirement_text or "")
            and not requirement.priced_separately
            and not requirement.is_conditional
        ):
            # Accepting a tender term is a commitment, not a document; our own terms only matter
            # if they deviate, and deviations are listed separately.
            status = (
                "Acknowledged: tender term that binds our offer; "
                "any deviation is listed in the assumptions"
            )
        elif (
            version is not None
            and EVIDENCE_DEMAND.search(version.requirement_text or "")
            and not requirement.priced_separately
            and not requirement.is_conditional
            and not _has_evidence_answer(session, ctx, requirement)
        ):
            # The answer only promises it. Nothing in the package shows it.
            status = "Missing before submission: evidence or attachment"
        elif requirement.priced_separately and requirement.code in quoted:
            status = "Priced separately (price draft, not approved)"
        elif requirement.priced_separately:
            status = "Priced separately (quotation required)"
        elif requirement.is_conditional:
            status = "Conditional"
        elif readiness is BidReadiness.REQUIRES_SIGNATURE:
            # The assessment says "needs a signed item". That is only the first
            # thing to chase when the clause itself is about signing; a CV pack or
            # a test report is an attachment first.
            wording = version.requirement_text if version else ""
            if SIGNING.search(wording):
                status = "Missing before submission: signed copy"
            else:
                status = "Missing before submission: evidence or attachment"
        elif readiness is BidReadiness.NEEDS_COMPANY_CONFIRMATION:
            status = "Pending company confirmation"
        elif readiness is BidReadiness.NEEDS_CLIENT_CLARIFICATION:
            status = "Clarification required"
        elif readiness is BidReadiness.BLOCKED:
            status = "Not yet answerable"
        else:
            status = "Present in text; evidence not verified"
        if version is not None and _deviates(version.requirement_text or ""):
            status = DEVIATION
        elif version is not None and _may_deviate(version.requirement_text or ""):
            status = POSSIBLE
        reference = requirement.code + (
            f" ({version.clause_ref})" if version and version.clause_ref else ""
        )
        found = None
        if version is not None and (
            status.startswith("Missing before submission")
            or status.startswith("Present in text")
            or status == "Conditional"
        ):
            new_status, attached = _evidence_status(requirement.code)
            if new_status:
                status, found = new_status, attached
        if found is None and version is not None and status.startswith("Missing before submission"):
            why = _withheld_reason(version.requirement_text or "")
            if why:
                status = f"Missing before submission: {why}"
        rows.append(
            (
                reference,
                _short(version.requirement_text) if version else "",
                status,
                "; ".join(titles + [f"Attachment: {t}" for t in (found or [])])
                if (titles or found)
                else "No approved answer yet",
            )
        )
    return rows


def customer_status(status: str) -> str:
    """What the customer sees. A promise to provide something later is never "answered":
    the status says what is missing, and the same words are used inside and outside."""
    if status == "Conditional":
        return "Comply subject to stated dependency"
    if status.startswith("Priced separately"):
        return "Priced separately"
    return status


def matrix_text(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    customer: bool = False,
    quoted: frozenset[str] = frozenset(),
) -> str | None:
    rows = matrix_rows(session, ctx, bid_id=bid_id, quoted=quoted)
    if customer:
        rows = [(a, b, customer_status(c), d) for a, b, c, d in rows]
    if not rows:
        return None
    lines = ["| Reference | Requirement | Status | Response location |"]
    for reference, text, status, where in rows:
        cells = [c.replace("|", "/") for c in (reference, text, status, where)]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


_BIDDER_CUE = re.compile(
    r"\b(?:the\s+)?(?:successful\s+|selected\s+|winning\s+)?(?:bidder|supplier|contractor|vendor"
    r"|firm|tenderer|proposer|respondent)"
    r"(?:'s|s')?\s+(?:shall|must|will|should|is\s+(?:required|requested|expected|obliged)"
    r"|are\s+(?:required|requested|expected))\b|"
    r"\b(?:inception|progress|final)\s+report\b|\b(?:performance|advance[\s-]*payment)\s+(?:bond"
    r"|guarantee)\b",
    re.IGNORECASE,
)


def post_award_rows(
    session: Session, ctx: ExecutionContext, bid_id: uuid.UUID
) -> list[tuple[str, str]]:
    """Requirements the matrix answers by a commitment now (the same decision, listed once)."""
    return [
        (ref.split(" ")[0], text)
        for ref, text, status, _ in matrix_rows(session, ctx, bid_id=bid_id)
        if status.startswith("Post-award obligation")
    ]


def matrix_exclusions(
    session: Session, ctx: ExecutionContext, bid_id: uuid.UUID
) -> list[tuple[str, str, str]]:
    """(code, reason, clause text) for every requirement code missing from the matrix: never
    extracted (a gap in the numbering), the customer's own obligation, marked not applicable, or
    no longer live. The reason `owner needs review` marks a customer-owned clause whose wording
    still asks the bidder to produce something."""
    shown = {row[0].split(" ")[0] for row in matrix_rows(session, ctx, bid_id=bid_id)}
    everything = list(
        session.execute(
            select(Requirement).where(
                Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id
            )
        ).scalars()
    )
    live = set(
        r.id
        for r in session.execute(
            select(Requirement).where(
                Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id, LIVE_REQUIREMENT
            )
        ).scalars()
    )

    def clause(r) -> str:
        v = session.get(RequirementVersion, r.current_version_id) if r.current_version_id else None
        return _short((v.requirement_text if v else "") or "")

    out: list[tuple[str, str, str]] = []
    present: set[int] = set()
    for r in everything:
        m = re.search(r"(\d+)$", r.code or "")
        if m:
            present.add(int(m.group(1)))
        if r.code in shown:
            continue
        text = clause(r)
        if r.id not in live:
            out.append((r.code, "superseded or withdrawn", text))
        elif r.applicability == RequirementApplicability.NOT_APPLICABLE:
            out.append((r.code, "marked not applicable", text))
        elif (
            getattr(r, "obligation_owner", None) or ObligationOwner.BIDDER
        ) is not ObligationOwner.BIDDER:
            why = (
                "owner needs review"
                if _BIDDER_CUE.search(text)
                else "the customer's own obligation"
            )
            out.append((r.code, why, text))
        else:
            out.append((r.code, "not shown", text))
    if present:
        prefix = next((re.sub(r"\d+$", "", r.code) for r in everything if r.code), "R-")
        for n in range(min(present), max(present) + 1):
            if n not in present:
                out.append(
                    (f"{prefix}{n:03d}", "no requirement was extracted under this number", "")
                )
    return sorted(out)
