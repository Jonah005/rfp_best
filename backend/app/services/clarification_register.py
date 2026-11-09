"""The remediation pack for a bid that cannot go out yet: what to ask the customer, what the
company must supply, and what the team must settle between its own documents.

Built from records the system already holds (open tender defects with their recommended
questions, and the findings of the latest package validation), so it is the same for any
tender and needs no hand-written list.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.models import ClarificationIssue
from app.models import Package as ReleasePackage
from app.models.enums import ClarificationStatus

# finding code -> (group, question or instruction template). {m} is the finding message.
_CUSTOMER = {
    "TEMPLATE_RESIDUE": "The tender text mentions names that look carried over from another "
    "project ({terms}). Please confirm whether they apply to this tender, and "
    "if so what they refer to.",
    "TENDER_INCOMPLETE": "The issued tender has unfinished fields (blank lines, dotted "
    "placeholders or template titles). Please confirm this is the final version and provide the "
    "missing information.",
    "FORMAL_TENDER_TITLE_UNVERIFIED": "Please confirm the formal title of this tender; the issued "
    "document carries a blank or template title.",
    "TENDER_REFERENCE_MISSING": "Please provide the tender reference number to quote in the "
    "proposal.",
    "CUSTOMER_IDENTITY_UNVERIFIED": "Please confirm the exact legal name of the contracting "
    "entity.",
}
# Questions that all ask the customer to tidy the same leftover template wording are one
# question, not a dozen.
_TEMPLATE_ARTEFACT = re.compile(
    r"<date>|<[^>]*template[^>]*>|template\s+(?:footer|placeholder|filename|artefact)|"
    r"issue\s+date|page\s+(?:count|numbering)|document\s+control|revision\s+history|"
    r"distribution\s+list|final\s+(?:issued\s+)?version|table\s+of\s+contents",
    re.IGNORECASE,
)
_CLAUSE = re.compile(r"\bclauses?\s+([0-9][0-9.]*(?:\([a-z0-9]+\))*)", re.IGNORECASE)
_OWNER_RULES = (
    (re.compile(r"\bterms|liabilit|legal|confidential|warrant|indemn", re.IGNORECASE), "Legal"),
    (
        re.compile(r"price|pricing|commercial|financial|bond|payment|tax|cost", re.IGNORECASE),
        "Commercial",
    ),
    (
        re.compile(
            r"\bCVs?\b|resume|staff|visa|training\s+cert|organi[sz]ational\s+chart|screening",
            re.IGNORECASE,
        ),
        "Delivery lead",
    ),
    (
        re.compile(
            r"architecture|methodology|solution|technical|project\s+plan|schedule|milestone"
            r"|brochure|specification|scope",
            re.IGNORECASE,
        ),
        "Solution architect",
    ),
    (
        re.compile(r"reference|company\s+(?:profile|overview)|appendix|testimonial", re.IGNORECASE),
        "Bid manager",
    ),
)


def _validation_counts(findings) -> dict:
    """Findings of the package's latest validation by waivability and code."""
    out: dict[str, dict[str, int]] = {"non_waivable": {}, "waivable": {}}
    for f in findings:
        side = "non_waivable" if f.waivability.value == "NON_WAIVABLE" else "waivable"
        out[side][f.check_code] = out[side].get(f.check_code, 0) + 1
    return out


def render_validation_report(data: dict, audit: dict, *, round_label: str = "") -> str:
    """The validation report, written from the same register object the register file is.

    Nothing here is typed by hand: every count comes from `data`, so the report and the
    register cannot disagree. The package check refuses a package where they do.
    """
    vc = data.get("validation_counts") or {"non_waivable": {}, "waivable": {}}

    def line(label: str, group: dict) -> str:
        parts = "; ".join(f"{code} {n}" for code, n in sorted(group.items(), key=lambda x: -x[1]))
        return f"{label} ({sum(group.values())}): {parts or 'none'}."

    c = data.get("counts") or {}
    names = (
        ("blocking", "blocking"),
        ("optional", "optional"),
        ("bidder_decisions", "bidder decisions"),
        ("answered", "answered in tender"),
        ("work_packages", "work packages"),
        ("company_items", "company items"),
        ("pre_bid", "pre-bid"),
        ("dispatch", "dispatch"),
        ("post_award", "post-award"),
        ("internal", "internal"),
    )
    register = ", ".join(f"{label} {c.get(key, 0)}" for key, label in names)
    active = ", ".join(audit.get("active_findings") or []) or "none"
    return "\n".join(
        [
            f"# ADX validation report{round_label} (package {data.get('package_id')}, REVIEW)",
            "",
            line("Non-waivable", vc["non_waivable"]),
            "",
            line("Waivable", vc["waivable"]),
            "",
            f"Contradiction audit: run {audit.get('consistency_run_id')}, "
            f"{audit.get('records')} records, active findings {active}.",
            "",
            f"Clarification register counts: {register}.",
            "",
        ]
    )


def _also(c: dict) -> str:
    """The other wordings a question stands for, so folding duplicates loses nothing."""
    others = c.get("also_asked_as") or []
    return f" [also covers: {' / '.join(o.strip() for o in others)}]" if others else ""


def _owner(text: str) -> str:
    for pattern, who in _OWNER_RULES:
        if pattern.search(text or ""):
            return who
    return "Bid manager"


_CAT_IMPACT = {
    "SUBMISSION_ADMIN": "The final documents cannot be issued with correct titles, dates and "
    "references",
    "LEGAL_CONTRACTUAL": "The contractual position cannot be accepted or taken as an exception "
    "with confidence",
    "ELIGIBILITY": "Eligibility cannot be shown against the correct thresholds",
    "COMMERCIAL": "The commercial proposal cannot be priced on the customer's basis",
    "STAFFING": "Staffing cannot be planned to the customer's expectations",
}
_ORDER = {"BLOCKING": 0, "MAJOR": 1, "MINOR": 2, "INFO": 3}
_COMPANY = (
    "EVIDENCE_SHORTFALL",
    "MANDATORY_DELIVERABLE_NOT_READY",
    "DELIVERABLE_NOT_READY",
    "REVIEW_ANNOTATION",
    "SECTION_WITHOUT_CONTENT",
    "UNRESOLVED_TASK",
    "INTERNAL_WORDING",
    "PRICE_NOT_APPROVED",
    "COMMERCIAL_WITHOUT_PRICE",
    "COMMERCIAL_TOTAL_MISSING",
    "PLACEHOLDER_PRESENT",
)
# The internal export names of the audit files and the names they carry in the delivered package.
_DELIVERED_NAME = {
    "contradiction-audit-BID.json": "04-contradiction-audit.json",
    "contradiction-audit-BID.csv": "04-contradiction-audit.csv",
}
_RESOLUTION_ITEM = re.compile(r"\bResolve\s+\w+\s+conflict\s+between\b", re.IGNORECASE)
_VALIDITY_SPLIT = "The documents start the offer's validity at different moments"
_INTERNAL = (
    "REQUIREMENT_COVERAGE_GAP",
    "REQUIREMENT_EXCLUSION_REVIEW",
    "CROSS_DOCUMENT_CONTRADICTION",
    "TERMS_DEVIATION",
    "TERMS_AMBIGUITY",
    "CROSS_BID_CONTENT",
    "MATRIX_UNVERIFIED",
    "CONSISTENCY_CHECK_NOT_RUN",
)


def build(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID, *, gateway=None) -> dict:
    from app.models import Deliverable
    from app.models.enums import DeliverableStatus
    from app.services import clarification_triage as ct
    from app.services import package_lint
    from app.services import release as release_service

    customer: list[dict] = []
    company: list[dict] = []
    internal: list[dict] = []
    readiness_note = ""

    for issue in session.execute(
        select(ClarificationIssue).where(
            ClarificationIssue.org_id == ctx.org_id,
            ClarificationIssue.bid_id == bid_id,
            ClarificationIssue.status.in_(
                [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
            ),
            ClarificationIssue.recommended_question.is_not(None),
            ClarificationIssue.restates_clarification_id.is_(None),
        )
    ).scalars():
        customer.append(
            {
                "severity": getattr(issue.severity, "value", str(issue.severity)),
                "question": issue.recommended_question,
                "basis": issue.description,
                "status": getattr(issue.status, "value", str(issue.status)),
                "category": ct.category_of(issue.recommended_question or ""),
            }
        )

    package = (
        session.execute(
            select(ReleasePackage)
            .where(ReleasePackage.org_id == ctx.org_id, ReleasePackage.bid_id == bid_id)
            .order_by(ReleasePackage.version_no.desc())
        )
        .scalars()
        .first()
    )
    findings = (
        release_service.validation_findings(session, ctx, package_id=package.id) if package else []
    )
    dispatch: list[dict] = []
    prebid: list[dict] = []
    for f in findings:
        code, message = f.check_code, f.message or ""
        if code in _CUSTOMER:
            terms = (
                ", ".join(re.findall(r"\b[A-Z]{3,6}\b", message.split(" appear")[0]))
                if code == "TEMPLATE_RESIDUE"
                else ""
            )
            customer.append(
                {
                    "severity": "MAJOR",
                    "question": _CUSTOMER[code].format(terms=terms),
                    "basis": message,
                    "status": "TO_ASK",
                    "category": "SUBMISSION_ADMIN_BLOCKER",
                }
            )
        elif code in ("DISPATCH_ITEM_PENDING", "DISPATCH_ITEM_BLOCKING"):
            dispatch.append({"code": code, "item": message})
        elif code == "PRE_BID_NOTICE_PENDING":
            prebid.append({"code": code, "item": message})
        elif code in _COMPANY:
            company.append({"code": code, "item": message})
        elif code == "MATRIX_UNVERIFIED":
            # A readiness total, not a contradiction: shown as a summary line, not an issue.
            readiness_note = message
        elif code in _INTERNAL:
            detail = f.detail if isinstance(getattr(f, "detail", None), dict) else {}
            fid = detail.get("finding_id")
            if message.startswith(_VALIDITY_SPLIT) and not any(
                c.get("basis") == message for c in customer
            ):
                # Our documents start the offer's validity at different moments. Whatever the
                # tender says, no position can be approved until ADX says which start date
                # controls, so this goes to the customer as a conflict in the tender's own rules.
                days = re.findall(r"\b(\d{1,3})[-\s]?days?\b", message)
                period = f"{days[0]}-day " if days else ""
                customer.append(
                    {
                        "severity": "MAJOR",
                        "category": "LEGAL_CONTRACTUAL",
                        "status": "TO_ASK",
                        "tender_conflict": True,
                        "input_from": "Legal and Commercial",
                        "question": f"Please confirm whether the {period}bid-validity period "
                        "starts on the bid submission date or on the tender opening date, and "
                        "whether bid validity and price firmness are meant to start on "
                        "different dates.",
                        "basis": message,
                    }
                )
            if fid:
                # A finding with an id is the same finding only if it has the same id: two
                # findings about different clauses can share many words and are still two.
                if any(i.get("finding_id") == fid for i in internal):
                    continue
                internal.append({"code": code, "item": message, "finding_id": fid, "_stems": set()})
                continue
            # An older finding without an id: the same conflict is often reported twice in
            # different words; keep one.
            stems = {w[:5] for w in re.findall(r"[a-z]{5,}", message.split(" -- ")[0].lower())}
            if any(
                i["code"] == code
                and i["_stems"]
                and len(stems & i["_stems"]) >= max(3, 0.5 * min(len(stems), len(i["_stems"])))
                for i in internal
            ):
                continue
            internal.append({"code": code, "item": message, "_stems": stems})

    # Post-award items are not findings; they are answered by a commitment at bid stage.
    # One entry per obligation: a deliverable and the requirement that states it are the same item.
    post_award: list[dict] = []
    try:
        from app.services import report_compliance as _rc

        linked = _rc.post_award_links(session, ctx, bid_id)
    except Exception:  # noqa: BLE001
        _rc, linked = None, {}  # type: ignore[assignment]
    codes_by_name: dict[str, list[str]] = {}
    for code, name in linked.items():
        codes_by_name.setdefault(name, []).append(code)
    for d in session.execute(
        select(Deliverable).where(
            Deliverable.org_id == ctx.org_id,
            Deliverable.bid_id == bid_id,
            Deliverable.is_mandatory.is_(True),
            Deliverable.duplicate_of_deliverable_id.is_(None),
            Deliverable.status.not_in(
                [
                    DeliverableStatus.READY,
                    DeliverableStatus.SUBMITTED,
                    DeliverableStatus.NOT_APPLICABLE,
                ]
            ),
        )
    ).scalars():
        if package_lint.deliverable_stage(d.name) == "POST_AWARD":
            codes = ", ".join(sorted(codes_by_name.get(d.name, [])))
            post_award.append(
                {"code": "POST_AWARD", "item": d.name + (f" [{codes}]" if codes else "")}
            )
    if _rc is not None:
        try:
            for code, text in _rc.post_award_rows(session, ctx, bid_id):
                if code not in linked:
                    post_award.append({"code": "POST_AWARD", "item": f"{code}: {text}"})
        except Exception:  # noqa: BLE001, S110 - the deliverable list still stands
            pass
    post_award = list({p["item"].casefold(): p for p in post_award}.values())

    # Design information the tender never gives, and questions the tender already answers.
    tender = ct.tender_text(session, ctx, bid_id)
    gaps = ct.design_gaps(session, ctx, bid_id, tender, gateway=gateway)
    merged = [c for c in customer if _TEMPLATE_ARTEFACT.search(c["question"] or "")]
    if len(merged) > 1:
        customer = [c for c in customer if c not in merged]
        customer.append(
            {
                "severity": "MAJOR",
                "question": (
                    "The issued document still carries template wording (placeholder dates, a "
                    "template "
                    "file name in the footers, blank document-control, revision-history and "
                    "distribution fields, an incomplete table of contents). Please confirm this is "
                    "the final issued version, or reissue a clean one with the issue date, "
                    "reference and page count."
                ),
                "basis": f"{len(merged)} separate observations of the same problem were merged.",
                "status": "OPEN",
                "category": "SUBMISSION_ADMIN_BLOCKER",
            }
        )
    # The answer check runs after the branches exist, over every generated question.
    gaps = ct.merge_branch_variants(ct.triage(session, ctx, gaps, tender, gateway=gateway))
    ct.stated_facts(gaps, tender)  # facts the tender states are answered by rule, not by sampling
    ct.demote_unspecific_answers(gaps)
    answered_gaps = [g for g in gaps if g.get("verdict") == "ANSWERED_IN_TENDER"]
    internal_gaps = [g for g in gaps if g.get("verdict") == "BIDDER_DECISION"]
    gaps = [g for g in gaps if g.get("verdict") not in ("ANSWERED_IN_TENDER", "BIDDER_DECISION")]
    minor = [c for c in customer if c["severity"] in ("MINOR", "INFO")]
    major = [c for c in customer if c["severity"] not in ("MINOR", "INFO")]
    minor = ct.triage(session, ctx, minor, tender, gateway=gateway)

    ct.stated_facts(minor, tender)
    ct.demote_unspecific_answers(minor)

    blocking = [g for g in gaps if g["severity"] == "BLOCKING"] + major
    optional = [g for g in gaps if g["severity"] != "BLOCKING"] + [
        c for c in minor if c.get("verdict") == "KEEP" and c["severity"] == "MINOR"
    ]
    answered = [c for c in minor if c.get("verdict") == "ANSWERED_IN_TENDER"] + answered_gaps
    bidder = [
        c
        for c in minor
        if c.get("verdict") == "BIDDER_DECISION"
        or (c.get("verdict") == "KEEP" and c["severity"] == "INFO")
    ] + internal_gaps
    blocking, optional, bidder, answered = ct.reconcile_sections(
        blocking, optional, bidder, answered
    )
    order = {
        "SOLUTION_DESIGN": 0,
        "SECURITY_COMPLIANCE": 1,
        "PROJECT_DELIVERY": 2,
        "STAFFING": 3,
        "COMMERCIAL": 4,
        "LEGAL_CONTRACTUAL": 5,
        "ELIGIBILITY": 6,
        "SUBMISSION_ADMIN": 7,
    }
    # The question that decides what the project is comes first, then by category.
    blocking.sort(
        key=lambda c: (
            0
            if c.get("question", "").startswith("The tender supports more than one reading")
            else 1,
            order.get(c.get("category") or "", 9),
            _ORDER.get(c["severity"], 9),
        )
    )
    for group, impact in (
        (blocking, "Needed before the offer can be completed"),
        (optional, "A reasonable assumption will be stated if unanswered"),
        (bidder, "Not sent to the customer"),
        (answered, "Already answered in the tender"),
    ):
        for c in group:
            m = _CLAUSE.search(c["question"] or "")
            c["clause"] = m.group(1) if m else ""
            c["owner"] = "Bid manager" + (
                f" (with {c['input_from']} input)" if c.get("input_from") else ""
            )
            c["impact_if_unanswered"] = c.get("impact") or (
                _CAT_IMPACT.get(c.get("category") or "", impact) if group is blocking else impact
            )
            c["response"] = ""
            c["resolution"] = c.get("status") or "OPEN"
            c["question_id"] = ct.question_id(c["question"] or "", c["clause"])

    # Company work: group the several items one person would do together.
    texts = [
        re.sub(r"^Required deliverable '(.*)' is not marked ready\.$", r"\1", c["item"])
        for c in company
    ]
    # Tasks that resolve a conflict between two approved answers are the bid manager's, with
    # Legal and Commercial contributing; they are not grouped with ordinary company work.
    is_conflict = [bool(_RESOLUTION_ITEM.search(t)) for t in texts]
    ordinary = [t for t, flag in zip(texts, is_conflict, strict=True) if not flag]
    packages = ct.work_packages(session, ctx, ordinary, gateway=gateway)
    work: list[dict] = []
    for p in packages:
        names = [ordinary[i] for i in p["items"]]
        work.append(
            {
                "package": p["name"],
                "owner": ct.owner_of(" ".join([p["name"], *names])),
                "items": names,
            }
        )
    conflicts = [t for t, flag in zip(texts, is_conflict, strict=True) if flag]
    if conflicts:
        work.append(
            {
                "package": "Resolve conflicts between approved answers",
                "owner": "Bid manager",
                "contributors": ["Legal", "Commercial"],
                "items": conflicts,
            }
        )
    for c in internal:
        c["owner"] = (
            "Legal / Compliance"
            if c["code"] in ("TERMS_DEVIATION", "TERMS_AMBIGUITY")
            else "Bid manager"
        )
    for c in dispatch:
        c["owner"] = "Bid coordinator"

    def row(n, c):
        clause = f" (clause {c['clause']})" if c.get("clause") else ""
        cat = f" [{c['category']}]" if c.get("category") else ""
        branch = f" [if scope is: {c['branch']}]" if c.get("branch") else ""
        note = (
            f' | Tender partly says: "{c["partial_answer"]}" but not: {c["still_missing"]}'
            if c.get("partial_answer")
            else ""
        )
        qid = f" ({c['question_id']})" if c.get("question_id") else ""
        qid += _also(c)
        return (
            f"{n}. [{c['severity']}]{cat}{branch} {c['question']}{clause}{qid} | "
            f"Owner: {c['owner']} | "
            f"If unanswered: {c['impact_if_unanswered']}{note} | Status: {c['resolution']}"
        )

    lines = ["# Clarification and remediation register", ""]

    def first_wave(c):
        return c.get("question", "").startswith(
            "The tender supports more than one reading"
        ) or c.get("category") in ("SUBMISSION_ADMIN", "SUBMISSION_ADMIN_BLOCKER")

    def _stems(t):
        return {w[:5] for w in re.findall(r"[a-z]{5,}", (t or "").lower())}

    wave1: list[dict] = []
    for c in [c for c in blocking if first_wave(c)]:
        # Before the scope is chosen these questions are branch-neutral; one per topic.
        if c.get("branch"):
            c["branch"] = ""
        s1 = _stems(c.get("question"))
        if any(
            len(s1 & _stems(o.get("question")))
            >= 0.5 * max(1, min(len(s1), len(_stems(o.get("question")))))
            for o in wave1
        ):
            continue
        wave1.append(c)

    # A question that asks for the title and the reference together is redundant once each is
    # asked on its own; the separate ones inherit its severity.
    def _asks(c, word):
        return bool(re.search(rf"\b{word}\b", c.get("question", ""), re.I))

    _rank = {"BLOCKING": 3, "MAJOR": 2, "MINOR": 1}
    for combined in [c for c in wave1 if _asks(c, "title") and _asks(c, "reference")]:
        singles = [
            c for c in wave1 if c is not combined and (_asks(c, "title") != _asks(c, "reference"))
        ]
        if any(_asks(c, "title") for c in singles) and any(_asks(c, "reference") for c in singles):
            for c in singles:
                if _rank.get(combined.get("severity") or "", 0) > _rank.get(
                    c.get("severity") or "", 0
                ):
                    c["severity"] = combined["severity"]
            wave1.remove(combined)
    wave2 = [c for c in blocking if not first_wave(c)]
    blocking = wave1 + wave2
    lines[2:2] = [
        "Summary (generated from the sections below): "
        f"{len(blocking)} blocking questions; {len(optional)} optional confirmations; "
        f"{len(bidder)} bidder decisions; {len(answered)} already answered in the tender; "
        f"{len(work)} company work packages ({len(company)} items); {len(prebid)} pre-bid items; "
        f"{len(dispatch)} dispatch items; {len(post_award)} post-award obligations; "
        f"{len(internal)} internal issues.",
        "",
    ]
    lines += [
        f"## A1. Blocking questions for the customer ({len(blocking)}), sent in two waves",
        "",
    ]
    lines += [
        f"### A1.1 Send first ({len(wave1)}): confirm the scope and the administrative facts",
        "",
    ]
    lines += [row(i, c) for i, c in enumerate(wave1, 1)] or ["None."]
    lines += [
        "",
        f"### A1.2 Send after ADX confirms the scope ({len(wave2)}): send only the branch ADX "
        f"selects; the other branches stay internal",
        "",
    ]
    lines += [row(i, c) for i, c in enumerate(wave2, len(wave1) + 1)] or ["None."]
    lines += [
        "",
        f"## A2. Optional confirmations, ask only if the procurement process allows "
        f"({len(optional)})",
        "",
    ]
    lines += [row(i, c) for i, c in enumerate(optional, 1)] or ["None."]
    lines += ["", f"## A3. Bidder decisions, not sent to the customer ({len(bidder)})", ""]
    lines += [row(i, c) for i, c in enumerate(bidder, 1)] or ["None."]
    lines += [
        "",
        f"## A4. Already answered in the tender, no question needed ({len(answered)})",
        "",
    ]
    lines += [
        f'- {c["question"]} ({c.get("question_id", "")}){_also(c)} -- tender says: '
        f'"{c.get("answer_quote", "")}"'
        for c in answered
    ] or ["None."]
    lines += [
        "",
        f"## B1. Bid work packages the company must finish before submission ({len(work)} "
        f"packages, {len(company)} items)",
        "",
    ]
    for w in work:
        helpers = " and ".join(w.get("contributors") or [])
        who = w["owner"] + (f", with {helpers} contributing" if helpers else "")
        lines.append(
            f"- [{who}] {w['package']} ({len(w['items'])} "
            f"item{'s' if len(w['items']) != 1 else ''})"
        )
        if len(w["items"]) > 1 or w.get("contributors"):
            lines += [f"    - {name}" for name in w["items"]]
    if not work:
        lines.append("None.")
    lines += ["", f"## B0. Pre-bid administration, time-critical ({len(prebid)})", ""]
    lines += [f"- [Bid manager] {c['item']}" for c in prebid] or ["None."]
    lines += [
        "",
        f"## B2. Dispatch checklist: physical items packed at the last step ({len(dispatch)})",
        "",
    ]
    lines += [f"- [{c['owner']}] {c['item']}" for c in dispatch] or ["None."]
    lines += [
        "",
        f"## B3. Post-award obligations: answered by commitment now, delivered after award "
        f"({len(post_award)})",
        "",
    ]
    lines += [f"- {c['item']}" for c in post_award] or ["None."]
    lines += [
        "",
        f"## C. Contradictions and deviations to resolve inside our own documents "
        f"({len(internal)})",
        "",
    ]
    lines += [f"- [{c['owner']}] {c['item']}" for c in internal] or ["None."]
    if readiness_note:
        lines += ["", f"Readiness summary (not a contradiction): {readiness_note}"]
    markdown = "\n".join(lines) + "\n"
    return {
        "package_id": str(package.id) if package else None,
        "blocking_questions": blocking,
        "wave_one": wave1,
        "wave_two": wave2,
        "optional_questions": optional,
        "bidder_decisions": bidder,
        "answered_in_tender": answered,
        "customer_questions": blocking + optional + bidder,
        "work_packages": work,
        "company_actions": company,
        "dispatch_checklist": dispatch,
        "pre_bid_administration": prebid,
        "post_award_obligations": post_award,
        "internal_issues": [{k: v for k, v in c.items() if k != "_stems"} for c in internal],
        "markdown": markdown,
        "validation_counts": _validation_counts(findings),
        "counts": {
            "blocking": len(blocking),
            "optional": len(optional),
            "bidder_decisions": len(bidder),
            "answered": len(answered),
            "work_packages": len(work),
            "company_items": len(company),
            "pre_bid": len(prebid),
            "dispatch": len(dispatch),
            "post_award": len(post_award),
            "internal": len(internal),
        },
    }


def export(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> dict:
    """Build the register and also write it (markdown and JSON) under the backend's local
    exports folder, so it can be dropped straight into a review folder."""
    import json
    from pathlib import Path

    data = build(session, ctx, bid_id)
    folder = Path(__file__).resolve().parents[2] / ".local" / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"clarification-register-{bid_id}.md").write_text(data["markdown"], encoding="utf-8")
    (folder / f"clarification-register-{bid_id}.json").write_text(
        json.dumps({k: v for k, v in data.items() if k != "markdown"}, indent=2, default=str),
        encoding="utf-8",
    )
    data["files"] = [
        str(folder / f"clarification-register-{bid_id}.md"),
        str(folder / f"clarification-register-{bid_id}.json"),
    ]
    try:
        import csv

        from app.services import report_attachments

        manifest = report_attachments.evidence_manifest(session, ctx, bid_id)
        (folder / f"evidence-manifest-{bid_id}.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8"
        )
        if manifest:
            with (folder / f"evidence-manifest-{bid_id}.csv").open(
                "w", newline="", encoding="utf-8"
            ) as fh:
                writer = csv.DictWriter(fh, fieldnames=list(manifest[0].keys()))
                writer.writeheader()
                for row in manifest:
                    writer.writerow(
                        {k: ", ".join(v) if isinstance(v, list) else v for k, v in row.items()}
                    )
        data["files"] += [
            str(folder / f"evidence-manifest-{bid_id}.json"),
            str(folder / f"evidence-manifest-{bid_id}.csv"),
        ]
    except Exception:  # noqa: BLE001, S110 - the register stands without the manifest
        pass
    try:
        import csv

        from app.services import package_consistency
        from app.services.storage import get_storage

        runs = package_consistency.load_audit(get_storage(), ctx, bid_id)
        info = package_consistency.write_audit_files(
            folder, bid_id, runs[-1] if runs else {}, data.get("package_id")
        )
        data["audit_integrity"] = info
        reg_ids = sorted(
            {
                c.get("finding_id")
                for c in (data.get("internal_issues") or [])
                if c.get("finding_id")
            }
        )
        if reg_ids != info["active_findings"]:
            raise ValueError(
                f"the register lists internal findings {reg_ids} but the audit's active findings "
                f"are {info['active_findings']}"
            )
        data["files"] += [
            str(folder / f"contradiction-audit-{bid_id}.json"),
            str(folder / f"contradiction-audit-{bid_id}.csv"),
        ]
        (folder / f"package-manifest-{bid_id}.json").write_text(
            json.dumps(
                {
                    "bid_id": str(bid_id),
                    "package_id": data.get("package_id"),
                    "consistency_run_id": info["consistency_run_id"],
                    "audit_records": info["records"],
                    "audit_active_findings": info["active_findings"],
                    "internal_issues_in_register": len(data.get("internal_issues") or []),
                    "sha256": {
                        _DELIVERED_NAME.get(name.replace(str(bid_id), "BID"), name): digest
                        for name, digest in info["files"].items()
                    },
                    "names_are": "the filenames as delivered in the review package",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        data["files"].append(str(folder / f"package-manifest-{bid_id}.json"))
        (folder / f"validation-report-{bid_id}.md").write_text(
            render_validation_report(data, info), encoding="utf-8"
        )
        data["files"].append(str(folder / f"validation-report-{bid_id}.md"))
    except Exception as exc:  # noqa: BLE001 - the register stands, but the failure is stated
        data["audit_integrity"] = {"error": str(exc)}
        for name in (
            f"contradiction-audit-{bid_id}.json",
            f"contradiction-audit-{bid_id}.csv",
            f"package-manifest-{bid_id}.json",
        ):
            (folder / name).unlink(missing_ok=True)
    return data
