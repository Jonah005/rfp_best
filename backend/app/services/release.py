"""Proposal assembly, deterministic validation, release and learning."""

from __future__ import annotations

import hashlib
import io
import re
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Protocol

from docx import Document as WordDocument
from docx.document import Document as DocxDocument
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import IllegalTransition, NotFound, StateConflict, ValidationError
from app.models import (
    ArtifactStatement,
    ArtifactVersion,
    Commitment,
    Deliverable,
    Evidence,
    GeneratedDocument,
    GeneratedDocumentVersion,
    KnowledgeUpdateCandidate,
    Manifest,
    ManifestEntry,
    Outcome,
    PackageItem,
    ProposalSection,
    ProposalStructure,
    ReleaseException,
    Requirement,
    RequirementVersion,
    ResponseArtifact,
    SectionArtifactMap,
    ValidationFinding,
    ValidationRun,
)
from app.models import Package as ReleasePackage
from app.models.enums import (
    ApprovalStatus,
    ArtifactState,
    ArtifactVersionState,
    CandidateState,
    ConfidentialityClass,
    DeliverableStatus,
    DocumentStream,
    ExceptionState,
    FindingSeverity,
    FindingWaivability,
    GeneratedFormat,
    InclusionMode,
    KnowledgeStatus,
    OutcomeResult,
    OutputMode,
    PackageState,
    StatementClass,
    StructureDerivationMethod,
    ValidationRunStatus,
)
from app.services import (
    audit,
    knowledge,
    report_attachments,
    report_compliance,
    report_consolidate,
    report_edit,
    report_extras,
    report_figures,
)
from app.services.storage import ObjectStorage, get_storage

RULESET_VERSION = "2026.08.v1"
PLACEHOLDER = re.compile(r"\b(?:TBD|TBC|TODO|INSERT\s+HERE|PLACEHOLDER)\b", re.IGNORECASE)
NON_WAIVABLE = {
    "PRICE_IN_TECHNICAL",
    "PRICE_NOT_APPROVED",
    "FIXTURE_EVIDENCE",
    "FIXTURE_DERIVED_CLAIMS",
    "TENANT_LEAKAGE",
    "PERMISSION_VIOLATION",
    "MALWARE_DETECTED",
    "CORRUPTED_ARTIFACT",
    "RESTRICTED_DISCLOSURE",
    # Found by reading the finished files: another customer's name, and no price.
    "CROSS_BID_CONTENT",
    "COMMERCIAL_WITHOUT_PRICE",
    # The tender itself is unfinished: no submission may be assembled, so no waiver either.
    "TENDER_INCOMPLETE",
    # A requirement missing from the matrix without a classification may be a bidder obligation.
    "REQUIREMENT_COVERAGE_GAP",
    # A mandatory evidence count the package cannot meet (two references where the tender asks for
    # three).
    "EVIDENCE_SHORTFALL",
    # The reference documents themselves record a missing letter or contact.
    "EVIDENCE_INCOMPLETE",
    # An attached document nobody has attested as genuine: approval alone is not proof it is real.
    "PROVENANCE_UNVERIFIED",
    # A core document the tender requires and nobody has produced cannot be waived away.
    "MANDATORY_DELIVERABLE_NOT_READY",
    # Physical packing is not drafting work, but a package cannot go out with it undone.
    "DISPATCH_ITEM_BLOCKING",
}

#: Classes that must not leave the building inside a proposal.
#:
#: CONFIDENTIAL is deliberately NOT here. It is the ordinary class for internal
#: company material -- policies, methodologies, CVs -- which is exactly what a
#: proposal is made of, and blocking it would block every release. RESTRICTED
#: and PRIVILEGED are the two a person had to choose deliberately: the rate
#: card, legal advice. The line is drawn where somebody made a decision, not
#: where a default landed.
UNRELEASABLE = frozenset({ConfidentialityClass.RESTRICTED, ConfidentialityClass.PRIVILEGED})

#: A money amount: a currency before or after a number. Checked against the
#: TECHNICAL submission only, where a tender that separates the envelopes makes
#: any price a disqualification. This complements RESTRICTED_DISCLOSURE, which
#: catches a classified rate card being cited; this catches a price typed in.
PRICE = re.compile(
    r"(?:\b(?:AED|SAR|USD|EUR|GBP|QAR|KWD|OMR|BHD|Dhs?)\.?\s?|[$\u20ac\u00a3]\s?)\d[\d,]*(?:\.\d+)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:AED|SAR|USD|EUR|GBP|QAR|dirhams?|riyals?)\b",
    re.IGNORECASE,
)


def _structure(session: Session, ctx: ExecutionContext, structure_id: uuid.UUID):
    row = session.get(ProposalStructure, structure_id)
    if row is None or row.org_id != ctx.org_id:
        raise NotFound("That proposal structure does not exist.")
    return row


def structure_for_bid(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID):
    row = session.execute(
        select(ProposalStructure).where(
            ProposalStructure.org_id == ctx.org_id,
            ProposalStructure.bid_id == bid_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("That bid has no proposal structure.")
    return row


def sections(session: Session, ctx: ExecutionContext, *, structure_id: uuid.UUID):
    _structure(session, ctx, structure_id)
    return list(
        session.execute(
            select(ProposalSection)
            .where(
                ProposalSection.org_id == ctx.org_id,
                ProposalSection.structure_id == structure_id,
            )
            .order_by(ProposalSection.order_index)
        ).scalars()
    )


COMMERCIAL_DEPARTMENTS = frozenset({"COMMERCIAL", "FINANCE"})
COMMERCIAL_CATEGORIES = frozenset({"COMMERCIAL", "FINANCIAL", "PRICING"})


def _artifact_facts(session: Session, ctx: ExecutionContext, artifact: ResponseArtifact) -> dict:
    """What an answer is about: its requirement codes and categories, and its department."""
    from app.models import ArtifactRequirement, Department, Requirement, Task

    rows = session.execute(
        select(Requirement.code, Requirement.category)
        .join(ArtifactRequirement, ArtifactRequirement.requirement_id == Requirement.id)
        .where(
            ArtifactRequirement.org_id == ctx.org_id,
            ArtifactRequirement.artifact_id == artifact.id,
        )
    ).all()
    department = session.execute(
        select(Department.code)
        .join(Task, Task.department_id == Department.id)
        .where(Task.id == artifact.task_id)
    ).scalar_one_or_none()
    return {
        "codes": sorted(code for code, _ in rows),
        "categories": sorted({getattr(c, "value", str(c)) for _, c in rows}),
        "department": getattr(department, "value", department),
    }


def stream_of(facts: dict) -> DocumentStream:
    """Which submission an answer belongs in when nobody has said otherwise.

    Commercial if the task belongs to Commercial or Finance, or every
    requirement it answers is commercial; technical otherwise. The tender's own
    instructions, read by `proposal_structure`, take precedence over this.
    """
    if facts.get("department") in COMMERCIAL_DEPARTMENTS:
        return DocumentStream.COMMERCIAL
    categories = set(facts.get("categories") or [])
    if categories and categories <= COMMERCIAL_CATEGORIES:
        return DocumentStream.COMMERCIAL
    return DocumentStream.TECHNICAL


def sections_from(proposed: list, refs: dict[str, uuid.UUID]) -> list[dict]:
    """Turn the model's proposed sections into a placement of every answer once.

    `refs` maps the refs the model was shown (A1, A2, ...) to artifact ids. A
    ref it was not shown is ignored; an answer placed twice stays in its first
    section. Returns [] when nothing usable came back, so the caller falls back.
    Answers no section claimed are left for the caller to place.
    """
    placed: set[str] = set()
    out: list[dict] = []
    for section in proposed or []:
        title = (getattr(section, "title", "") or "").strip()
        if not title:
            continue
        artifact_ids = []
        for ref in getattr(section, "artifact_refs", []) or []:
            key = (ref or "").strip().upper()
            if key in refs and key not in placed:
                placed.add(key)
                artifact_ids.append(refs[key])
        out.append(
            {
                "number_label": getattr(section, "number_label", None),
                "title": title,
                "stream": DocumentStream(getattr(section, "stream", "TECHNICAL") or "TECHNICAL"),
                "is_mandatory": bool(getattr(section, "is_mandatory", True)),
                "artifact_ids": artifact_ids,
            }
        )
    return out if any(s["artifact_ids"] for s in out) or (out and not refs) else []


_GENERIC_TITLE_WORDS = {
    "requirements",
    "requirement",
    "section",
    "proposal",
    "summary",
    "executive",
    "general",
    "guidelines",
    "overview",
}


def fill_empty_sections(plan: list[dict], titles: dict, categories: dict) -> list[dict]:
    """Give a section the customer asked for the answers that are about it.

    The model places answers by title and requirement; it sometimes leaves a
    section empty while the answers for it sit under a neighbour (integration
    answers under "Business Requirements"). For each empty section, move the
    answers whose title or requirement categories carry the section's own
    subject word. An answer only moves if its current section keeps another.
    """
    for entry in plan:
        if entry["artifact_ids"] or "summary" in entry["title"].casefold():
            continue
        stems = {
            w[:6]
            for w in re.findall(r"[a-z]{5,}", entry["title"].casefold())
            if w not in _GENERIC_TITLE_WORDS
        }
        if not stems:
            continue
        for other in plan:
            if other is entry:
                continue
            for aid in list(other["artifact_ids"]):
                text = (titles.get(aid, "") + " " + " ".join(categories.get(aid, []))).casefold()
                if len(other["artifact_ids"]) > 1 and any(stem in text for stem in stems):
                    other["artifact_ids"].remove(aid)
                    entry["artifact_ids"].append(aid)
    return plan


def _tender_structure_text(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> str:
    """The parts of the tender that talk about structure and submission."""
    from app.services.tender_analysis import tender_blocks

    keywords = re.compile(
        r"\b(proposal|section|appendix|annex|envelope|submi|format|volume|separate|"
        r"technical|commercial|financial)\w*",
        re.IGNORECASE,
    )
    lines: list[str] = []
    budget = 12_000
    for block in tender_blocks(session, ctx, bid_id=bid_id):
        text = " ".join((block.text or "").split())
        heading = " / ".join(block.heading_path or [])
        if not keywords.search(text) and not keywords.search(heading):
            continue
        entry = f"[{heading}] {text}" if heading else text
        if budget - len(entry) < 0:
            break
        budget -= len(entry)
        lines.append(entry)
    return "\n".join(lines)


def _propose_structure(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    artifacts: list[ResponseArtifact],
    facts: dict[uuid.UUID, dict],
    gateway=None,
) -> list[dict]:
    """The customer's structure, read from the tender, or [] to fall back."""
    import json

    from app.gateway.gateway import build_gateway
    from app.gateway.registry import wrap_untrusted
    from app.schemas.proposal_structure import ProposalStructureResult

    if not artifacts:
        return []
    refs = {f"A{i}": a.id for i, a in enumerate(artifacts, start=1)}
    payload = {
        "tender_structure_text": _tender_structure_text(session, ctx, bid_id=bid_id),
        "approved_answers": [
            {
                "ref": ref,
                "title": next(a.title for a in artifacts if a.id == artifact_id),
                "requirements": facts[artifact_id]["codes"],
                "categories": facts[artifact_id]["categories"],
            }
            for ref, artifact_id in refs.items()
        ],
    }
    try:
        model_gateway = gateway or build_gateway()
        result = model_gateway.generate_structured(
            "proposal_structure",
            wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
            ctx=ctx,
            session=session,
        )
    except Exception:  # noqa: BLE001 - structure degrades to one section per answer
        return []
    if not isinstance(result, ProposalStructureResult):
        return []
    return sections_from(result.sections, refs)


def _document_for(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    structure: ProposalStructure,
    stream: DocumentStream,
) -> GeneratedDocument:
    existing = session.execute(
        select(GeneratedDocument).where(
            GeneratedDocument.org_id == ctx.org_id,
            GeneratedDocument.bid_id == bid_id,
            GeneratedDocument.stream == stream,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    name = "Commercial Proposal" if stream is DocumentStream.COMMERCIAL else "Technical Proposal"
    document = GeneratedDocument(
        org_id=ctx.org_id,
        bid_id=bid_id,
        structure_id=structure.id,
        name=name,
        required_filename=f"{stream.value.lower()}-proposal.docx",
        format=GeneratedFormat.DOCX,
        stream=stream,
        created_by=ctx.actor_user_id,
    )
    session.add(document)
    session.flush()
    return document


def derive_structure(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    source_document_version_id: uuid.UUID | None = None,
    gateway=None,
) -> ProposalStructure:
    """The proposal's sections, in the customer's order, split by submission.

    First choice is the structure the tender asks for (`proposal_structure`);
    failing that, one section per answer, technical and commercial kept apart
    by `stream_of`. Either way each section is bound to the document -- the
    technical or the commercial proposal -- it will be printed in.
    """
    existing = session.execute(
        select(ProposalStructure).where(
            ProposalStructure.org_id == ctx.org_id,
            ProposalStructure.bid_id == bid_id,
        )
    ).scalar_one_or_none()
    if existing:
        has_package = session.execute(
            select(ReleasePackage.id).where(
                ReleasePackage.org_id == ctx.org_id,
                ReleasePackage.bid_id == bid_id,
                ReleasePackage.immutable.is_(True),
            )
        ).first()
        if has_package:
            return existing
        # No package has been released from it, so the structure is a working
        # plan, not a record: rebuild it so it reflects the answers as they are.
        # Only its sections are replaced. The structure and the documents it owns
        # stay, because an unreleased package may already hold versions of them.
        for section in session.execute(
            select(ProposalSection).where(ProposalSection.structure_id == existing.id)
        ).scalars():
            session.delete(section)
        session.flush()
    from app.models import Task
    from app.services.consistency import RESOLUTION_MARK

    # What goes in the proposal is the approved answers. A conflict-resolution
    # task is internal work; its note is not a chapter. Nor is an answer that
    # was superseded or never had any text.
    has_text = (
        select(ArtifactVersion.artifact_id)
        .join(ArtifactStatement, ArtifactStatement.artifact_version_id == ArtifactVersion.id)
        .where(ArtifactVersion.org_id == ctx.org_id)
    )
    internal = select(Task.id).where(
        Task.org_id == ctx.org_id, Task.grouping_rationale.like(RESOLUTION_MARK + "%")
    )
    artifacts = list(
        session.execute(
            select(ResponseArtifact)
            .where(
                ResponseArtifact.org_id == ctx.org_id,
                ResponseArtifact.bid_id == bid_id,
                ResponseArtifact.state != ArtifactState.SUPERSEDED,
                ResponseArtifact.task_id.not_in(internal),
                ResponseArtifact.id.in_(has_text),
            )
            .order_by(ResponseArtifact.created_at)
        ).scalars()
    )
    facts = {a.id: _artifact_facts(session, ctx, a) for a in artifacts}
    proposed = _propose_structure(
        session, ctx, bid_id=bid_id, artifacts=artifacts, facts=facts, gateway=gateway
    )
    method = (
        StructureDerivationMethod.TENDER_SPECIFIED
        if proposed or source_document_version_id
        else StructureDerivationMethod.TEMPLATE
    )
    if existing is not None:
        structure = existing
        structure.derived_from_document_version_id = source_document_version_id
        structure.derivation_method = method
    else:
        structure = ProposalStructure(
            org_id=ctx.org_id,
            bid_id=bid_id,
            derived_from_document_version_id=source_document_version_id,
            derivation_method=method,
            created_by=ctx.actor_user_id,
        )
        session.add(structure)
    session.flush()

    if proposed:
        placed = {aid for section in proposed for aid in section["artifact_ids"]}
        plan = list(proposed)
        # Answers the customer's structure had no place for are still printed,
        # at the end of their own submission, rather than dropped.
        for artifact in artifacts:
            if artifact.id not in placed:
                plan.append(
                    {
                        "number_label": None,
                        "title": artifact.title,
                        "stream": stream_of(facts[artifact.id]),
                        "is_mandatory": True,
                        "artifact_ids": [artifact.id],
                    }
                )
    else:
        plan = [
            {
                "number_label": None,
                "title": artifact.title,
                "stream": stream_of(facts[artifact.id]),
                "is_mandatory": True,
                "artifact_ids": [artifact.id],
            }
            for artifact in artifacts
        ]

    fill_empty_sections(
        plan,
        {a.id: a.title for a in artifacts},
        {a_id: f["categories"] for a_id, f in facts.items()},
    )
    counters: dict[DocumentStream, int] = {}
    for index, entry in enumerate(plan, start=1):
        stream = entry["stream"]
        document = _document_for(session, ctx, bid_id=bid_id, structure=structure, stream=stream)
        counters[stream] = counters.get(stream, 0) + 1
        section = ProposalSection(
            org_id=ctx.org_id,
            structure_id=structure.id,
            order_index=index,
            number_label=entry["number_label"] or str(counters[stream]),
            title=entry["title"],
            target_document_id=document.id,
            required_format=GeneratedFormat.DOCX,
            is_mandatory=entry["is_mandatory"],
        )
        session.add(section)
        session.flush()
        for order, artifact_id in enumerate(entry["artifact_ids"], start=1):
            session.add(
                SectionArtifactMap(
                    org_id=ctx.org_id,
                    section_id=section.id,
                    artifact_id=artifact_id,
                    order_index=order,
                    inclusion_mode=InclusionMode.FULL,
                )
            )
    session.flush()
    audit.record(
        session,
        ctx,
        action="proposal.structure_derived",
        entity_type="proposal_structure",
        entity_id=structure.id,
        bid_id=bid_id,
        after={
            "sections": len(plan),
            "method": structure.derivation_method.value,
            "streams": sorted(s.value for s in counters),
        },
    )
    return structure


def map_artifact(
    session: Session,
    ctx: ExecutionContext,
    *,
    section_id: uuid.UUID,
    artifact_id: uuid.UUID,
    order_index: int,
    inclusion_mode: InclusionMode,
    excerpt_statement_ids: list[uuid.UUID] | None = None,
) -> SectionArtifactMap:
    section = session.get(ProposalSection, section_id)
    artifact = session.get(ResponseArtifact, artifact_id)
    if (
        section is None
        or artifact is None
        or section.org_id != ctx.org_id
        or artifact.org_id != ctx.org_id
    ):
        raise NotFound("The section or artifact does not exist.")
    structure = _structure(session, ctx, section.structure_id)
    if artifact.bid_id != structure.bid_id:
        raise ValidationError("A section cannot map an artifact from another bid.")
    existing = session.execute(
        select(SectionArtifactMap).where(
            SectionArtifactMap.org_id == ctx.org_id,
            SectionArtifactMap.section_id == section_id,
            SectionArtifactMap.artifact_id == artifact_id,
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    row = SectionArtifactMap(
        org_id=ctx.org_id,
        section_id=section_id,
        artifact_id=artifact_id,
        order_index=order_index,
        inclusion_mode=inclusion_mode,
        excerpt_statement_ids=excerpt_statement_ids,
    )
    session.add(row)
    session.flush()
    return row


def coverage(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> dict:
    structure = structure_for_bid(session, ctx, bid_id=bid_id)
    section_rows = sections(session, ctx, structure_id=structure.id)
    mapped_section_ids = set(
        session.execute(
            select(SectionArtifactMap.section_id).where(
                SectionArtifactMap.org_id == ctx.org_id,
                SectionArtifactMap.section_id.in_([row.id for row in section_rows]),
            )
        ).scalars()
    )
    mapped_artifact_ids = set(
        session.execute(
            select(SectionArtifactMap.artifact_id)
            .join(ProposalSection, ProposalSection.id == SectionArtifactMap.section_id)
            .where(
                SectionArtifactMap.org_id == ctx.org_id,
                ProposalSection.structure_id == structure.id,
            )
        ).scalars()
    )
    from app.models import Task
    from app.services.consistency import RESOLUTION_MARK

    # Only what belongs in the proposal: not superseded, not internal
    # conflict-resolution notes, and with some text.
    artifacts = list(
        session.execute(
            select(ResponseArtifact).where(
                ResponseArtifact.org_id == ctx.org_id,
                ResponseArtifact.bid_id == bid_id,
                ResponseArtifact.state != ArtifactState.SUPERSEDED,
                ResponseArtifact.task_id.not_in(
                    select(Task.id).where(
                        Task.org_id == ctx.org_id,
                        Task.grouping_rationale.like(RESOLUTION_MARK + "%"),
                    )
                ),
                ResponseArtifact.id.in_(
                    select(ArtifactVersion.artifact_id)
                    .join(
                        ArtifactStatement,
                        ArtifactStatement.artifact_version_id == ArtifactVersion.id,
                    )
                    .where(ArtifactVersion.org_id == ctx.org_id)
                ),
            )
        ).scalars()
    )
    return {
        "uncovered_sections": [
            str(row.id)
            for row in section_rows
            if row.is_mandatory and row.id not in mapped_section_ids
        ],
        "unmapped_artifacts": [
            str(row.id) for row in artifacts if row.id not in mapped_artifact_ids
        ],
    }


def _field(paragraph, instruction: str, placeholder: str = "") -> None:
    """A Word field (page number, contents) that Word fills in when it opens the file."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def run_with(child):
        run = paragraph.add_run()
        run._r.append(child)
        return run

    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    begin.set(qn("w:dirty"), "true")
    run_with(begin)
    code = OxmlElement("w:instrText")
    code.set(qn("xml:space"), "preserve")
    code.text = f" {instruction} "
    run_with(code)
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    run_with(separate)
    paragraph.add_run(placeholder)
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run_with(end)


def _price_unapproved(
    session: Session, ctx: ExecutionContext, bid_id: uuid.UUID, storage: ObjectStorage
) -> bool:
    """A price drafted from a demonstration rate card, with no financial proposal ready."""
    priced = report_extras.financial_record(ctx, bid_id=bid_id, storage=storage)
    if priced is None:
        return False
    ready = session.execute(
        select(Deliverable.id).where(
            Deliverable.org_id == ctx.org_id,
            Deliverable.bid_id == bid_id,
            # tenders name the price document differently: financial, commercial, price schedule
            or_(
                Deliverable.name.ilike("%financial proposal%"),
                Deliverable.name.ilike("%commercial proposal%"),
                Deliverable.name.ilike("%price schedule%"),
                Deliverable.name.ilike("%pricing submission%"),
            ),
            Deliverable.status.in_([DeliverableStatus.READY, DeliverableStatus.SUBMITTED]),
        )
    ).first()
    return not ready


def _front_matter(
    document: DocxDocument,
    title: str,
    cover: dict,
    figures: bool = False,
    compact: bool = False,
) -> None:
    document.add_heading(title, 0)
    document.add_paragraph(f"Prepared for {cover['customer']}")
    document.add_paragraph(cover["tender"])
    document.add_paragraph(f"Submitted by {cover['bidder']}")
    document.add_paragraph()
    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    head = table.add_row().cells
    head[0].text = "Item"
    head[1].text = "Detail"
    _mark_header(table)
    for label, value in (
        ("Document", cover["document"]),
        ("Version", str(cover["version"])),
        ("Date", cover["date"]),
        ("Status", cover["status"]),
        ("Reference", cover["reference"]),
    ):
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value
    if compact:
        _page_furniture(document, cover)
        return
    document.add_page_break()
    document.add_heading("Contents", level=1)
    _field(
        document.add_paragraph(),
        'TOC \\o "1-2" \\h \\z \\u',
        "The contents list is filled in when this document is opened in Word.",
    )
    if figures:
        document.add_paragraph().add_run("List of figures").bold = True
        _field(
            document.add_paragraph(),
            'TOC \\h \\z \\c "Figure"',
            "The list of figures is filled in when this document is opened in Word.",
        )
    document.add_page_break()
    _page_furniture(document, cover)


def _page_furniture(document: DocxDocument, cover: dict) -> None:
    section = document.sections[0]
    header = section.header.paragraphs[0]
    header.text = f"{cover['bidder']}  |  {cover['document']}"
    footer = section.footer.paragraphs[0]
    footer.add_run("Page ")
    _field(footer, "PAGE", "1")
    footer.add_run(" of ")
    _field(footer, "NUMPAGES", "1")
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    update = OxmlElement("w:updateFields")
    update.set(qn("w:val"), "true")
    document.settings.element.append(update)


def _cover(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    name: str,
    version: int,
    output_mode: OutputMode,
) -> dict:
    details = report_extras.tender_details(session, ctx, bid_id)
    unknown = "[to be confirmed against the tender]"
    ok = details["verified"]
    return {
        "bidder": report_extras.bidder_name(session, ctx) or "[bidder name to be confirmed]",
        "customer": details["customer"] if ok else unknown,
        "tender": details["tender"] if ok else unknown,
        "reference": (
            details["reference"]
            if ok and details["reference"]
            else (report_extras.proposal_reference(session, ctx, bid_id) or "Not assigned")
        ),
        "document": name,
        "version": f"v{version}",
        "date": datetime.now(timezone.utc).strftime("%d %B %Y"),
        "status": (
            "Review copy, not for submission"
            if output_mode is OutputMode.REVIEW
            else (
                "Incomplete, not for submission: the source tender is unfinished or unverified"
                if not ok or _tender_unfinished(session, ctx, bid_id)
                else "For submission"
            )
        ),
    }


def _quarantine(text: str, terms: list[str], tables: bool = True) -> tuple[str, int]:
    """Drop the sentences that carry another project's template names (outside tables)."""
    if not terms or not text:
        return text, 0
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b")
    removed = 0
    lines: list[str] = []
    for line in text.split("\n"):
        if not pattern.search(line):
            lines.append(line)
            continue
        if line.lstrip().startswith("|"):
            if tables:
                lines.append(pattern.sub("[template name withheld]", line))
                removed += len(pattern.findall(line))
            else:
                lines.append(line)
            continue
        kept = []
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if pattern.search(sentence):
                removed += 1
            else:
                kept.append(sentence)
        lines.append(" ".join(kept))
    return "\n".join(lines), removed


def _deliverable_code(stage: str, package) -> str:
    """The finding for an undone deliverable, by the stage it belongs to."""
    mode = str(
        getattr(getattr(package, "output_mode", None), "value", getattr(package, "output_mode", ""))
    )
    if stage == "DISPATCH":
        return "DISPATCH_ITEM_BLOCKING" if mode == "SUBMISSION" else "DISPATCH_ITEM_PENDING"
    if stage == "PRE_BID":
        return "PRE_BID_NOTICE_PENDING"
    return "MANDATORY_DELIVERABLE_NOT_READY"


def _readable(storage, version) -> bool:
    try:
        storage.get(version.storage_key)
        return True
    except Exception:  # noqa: BLE001
        return False


def _tender_unfinished(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> bool:
    from app.services import package_lint

    return package_lint.tender_unfinished(session, ctx, bid_id) >= package_lint.TENDER_UNFINISHED_AT


_WIDTHS = {
    ("Reference", 4): [0.13, 0.49, 0.12, 0.26],
    ("ID", 3): [0.1, 0.2, 0.7],
    ("ID", 2): [0.1, 0.9],
}


def _landscape(document, on: bool) -> None:
    """Start a new page in landscape (for a wide figure) or back in portrait."""
    from docx.enum.section import WD_ORIENT, WD_SECTION

    section = document.add_section(WD_SECTION.NEW_PAGE)
    want = WD_ORIENT.LANDSCAPE if on else WD_ORIENT.PORTRAIT
    if section.orientation != want:
        section.orientation = want
        section.page_width, section.page_height = section.page_height, section.page_width


def _mark_header(table) -> None:
    """The first row is a header row: it repeats on each page and is read as headers."""
    from docx.oxml import OxmlElement

    row = table.rows[0]
    properties = row._tr.get_or_add_trPr()
    properties.append(OxmlElement("w:tblHeader"))
    for cell in row.cells:
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True


def _set_widths(table, header: list[str], columns: int, total: float = 6.4) -> None:
    weights = _WIDTHS.get((header[0], columns)) if header else None
    if total > 7 and (header[0], columns) == ("Reference", 4):
        weights = [0.17, 0.43, 0.19, 0.21]
    if not weights or len(weights) != columns:
        return
    from docx.shared import Inches

    table.autofit = False
    for index, column in enumerate(table.columns):
        column.width = Inches(total * weights[index])  # the grid, which LibreOffice also reads
    for row in table.rows:
        for index, cell in enumerate(row.cells):
            cell.width = Inches(total * weights[index])


def _chunks(paragraph: str, limit: int = 170) -> list[str]:
    """A very long paragraph, cut at sentence ends into readable pieces (no words change)."""
    if len(paragraph.split()) <= limit * 1.4:
        return [paragraph]
    sentences = re.split(r"(?<=[.;:!?])\s+", paragraph)
    out: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in sentences:
        words = len(sentence.split())
        if current and count + words > limit:
            out.append(" ".join(current))
            current, count = [], 0
        current.append(sentence)
        count += words
    if current:
        out.append(" ".join(current))
    return out


class _Heading(Protocol):
    """What the renderer reads of a heading: a proposal section, or an appendix stand-in."""

    @property
    def number_label(self) -> str | None: ...

    @property
    def title(self) -> str: ...


def _render_docx(
    title: str,
    rows: list[tuple[_Heading, str]],
    cover: dict | None = None,
    storage: ObjectStorage | None = None,
    compact: bool = False,
    landscape: bool = False,
) -> bytes:
    document = WordDocument()
    if landscape:
        from docx.enum.section import WD_ORIENT

        first = document.sections[0]
        first.page_width, first.page_height = first.page_height, first.page_width
        first.orientation = WD_ORIENT.LANDSCAPE
    figure_no = 0
    if cover is not None:
        has_figures = any("[[FIGURE:" in text for _s, text in rows)
        _front_matter(document, title, cover, figures=has_figures, compact=compact)
    else:
        document.add_heading(title, 0)
    for section, text in rows:
        if section.title.startswith("Terms and Conditions"):
            document.add_page_break()
        document.add_heading(
            f"{section.number_label + ' ' if section.number_label else ''}{section.title}",
            level=1,
        )
        for paragraph in text.split("\n\n"):
            figure = report_figures.MARKER.match(paragraph.strip())
            if figure:
                if storage is not None and storage.exists(figure.group(1)):
                    from docx.enum.text import WD_ALIGN_PARAGRAPH
                    from docx.shared import Inches

                    figure_no += 1
                    picture = storage.get(figure.group(1))
                    from PIL import Image

                    with Image.open(io.BytesIO(picture)) as img:
                        ratio = img.width / img.height
                    wide = ratio > 1.35
                    if wide:
                        _landscape(document, True)
                    width_in = min(9.0, 5.4 * ratio) if wide else min(6.3, 8.0 * ratio)
                    document.add_picture(io.BytesIO(picture), width=Inches(width_in))
                    holder = document.paragraphs[-1]
                    holder.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    holder.paragraph_format.keep_with_next = True
                    text = figure.group(2).strip()
                    properties = document.inline_shapes[-1]._inline.docPr
                    properties.set("descr", (figure.group(3) or f"Diagram: {text}")[:900])
                    properties.set("title", f"Figure {figure_no}")
                    caption = document.add_paragraph(style="Caption")
                    caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    caption.add_run("Figure ")
                    _field(caption, "SEQ Figure \\* ARABIC", str(figure_no))
                    caption.add_run(f": {text}")
                    if wide:
                        _landscape(document, False)
            elif paragraph.startswith("### "):
                document.add_heading(paragraph[4:].strip(), level=2)
            elif paragraph.startswith("|"):
                grid = [
                    [cell.strip() for cell in line.strip().strip("|").split("|")]
                    for line in paragraph.split("\n")
                    if line.startswith("|")
                ]
                width = max(len(r) for r in grid)
                table = document.add_table(rows=len(grid), cols=width)
                table.style = "Table Grid"
                _set_widths(table, grid[0], width, 9.0 if landscape else 6.4)
                for r, cells in enumerate(grid):
                    for c, value in enumerate(cells):
                        table.cell(r, c).text = value
                _mark_header(table)
            elif paragraph.startswith("- "):
                for line in paragraph.split("\n"):
                    document.add_paragraph(line[2:].strip(), style="List Bullet")
            elif paragraph.strip():
                for chunk in _chunks(paragraph):
                    document.add_paragraph(chunk)
    if cover is not None and not compact:
        _fill_field_results(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


_TOC_PLACEHOLDERS = (
    "The contents list is filled in when this document is opened in Word.",
    "The list of figures is filled in when this document is opened in Word.",
)


def _fill_field_results(document: DocxDocument) -> None:
    """Give the contents and figure lists a real stored result.

    Word rebuilds both lists, with page numbers, when it opens the file. A reader that
    does not run fields (a viewer, a text extractor) otherwise sees an instruction in
    their place, so the stored result is the chapter and figure names themselves.
    """
    chapters = [
        p.text.strip()
        for p in document.paragraphs
        if p.style is not None
        and p.style.name == "Heading 1"
        and p.text.strip()
        and p.text.strip() != "Contents"
    ]
    figures = [
        p.text.strip()
        for p in document.paragraphs
        if p.style is not None and p.style.name == "Caption"
    ]
    for paragraph in document.paragraphs:
        for run in paragraph.runs:
            if run.text == _TOC_PLACEHOLDERS[0] and chapters:
                run.text = "\n".join(chapters)
            elif run.text == _TOC_PLACEHOLDERS[1] and figures:
                run.text = "\n".join(figures)


def render_version(
    session: Session, ctx: ExecutionContext, *, version: ArtifactVersion, output_mode: OutputMode
) -> str:
    """An approved answer as it will read in this mode.

    SUBMISSION is the customer's copy: the approved words and nothing else.
    REVIEW is the company's copy of the same words with every claim's footing
    beside it -- which evidence, its status, which role approved a promise, what
    is an assumption -- so a reviewer can check the proposal line by line. They
    were identical before; only the filename differed.
    """
    statements = list(
        session.execute(
            select(ArtifactStatement)
            .where(
                ArtifactStatement.org_id == ctx.org_id,
                ArtifactStatement.artifact_version_id == version.id,
            )
            .order_by(ArtifactStatement.order_index)
        ).scalars()
    )
    if not statements:
        return version.rendered_text or ""
    from app.services.drafting import strip_commitment_note

    approved_promises = {
        row.id
        for row in session.execute(
            select(Commitment).where(
                Commitment.org_id == ctx.org_id,
                Commitment.id.in_([s.commitment_id for s in statements if s.commitment_id]),
                Commitment.approval_status == ApprovalStatus.APPROVED,
            )
        ).scalars()
    }

    # A fact whose evidence comes from a document that has since been replaced, or that
    # says it is a test fixture, no longer has a source. It is withheld here, by where it
    # came from, rather than guessed at afterwards from its wording.
    stale_versions = report_attachments.superseded_version_ids(
        session, ctx
    ) | report_attachments.fixture_version_ids(session, ctx)
    cited = {eid for s in statements for eid in (s.evidence_ids or [])}
    stale_evidence = (
        {
            row
            for row in session.execute(
                select(Evidence.id).where(
                    Evidence.org_id == ctx.org_id,
                    Evidence.id.in_(list(cited)),
                    Evidence.source_document_version_id.in_(list(stale_versions) or [None]),
                )
            ).scalars()
        }
        if cited and stale_versions
        else set()
    )

    def words(statement: ArtifactStatement) -> str:
        if (
            statement.statement_class is StatementClass.EVIDENCE_BACKED_FACT
            and statement.evidence_ids
            and all(eid in stale_evidence for eid in statement.evidence_ids)
        ):
            return WITHHELD
        # The approver's note on a promise is dropped once that promise is approved.
        if statement.commitment_id in approved_promises:
            return strip_commitment_note(statement.text)
        return statement.text

    if output_mode is not OutputMode.REVIEW:
        return "\n\n".join(words(statement) for statement in statements)

    lines: list[str] = []
    for statement in statements:
        marker = ""
        klass = statement.statement_class
        if klass is StatementClass.EVIDENCE_BACKED_FACT:
            evidence = list(
                session.execute(
                    select(Evidence).where(
                        Evidence.org_id == ctx.org_id,
                        Evidence.id.in_(list(statement.evidence_ids or []) or [None]),
                    )
                ).scalars()
            )
            marker = "; ".join(
                f"E-{str(row.id)[:8]} {row.status.value.replace('_', ' ').title()}"
                + (f", expires {row.expiry_date.isoformat()}" if row.expiry_date else "")
                for row in evidence
            )
            marker = f"[Evidence: {marker}]" if marker else "[Evidence: none found]"
        elif klass is StatementClass.PROPOSED_COMMITMENT:
            commitment = session.get(Commitment, statement.commitment_id)
            if commitment is not None and commitment.approval_status is ApprovalStatus.APPROVED:
                marker = f"[Proposed commitment - approved by {commitment.required_approver_role}]"
            else:
                marker = "[Proposed commitment - NOT YET APPROVED]"
        elif klass is StatementClass.ASSUMPTION:
            marker = "[Assumption]"
        elif klass is StatementClass.TENDER_DERIVED:
            marker = "[From the tender]"
        elif klass is StatementClass.DIAGRAM_DESCRIPTION:
            marker = "[Describes a diagram]"
        elif klass is StatementClass.HUMAN_PROVIDED_INPUT:
            marker = "[Provided by a contributor]"
        shown = words(statement)
        lines.append(shown if shown == WITHHELD else f"{shown} {marker}".strip())
    return "\n\n".join(lines)


def _tainted_answers(
    session: Session,
    ctx: ExecutionContext,
    bid_id: uuid.UUID,
    facts: dict[str, str],
    artifacts: list[ResponseArtifact] | None = None,
) -> dict[str, list[str]]:
    """Approved answers (by title) that state facts found only in fixture documents."""
    if not facts:
        return {}
    if artifacts is None:
        artifacts = list(
            session.execute(
                select(ResponseArtifact).where(
                    ResponseArtifact.org_id == ctx.org_id,
                    ResponseArtifact.bid_id == bid_id,
                    ResponseArtifact.state != ArtifactState.SUPERSEDED,
                    ResponseArtifact.approved_version_id.is_not(None),
                )
            ).scalars()
        )
    held: dict[str, list[str]] = {}
    for artifact in artifacts:
        if artifact.approved_version_id is None:
            continue
        for statement in session.execute(
            select(ArtifactStatement).where(
                ArtifactStatement.org_id == ctx.org_id,
                ArtifactStatement.artifact_version_id == artifact.approved_version_id,
            )
        ).scalars():
            hit = report_attachments.tainted(statement.text, facts, minimum=1)
            if not hit and APPROVED_CLAIM.search(statement.text):
                hit = ["an approved-record claim"]
            if hit:
                held.setdefault(artifact.title, []).extend(hit)
    return held


# Wording that claims a company record was approved; with fixtures only, none was.
APPROVED_CLAIM = re.compile(
    r"\b(?:our|the) approved (?:project )?(?:reference|evidence|record|cv)s?\b"
    r"|\b(?:our|we have|we)\s+(?:delivered|verified|recorded|implemented|completed)\b"
    r"|\bour (?:delivered|verified|recorded) \w+"
    r"|\b(?:results|records?|evidence|references?) (?:that )?we (?:hold|have)\b"
    # Claims that only evidence can back: experience, named people, the company's own terms.
    r"|\b(?:directly )?comparable[\s-](?:delivery|experience|projects?|portals?)\b"
    r"|\bnamed (?:key )?(?:personnel|staff)\b|\bsubstitution requir\w+"
    r"|\bour standard (?:terms|warranty|confidentiality|liability|payment)\b"
    # Experience, availability and results stated as already held, and the terms' payment clause.
    r"|\bwhat we have is\b|\bwe have (?:experience|integrated|built|run|tested|delivered)\b"
    r"|\bcase studies\b|\bavailable from the (?:project )?start\b"
    r"|\bnet\s*30\b|\bwritten variation\b"
    # Delivered work, references on request, past integrations, owned code and licences.
    r"|\b(?:have|has) delivered\b|\bdelivered\b[^.]{0,80}\bportals?\b"
    r"|\breferences?\b[^.]{0,60}\bon request\b|\bSales\b[^.]{0,40}\b(?:hold|holds|has)\b"
    r"|\b(?:previously|already) (?:performed|integrated|done|delivered|built|implemented)\b"
    r"|\bhave (?:performed|done|previously)\b"
    r"|\b(?:in-house|own|owned)\b[\w\s./#+-]{0,40}\b(?:code ?base|workflow engine|credential "
    r"engine|engine)\b"
    # Company governance facts that a responsible person must confirm, not the system.
    r"|\bstandard mutual NDA\b|\b(?:Managing|Commercial) Director\b[^.]{0,60}\b(?:will sign"
    r"|signing|delegat\w+)"
    r"|\bdelegated (?:signing|signature|authority)\b|\bboard delegation\b"
    r"|\breview slots?\b[^.]{0,40}\b(?:booked|reserved)\b|\b(?:booked"
    r"|reserved)\b[^.]{0,40}\breview slots?\b"
    r"|\bcommercially licen[cs]ed\b|\bno GPL\b|\bMIT and Apache\b",
    re.IGNORECASE,
)
CELL_WITHHELD = "[Withheld: unverified fixture evidence]"
WITHHELD = "[Withheld: this statement rests on unverified fixture evidence.]"


def _rests_on_fixtures(
    text: str, facts: dict[str, str], shingles: frozenset[str] | set[str] = frozenset()
) -> bool:
    return bool(
        report_attachments.tainted(text, facts, minimum=1)
        or APPROVED_CLAIM.search(text)
        or report_attachments.copies_fixture(text, shingles)
    )


def _withhold_in_table(
    table: str, facts: dict[str, str], shingles: frozenset[str] | set[str] = frozenset()
) -> tuple[str, int]:
    """A table row whose cells state fixture facts keeps its first cell (the label) and
    shows a marker in every other cell that does."""
    lines = []
    count = 0
    for index, line in enumerate(table.split("\n")):
        if index == 0 or not line.startswith("|") or set(line) <= set("|-: "):
            lines.append(line)
            continue
        cells = line.strip().strip("|").split("|")
        for position, cell in enumerate(cells):
            if position and _rests_on_fixtures(cell, facts, shingles):
                cells[position] = " " + CELL_WITHHELD + " "
                count += 1
        lines.append("|" + "|".join(cells) + "|")
    return "\n".join(lines), count


def _drop_empty_headings(parts: list[str]) -> list[str]:
    """A heading left with nothing but withheld markers under it is removed with them."""
    out: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part.startswith("#"):
            # The same heading twice in a row is one heading.
            nxt = index + 1
            while nxt < len(parts) and not parts[nxt].strip():
                nxt += 1
            if (
                nxt < len(parts)
                and parts[nxt].startswith("#")
                and (parts[nxt].lstrip("# ").strip().lower() == part.lstrip("# ").strip().lower())
            ):
                index += 1
                continue
        if part.startswith("### "):
            end = index + 1
            while end < len(parts) and not parts[end].startswith("#"):
                end += 1
            body = [p for p in parts[index + 1 : end] if p.strip()]
            if body and all(p == WITHHELD for p in body):
                index = end
                continue
        out.append(part)
        index += 1
    return out


def _withhold_fixture_claims(
    parts: list[str],
    facts: dict[str, str],
    shingles: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[str], int]:
    """Paragraphs and table cells that state fixture-only facts are replaced by a marker.

    The words were approved, but they describe people, customers and results that exist
    only in a test fixture. Code, not the reader, decides they do not go into a proposal.
    """
    out: list[str] = []
    withheld = 0
    for part in parts:
        for paragraph in part.split("\n\n"):
            if paragraph.startswith("|"):
                paragraph, cells = _withhold_in_table(paragraph, facts, shingles)
                withheld += cells
                out.append(paragraph)
            elif report_figures.MARKER.match(paragraph.strip()):
                out.append(paragraph)  # a diagram is judged by its own checks
            elif not paragraph.startswith("#") and _rests_on_fixtures(paragraph, facts, shingles):
                withheld += 1
                if not out or out[-1] != WITHHELD:
                    out.append(WITHHELD)
            else:
                out.append(paragraph)
    return _drop_empty_headings(out), withheld


NO_CONTENT = "[No approved answer for this section yet]"


def assemble(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    output_mode: OutputMode,
    storage: ObjectStorage | None = None,
    join: bool = False,
) -> list[GeneratedDocumentVersion]:
    """Render every submission of this bid -- technical and commercial -- from approved answers.

    Only approved versions are printed. A section with nothing approved yet is
    printed with a visible gap in REVIEW mode and left empty in SUBMISSION mode;
    either way `validate_package` blocks release until it is filled. Assembly
    no longer refuses outright, because a reviewer needs to SEE the draft
    proposal with its gaps to know what is left.
    """
    try:
        from app.services import requirements as _requirements

        _requirements.recheck_customer_owned(session, ctx, bid_id=bid_id)
    except Exception as exc:  # noqa: BLE001 - advisory; assembly never depends on it
        import structlog

        structlog.get_logger("release").warning("owner_recheck_failed", reason=type(exc).__name__)
    structure = structure_for_bid(session, ctx, bid_id=bid_id)
    section_rows = sections(session, ctx, structure_id=structure.id)
    object_storage = storage or get_storage()
    if output_mode is OutputMode.SUBMISSION and _price_unapproved(
        session, ctx, bid_id, object_storage
    ):
        # The submission copy has no draft-price notice, so a price nobody approved
        # would read as final. Review copies still build, with the notice.
        raise StateConflict(
            "The price was calculated from a demonstration rate card and has not been "
            "approved. A submission copy is not built until the financial proposal is "
            "approved and marked ready."
        )
    if output_mode is OutputMode.SUBMISSION:
        from app.services import package_lint as _lint

        _unfinished = _lint.tender_unfinished(session, ctx, bid_id)
        if _unfinished >= _lint.TENDER_UNFINISHED_AT:
            raise StateConflict(
                f"The source tender has {_unfinished} unfinished fields (blank lines, dotted "
                "placeholders or template titles), so a submission copy is not built. Ask the "
                "customer for the final tender and the missing information; build a review "
                "copy in the meantime."
            )
    evidence = report_attachments.selected(session, ctx, bid_id)
    if output_mode is OutputMode.SUBMISSION:
        fixtures = [a.title for a in evidence if a.fixture]
        if fixtures:
            raise StateConflict(
                "These attachments declare themselves test fixtures and cannot go to a "
                "customer: " + "; ".join(fixtures) + ". Replace them with approved company "
                "documents."
            )
        facts = report_attachments.fixture_only_facts(session, ctx, bid_id)
        claims = _tainted_answers(session, ctx, bid_id, facts)
        if claims:
            raise StateConflict(
                f"{len(claims)} approved answers state facts found only in fixture documents "
                "(" + "; ".join(sorted(claims)[:5]) + "). Replace the fixtures and regenerate "
                "or reapprove these answers before a submission copy is built."
            )
        uncleared = [a.title for a in evidence if not a.cleared]
        if uncleared:
            raise StateConflict(
                "These attachments have not been approved and cleared for a customer by a "
                "person: " + "; ".join(uncleared) + "."
            )
    documents = {
        d.id: d
        for d in session.execute(
            select(GeneratedDocument).where(
                GeneratedDocument.org_id == ctx.org_id,
                GeneratedDocument.bid_id == bid_id,
            )
        ).scalars()
    }
    # Sections from before streams existed have no document; they are technical.
    fallback = _document_for(
        session, ctx, bid_id=bid_id, structure=structure, stream=DocumentStream.TECHNICAL
    )
    documents.setdefault(fallback.id, fallback)

    rendered: dict[uuid.UUID, list[tuple[_Heading, str]]] = {}
    fixture_facts = (
        report_attachments.fixture_only_facts(session, ctx, bid_id)
        if output_mode is OutputMode.REVIEW
        else {}
    )

    fixture_runs = (
        report_attachments.fixture_shingles(session, ctx, bid_id)
        if output_mode is OutputMode.REVIEW
        else set()
    )
    flow_figures: list[str] = []
    traces: dict[uuid.UUID, list[dict]] = {}
    for section in section_rows:
        document_id = section.target_document_id or fallback.id
        mappings = list(
            session.execute(
                select(SectionArtifactMap)
                .where(
                    SectionArtifactMap.org_id == ctx.org_id,
                    SectionArtifactMap.section_id == section.id,
                )
                .order_by(SectionArtifactMap.order_index)
            ).scalars()
        )
        text_parts: list[str] = []
        approved_here: list[ResponseArtifact] = []
        for mapping in mappings:
            artifact = session.get(ResponseArtifact, mapping.artifact_id)
            if artifact is None or artifact.org_id != ctx.org_id:
                raise StateConflict("A mapped artifact no longer exists.")
            if artifact.state is not ArtifactState.APPROVED or not artifact.approved_version_id:
                continue
            approved_here.append(artifact)
            approved_version = session.get(ArtifactVersion, artifact.approved_version_id)
            if (
                approved_version is None
                or approved_version.state is not ArtifactVersionState.APPROVED
            ):
                raise StateConflict("The approved artifact pointer is inconsistent.")
            joined = (
                report_edit.edited_text(
                    ctx, bid_id=bid_id, version_id=approved_version.id, storage=object_storage
                )
                if join
                else None
            )
            if joined is not None:
                text_parts.append(f"### {artifact.title}\n\n{joined}")
            else:
                text = render_version(
                    session, ctx, version=approved_version, output_mode=output_mode
                )
                text_parts.append(report_edit.clean_for_report(text) if join else text)
            traces.setdefault(document_id, []).append(
                {
                    "section_id": str(section.id),
                    "artifact_id": str(artifact.id),
                    "artifact_version_id": str(approved_version.id),
                }
            )
        if join and approved_here and documents[document_id].stream is DocumentStream.TECHNICAL:
            # One edited chapter in place of the separate answers, when it has been
            # written and still matches them.
            chapter = report_consolidate.chapter_text(
                session,
                ctx,
                bid_id=bid_id,
                section=section,
                artifacts=approved_here,
                storage=object_storage,
                marks=output_mode is OutputMode.REVIEW,
                appendix_out=flow_figures,
            )
            if chapter:
                text_parts = [chapter]
        if not text_parts and join and "executive summary" in section.title.casefold():
            summary = report_edit.summary_text(ctx, bid_id=bid_id, storage=object_storage)
            if summary:
                text_parts.append(summary)
        if not text_parts and output_mode is OutputMode.REVIEW:
            text_parts.append(NO_CONTENT)
        if join and "financial" in section.title.casefold():
            if documents[document_id].stream is DocumentStream.COMMERCIAL:
                # The customer's financial proposal is built from the drafted price,
                # not from the answers about it, which describe the proposal.
                page = report_extras.proposal_page(
                    session,
                    ctx,
                    bid_id=bid_id,
                    storage=object_storage,
                    output_mode=output_mode,
                )
                if page:
                    text_parts = [page]
        if (fixture_facts or fixture_runs) and documents[document_id].stream in (
            DocumentStream.TECHNICAL,
            DocumentStream.COMMERCIAL,
        ):
            # Whatever the section is made of (approved answers or a summary of them),
            # a paragraph that rests on a fixture's facts is not shown. The bidder's own
            # name is a fixture fact too, so the price document is judged on copied
            # wording only.
            technical = documents[document_id].stream is DocumentStream.TECHNICAL
            text_parts, withheld = _withhold_fixture_claims(
                text_parts, fixture_facts if technical else {}, fixture_runs
            )
            if withheld:
                text_parts.insert(
                    0,
                    f"Review note ({section.title}): {withheld} "
                    + ("item" if withheld == 1 else "items")
                    + " in this section rested on facts found only in fixture documents and "
                    + ("was" if withheld == 1 else "were")
                    + " withheld. Replace the fixtures, then regenerate this answer.",
                )
        rendered.setdefault(document_id, []).append((section, "\n\n".join(text_parts)))

    # Long reference tables go out as their own files, listed in the proposal.
    separate_files: list[tuple[str, str]] = []
    if join:
        for document_id, rows in rendered.items():
            seen: set[str] = set()
            kept = []
            if documents[document_id].stream is DocumentStream.COMMERCIAL:
                # The price page repeats its figures on purpose (page and annex).
                continue
            for section, text in rows:
                paragraphs = []
                for paragraph in text.split("\n\n"):
                    key = " ".join(paragraph.casefold().split())
                    if len(key) > 60 and key in seen:
                        continue
                    seen.add(key)
                    paragraphs.append(paragraph)
                kept.append((section, "\n\n".join(paragraphs)))
            rendered[document_id] = kept
        matrix = report_extras.matrix_text(ctx, bid_id=bid_id, storage=object_storage)
        if matrix:
            matrix = report_extras.complete_matrix(session, ctx, bid_id, matrix)
        technical_id = next(
            (d for d in rendered if documents[d].stream is DocumentStream.TECHNICAL),
            None,
        )
        if technical_id is not None:
            appendices: list[tuple[str, str]] = []
            if matrix and (fixture_facts or fixture_runs):
                matrix, _cells = _withhold_in_table(matrix, fixture_facts, fixture_runs)
            if matrix:
                appendices.append(
                    (
                        "Evaluation criteria cross-reference and fair comparison matrix",
                        "This table sets the customer's evaluation factors against the "
                        "chapters of this proposal that answer them.\n\n" + matrix,
                    )
                )
            priced = report_extras.financial_record(ctx, bid_id=bid_id, storage=object_storage)
            compliance = report_compliance.matrix_text(
                session,
                ctx,
                bid_id=bid_id,
                customer=output_mode is not OutputMode.REVIEW,
                quoted=frozenset((priced or {}).get("separate") or {}),
            )
            if compliance:
                separate_files.append(
                    (
                        "Requirements compliance matrix",
                        "Each requirement the tender places on us, and the chapter that "
                        "answers it.\n\n" + compliance,
                    )
                )
            customer_copy = output_mode is not OutputMode.REVIEW
            register = report_consolidate.register_text(
                ctx, bid_id=bid_id, storage=object_storage, submission=customer_copy
            )
            if register:
                separate_files.append(
                    (
                        "Assumptions and dependencies",
                        (
                            "The assumptions on which this proposal rests that affect "
                            "price, schedule, responsibility or acceptance."
                            if customer_copy
                            else "Every assumption, with its type. Only the customer "
                            "dependencies and commercial assumptions go into the "
                            "submitted copy; settle or remove the rest before submission."
                        )
                        + "\n\n"
                        + register,
                    )
                )
            if flow_figures and output_mode is OutputMode.REVIEW:
                # The RFP does not ask for them and each repeats a chapter figure's topic,
                # so the submission copy leaves them out.
                appendices.append(
                    (
                        "Process flows",
                        "The process flows behind the architecture diagrams in the "
                        "chapters above.\n\n" + "\n\n".join(flow_figures),
                    )
                )
            if evidence or separate_files:
                appendices.append(
                    (
                        "Attachments",
                        _attachment_list(
                            [t for t, _b in separate_files],
                            evidence,
                            output_mode,
                            report_attachments.withheld_documents(session, ctx, bid_id)
                            if output_mode is OutputMode.REVIEW
                            else [],
                        ),
                    )
                )
            for letter, (name, body) in zip("ABCDEFGHIJ", appendices, strict=False):
                rendered[technical_id].append(
                    (SimpleNamespace(number_label=f"Appendix {letter}:", title=name), body)
                )

    # Names that belong to another project's template are kept out of the proposal text; they are
    # raised with the customer in the clarification register instead.
    try:
        from app.services import package_lint as _pl

        _blob = [t for rows in rendered.values() for _s, t in rows] + [
            b for _t, b in separate_files
        ]
        _terms = _pl.template_residue(session, ctx, bid_id, _blob)
        if _terms:
            _total = 0
            for _doc_id, _rows in list(rendered.items()):
                _new = []
                for _sec, _txt in _rows:
                    _txt2, _n = _quarantine(_txt, _terms)
                    _total += _n
                    _new.append((_sec, _txt2))
                rendered[_doc_id] = _new
            _sep = []
            for _t, _b in separate_files:
                _b2, _n = _quarantine(
                    _b, _terms, tables=not _t.startswith("Requirements compliance")
                )
                _total += _n
                _sep.append((_t, _b2))
            separate_files[:] = _sep
            if _total and output_mode is OutputMode.REVIEW:
                _tid = next(
                    (d for d in rendered if documents[d].stream is DocumentStream.TECHNICAL), None
                )
                if _tid is not None and rendered[_tid]:
                    _first, _txt = rendered[_tid][0]
                    rendered[_tid][0] = (
                        _first,
                        f"Review note: {_total} sentence(s) naming {', '.join(_terms)} were "
                        f"withheld. These names "
                        "come from template wording in the tender, not from this customer; they "
                        "are raised in the "
                        "clarification register.\n\n" + _txt,
                    )
    except Exception:  # noqa: BLE001, S110 - advisory
        pass

    produced: list[GeneratedDocumentVersion] = []
    for document_id, rows in rendered.items():
        generated = documents[document_id]
        next_version = (
            session.execute(
                select(func.max(GeneratedDocumentVersion.version_no)).where(
                    GeneratedDocumentVersion.org_id == ctx.org_id,
                    GeneratedDocumentVersion.generated_document_id == generated.id,
                )
            ).scalar_one()
            or 0
        ) + 1
        title = generated.name + (" (review copy)" if output_mode is OutputMode.REVIEW else "")
        cover = (
            _cover(
                session,
                ctx,
                bid_id=bid_id,
                name=generated.name,
                version=next_version,
                output_mode=output_mode,
            )
            if join
            else None
        )
        # The customer's financial proposal may be at most two pages, so its submission
        # copy carries no cover page or contents page of its own.
        compact = (
            output_mode is OutputMode.SUBMISSION and generated.stream is DocumentStream.COMMERCIAL
        )
        content = _render_docx(title, rows, cover=cover, storage=object_storage, compact=compact)
        digest = hashlib.sha256(content).hexdigest()
        key = ObjectStorage.tenant_key(
            ctx.org_id,
            "generated",
            str(bid_id),
            str(generated.id),
            f"v{next_version}-{output_mode.value.lower()}.docx",
        )
        object_storage.put(
            key,
            content,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        generated_version = GeneratedDocumentVersion(
            org_id=ctx.org_id,
            generated_document_id=generated.id,
            version_no=next_version,
            output_mode=output_mode,
            storage_key=key,
            sha256=digest,
            byte_size=len(content),
            generated_at=datetime.now(timezone.utc),
            generated_by=ctx.actor_user_id,
            generation_trace={
                "artifacts": traces.get(document_id, []),
                "ruleset": RULESET_VERSION,
                "stream": generated.stream.value,
            },
        )
        session.add(generated_version)
        session.flush()
        generated.current_version_id = generated_version.id
        audit.record(
            session,
            ctx,
            action="proposal.assembled",
            entity_type="generated_document_version",
            entity_id=generated_version.id,
            bid_id=bid_id,
            after={
                "sha256": digest,
                "bytes": len(content),
                "mode": output_mode.value,
                "stream": generated.stream.value,
            },
        )
        produced.append(generated_version)
    produced.extend(
        _record_versions(
            session,
            ctx,
            bid_id=bid_id,
            structure=structure,
            records=[
                {
                    "title": title,
                    "group": title,
                    "body": body,
                    "review_line": None,
                    "source": "generated-landscape"
                    if title.startswith("Requirements compliance")
                    else "generated",
                    "landscape": title.startswith("Requirements compliance"),
                    "trace": {"generated": True},
                }
                for title, body in separate_files
            ],
            output_mode=output_mode,
            storage=object_storage,
        )
    )
    produced.extend(
        _attachment_versions(
            session,
            ctx,
            bid_id=bid_id,
            structure=structure,
            evidence=evidence,
            output_mode=output_mode,
            storage=object_storage,
        )
    )
    _retire_unproduced(session, ctx, bid_id=bid_id, produced=produced)
    # Technical first: callers index [0] for the main proposal.
    produced.sort(
        key=lambda v: (v.generation_trace or {}).get("stream") != DocumentStream.TECHNICAL.value
    )
    return produced


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "attachment"


def _attachment_name(attachment) -> str:
    return f"Attachment: {attachment.title}"


def _attachment_list(generated: list[str], evidence, output_mode: OutputMode, withheld=()) -> str:
    lines = [
        "Each document below is supplied as its own file, with the proposal.",
    ]
    for number, title in enumerate(generated, start=1):
        lines.append(f"- Attachment {number}: {title} (file {_slug(title)}.docx)")
    for number, attachment in enumerate(evidence, start=len(generated) + 1):
        line = (
            f"- Attachment {number}: {attachment.title} "
            f"(file {_slug(attachment.title)}.docx; {attachment.group.lower()})"
        )
        if output_mode is OutputMode.REVIEW:
            state = "cleared for a customer" if attachment.cleared else "NOT cleared"
            if attachment.fixture:
                state = "TEST FIXTURE, blocked from any submission"
            line += f" [review: {attachment.status.value.lower()}, {state}]"
        lines.append(line)
    if withheld:
        lines.append(
            "[review] Company documents considered and not attached: "
            + "; ".join(f"{title} ({group.lower()}): {reason}" for title, group, reason in withheld)
            + "."
        )
    return "\n\n".join(lines)


def _attachment_body(text: str) -> str:
    """An attached company document's body as Word-ready paragraphs, word for word.

    The first line is the document's own title, which the file shows as its title, so it
    is dropped (and only that line: a document written without blank lines is one block).
    Markdown marks are turned into what Word shows as headings, bullets and tables; no
    word is added, changed or removed.
    """
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines:
        lines.pop(0)
    out: list[str] = []
    table: list[str] = []

    def flush() -> None:
        if table:
            out.append("\n".join(table))
            table.clear()

    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append(" ".join(para))
            para.clear()

    for raw in lines:
        line = raw.strip()
        if not line:
            flush()
            flush_para()
        elif line.startswith("|"):
            flush_para()
            if re.fullmatch(r"\|[\s:|-]+\|?", line):
                continue
            table.append(line if line.endswith("|") else line + " |")
        elif re.match(r"#{1,6}\s", line):
            flush()
            flush_para()
            out.append("### " + re.sub(r"^#{1,6}\s+", "", line))
        elif re.match(r"[-*]\s+", line):
            flush()
            flush_para()
            out.append("- " + re.sub(r"^[-*]\s+", "", line))
        else:
            flush()
            para.append(line)
    flush()
    flush_para()
    return "\n\n".join(out)


def _retire_unproduced(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, produced: list
) -> None:
    """An attachment whose source document was replaced or is no longer asked for must
    leave the package: its old file would otherwise ride along with the new ones.

    Decided once, against everything this build produced. Deciding inside each group of
    files retired the other group's documents and gave them a new version every build.
    """
    made = {v.generated_document_id for v in produced}
    for stale in session.execute(
        select(GeneratedDocument).where(
            GeneratedDocument.org_id == ctx.org_id,
            GeneratedDocument.bid_id == bid_id,
            GeneratedDocument.stream == DocumentStream.SUPPORTING,
            GeneratedDocument.name.like("Attachment: %"),
            GeneratedDocument.current_version_id.is_not(None),
        )
    ).scalars():
        if stale.id not in made:
            stale.current_version_id = None
            audit.record(
                session,
                ctx,
                action="attachment.retired",
                entity_type="generated_document",
                entity_id=stale.id,
                bid_id=bid_id,
                after={"name": stale.name},
            )


def _attachment_versions(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    structure,
    evidence,
    output_mode: OutputMode,
    storage: ObjectStorage,
) -> list[GeneratedDocumentVersion]:
    """One file per evidence attachment, each with its own versions."""
    records = []
    for attachment in evidence:
        body = _attachment_body(attachment.body)
        review_line = (
            "TEST FIXTURE: blocked from any submission."
            if attachment.fixture
            else f"Evidence {attachment.status.value.lower()}; "
            f"use {attachment.scope.value.lower().replace('_', ' ')}."
        )
        records.append(
            {
                "title": attachment.title,
                "group": attachment.group,
                "body": body,
                "review_line": review_line,
                "source": "\n".join(
                    [
                        attachment.body,
                        str(attachment.version_id),
                        attachment.status.value,
                        attachment.scope.value,
                        attachment.provenance or "",
                    ]
                ),
                "trace": {
                    "source_document_id": str(attachment.document_id),
                    "source_document_version_id": str(attachment.version_id),
                    "evidence_status": attachment.status.value,
                    "use_scope": attachment.scope.value,
                    "provenance": attachment.provenance,
                    "fixture": attachment.fixture,
                },
            }
        )
    return _record_versions(
        session,
        ctx,
        bid_id=bid_id,
        structure=structure,
        records=records,
        output_mode=output_mode,
        storage=storage,
    )


def _record_versions(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    structure,
    records: list[dict],
    output_mode: OutputMode,
    storage: ObjectStorage,
) -> list[GeneratedDocumentVersion]:
    """A separate, versioned file for each record.

    A new version is made only when what it holds changed: its text, its source, or the
    decision a person recorded about it. So an unchanged attachment keeps its version
    number from build to build, and a package names exactly which version it carries.
    """
    produced: list[GeneratedDocumentVersion] = []
    for record in records:
        title = record["title"]
        name = f"Attachment: {title}"
        document = session.execute(
            select(GeneratedDocument).where(
                GeneratedDocument.org_id == ctx.org_id,
                GeneratedDocument.bid_id == bid_id,
                GeneratedDocument.stream == DocumentStream.SUPPORTING,
                GeneratedDocument.name == name,
            )
        ).scalar_one_or_none()
        if document is None:
            document = GeneratedDocument(
                org_id=ctx.org_id,
                bid_id=bid_id,
                structure_id=structure.id,
                name=name,
                required_filename=f"{_slug(title)}.docx",
                format=GeneratedFormat.DOCX,
                stream=DocumentStream.SUPPORTING,
                created_by=ctx.actor_user_id,
            )
            session.add(document)
            session.flush()
        source = hashlib.sha256(
            "\n".join(
                [
                    record["body"],
                    record["source"],
                    output_mode.value,
                    f"layout-r3-{record.get('landscape', False)}",
                ]
            ).encode()
        ).hexdigest()
        current = (
            session.get(GeneratedDocumentVersion, document.current_version_id)
            if document.current_version_id
            else None
        )
        if (
            current is not None
            and current.output_mode is output_mode
            and (current.generation_trace or {}).get("source_sha") == source
        ):
            produced.append(current)
            continue
        next_version = (
            session.execute(
                select(func.max(GeneratedDocumentVersion.version_no)).where(
                    GeneratedDocumentVersion.org_id == ctx.org_id,
                    GeneratedDocumentVersion.generated_document_id == document.id,
                )
            ).scalar_one()
            or 0
        ) + 1
        body = record["body"]
        if output_mode is OutputMode.REVIEW and record.get("review_line"):
            body = f"Review copy. {record['review_line']}\n\n{body}"
        content = _render_docx(
            title,
            [(SimpleNamespace(number_label="", title=record["group"]), body)],
            cover=None,
            storage=storage,
            landscape=bool(record.get("landscape")),
        )
        digest = hashlib.sha256(content).hexdigest()
        key = ObjectStorage.tenant_key(
            ctx.org_id,
            "generated",
            str(bid_id),
            str(document.id),
            f"v{next_version}-{output_mode.value.lower()}.docx",
        )
        storage.put(
            key,
            content,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        version = GeneratedDocumentVersion(
            org_id=ctx.org_id,
            generated_document_id=document.id,
            version_no=next_version,
            output_mode=output_mode,
            storage_key=key,
            sha256=digest,
            byte_size=len(content),
            generated_at=datetime.now(timezone.utc),
            generated_by=ctx.actor_user_id,
            generation_trace={
                "ruleset": RULESET_VERSION,
                "stream": DocumentStream.SUPPORTING.value,
                "source_sha": source,
                "title": title,
                **record["trace"],
            },
        )
        session.add(version)
        session.flush()
        document.current_version_id = version.id
        audit.record(
            session,
            ctx,
            action="attachment.assembled",
            entity_type="generated_document_version",
            entity_id=version.id,
            bid_id=bid_id,
            after={"sha256": digest, "name": name, "version": next_version},
        )
        produced.append(version)
    return produced


def _manifest_approval(document: GeneratedDocument, version: GeneratedDocumentVersion):
    """An attachment is approved only if a person approved the document it came from."""
    if document.stream is not DocumentStream.SUPPORTING:
        return ApprovalStatus.APPROVED
    trace = version.generation_trace or {}
    if trace.get("generated"):
        return ApprovalStatus.APPROVED
    if trace.get("fixture"):
        return ApprovalStatus.REJECTED
    if trace.get("evidence_status") == "APPROVED" and trace.get("use_scope") == "EXTERNAL_BID":
        return ApprovalStatus.APPROVED
    return ApprovalStatus.PENDING_APPROVAL


def create_package(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    output_mode: OutputMode,
) -> ReleasePackage:
    versions = list(
        session.execute(
            select(GeneratedDocumentVersion, GeneratedDocument)
            .join(
                GeneratedDocument,
                GeneratedDocument.id == GeneratedDocumentVersion.generated_document_id,
            )
            .where(
                GeneratedDocumentVersion.org_id == ctx.org_id,
                GeneratedDocument.bid_id == bid_id,
                GeneratedDocumentVersion.output_mode == output_mode,
                GeneratedDocument.current_version_id == GeneratedDocumentVersion.id,
            )
        ).all()
    )
    if not versions:
        raise StateConflict("Assemble generated documents before creating a package.")
    last = (
        session.execute(
            select(func.max(ReleasePackage.version_no)).where(
                ReleasePackage.org_id == ctx.org_id,
                ReleasePackage.bid_id == bid_id,
            )
        ).scalar_one()
        or 0
    )
    prior = (
        session.execute(
            select(ReleasePackage)
            .where(
                ReleasePackage.org_id == ctx.org_id,
                ReleasePackage.bid_id == bid_id,
            )
            .order_by(ReleasePackage.version_no.desc())
        )
        .scalars()
        .first()
    )
    package = ReleasePackage(
        org_id=ctx.org_id,
        bid_id=bid_id,
        version_no=last + 1,
        state=PackageState.REVIEW_MODE_READY,
        output_mode=output_mode,
        supersedes_package_id=prior.id if prior else None,
        created_by=ctx.actor_user_id,
    )
    session.add(package)
    session.flush()
    manifest = Manifest(
        org_id=ctx.org_id,
        package_id=package.id,
        generated_at=datetime.now(timezone.utc),
        generated_by=ctx.actor_user_id,
    )
    session.add(manifest)
    session.flush()
    for index, (version, document) in enumerate(versions, start=1):
        destination = document.stream.value.lower()
        session.add(
            PackageItem(
                org_id=ctx.org_id,
                package_id=package.id,
                generated_document_version_id=version.id,
                destination=destination,
                order_index=index,
            )
        )
        session.add(
            ManifestEntry(
                org_id=ctx.org_id,
                manifest_id=manifest.id,
                filename=document.required_filename,
                version=str(version.version_no),
                byte_size=version.byte_size,
                sha256=version.sha256,
                confidentiality=ConfidentialityClass.CONFIDENTIAL,
                approval_status=_manifest_approval(document, version),
                destination=destination,
                order_index=index,
            )
        )
    session.flush()
    audit.record(
        session,
        ctx,
        action="package.created",
        entity_type="package",
        entity_id=package.id,
        bid_id=bid_id,
        after={"version": package.version_no, "items": len(versions)},
    )
    return package


def _package(session: Session, ctx: ExecutionContext, package_id: uuid.UUID):
    row = session.get(ReleasePackage, package_id)
    if row is None or row.org_id != ctx.org_id:
        raise NotFound("That package does not exist.")
    return row


def packages(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID):
    return list(
        session.execute(
            select(ReleasePackage)
            .where(
                ReleasePackage.org_id == ctx.org_id,
                ReleasePackage.bid_id == bid_id,
            )
            .order_by(ReleasePackage.version_no.desc())
        ).scalars()
    )


def _tender_place(locator: dict | None) -> tuple | None:
    """Where in the tender text something was read from: document and character range."""
    if not locator:
        return None
    place = (
        locator.get("document_version_id"),
        locator.get("char_start"),
        locator.get("char_end"),
    )
    return None if any(part is None for part in place) else place


def _requirement_place(session: Session, requirement_id: uuid.UUID) -> tuple | None:
    version = (
        session.execute(
            select(RequirementVersion)
            .where(
                RequirementVersion.requirement_id == requirement_id,
                RequirementVersion.superseded_at.is_(None),
            )
            .order_by(RequirementVersion.version_no.desc())
        )
        .scalars()
        .first()
    )
    return _tender_place(version.source_locator) if version else None


def _signature_message(session: Session, requirement_id: uuid.UUID) -> tuple[str, str]:
    """Name the requirement, so the finding says what to chase and not only that."""
    requirement = session.get(Requirement, requirement_id)
    if requirement is None:
        return (
            "SIGNATURE_REQUIRED",
            "A requirement needs a signed item; confirm the signed copy is included.",
        )
    version = (
        session.execute(
            select(RequirementVersion)
            .where(
                RequirementVersion.requirement_id == requirement_id,
                RequirementVersion.superseded_at.is_(None),
            )
            .order_by(RequirementVersion.version_no.desc())
        )
        .scalars()
        .first()
    )
    text = " ".join((version.requirement_text if version else "").split())
    if len(text) > 160:
        text = text[:160].rsplit(" ", 1)[0] + "..."
    if not report_compliance.SIGNING.search(text):
        return (
            "ATTACHMENT_REQUIRED",
            f"{requirement.code}: an attachment or evidence is needed ({text}). "
            "Confirm it is attached and who supplies it.",
        )
    return (
        "SIGNATURE_REQUIRED",
        f"{requirement.code}: a signed item is needed ({text}). "
        "Confirm the signed copy is attached and who signs it.",
    )


def _finding(
    session: Session,
    ctx: ExecutionContext,
    run: ValidationRun,
    *,
    code: str,
    message: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> ValidationFinding:
    row = ValidationFinding(
        org_id=ctx.org_id,
        validation_run_id=run.id,
        check_code=code,
        severity=FindingSeverity.BLOCKING,
        waivability=(
            FindingWaivability.NON_WAIVABLE if code in NON_WAIVABLE else FindingWaivability.WAIVABLE
        ),
        entity_type=entity_type,
        entity_id=entity_id,
        message=message,
        detail=detail or {},
        detected_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def validate_package(
    session: Session,
    ctx: ExecutionContext,
    *,
    package_id: uuid.UUID,
    storage: ObjectStorage | None = None,
    gateway=None,
) -> ValidationRun:
    package = _package(session, ctx, package_id)
    if package.immutable:
        raise StateConflict("A released package is immutable; validate a new version.")
    package.state = PackageState.VALIDATION_RUNNING
    run = ValidationRun(
        org_id=ctx.org_id,
        package_id=package.id,
        started_at=datetime.now(timezone.utc),
        status=ValidationRunStatus.RUNNING,
        ruleset_version=RULESET_VERSION,
        triggered_by=ctx.actor_user_id,
    )
    session.add(run)
    session.flush()

    items = list(
        session.execute(
            select(PackageItem, GeneratedDocumentVersion)
            .join(
                GeneratedDocumentVersion,
                GeneratedDocumentVersion.id == PackageItem.generated_document_version_id,
            )
            .where(PackageItem.org_id == ctx.org_id, PackageItem.package_id == package.id)
        ).all()
    )
    if not items:
        _finding(session, ctx, run, code="MISSING_REQUIRED_FILE", message="The package is empty.")
    object_storage = storage or get_storage()
    for _item, version in items:
        try:
            content = object_storage.get(version.storage_key)
        except Exception:
            _finding(
                session,
                ctx,
                run,
                code="CORRUPTED_ARTIFACT",
                message="A generated file is missing or unreadable.",
                entity_type="generated_document_version",
                entity_id=version.id,
            )
            continue
        if hashlib.sha256(content).hexdigest() != version.sha256:
            _finding(
                session,
                ctx,
                run,
                code="CORRUPTED_ARTIFACT",
                message="A generated file checksum does not match its immutable record.",
                entity_type="generated_document_version",
                entity_id=version.id,
            )

    # Read the finished files and the source tender, not only the records behind them.
    from app.services import package_lint

    submission_files = [v for _i, v in items]
    package_text = ""
    if submission_files:
        package_lint.lint(
            session,
            ctx,
            bid_id=package.bid_id,
            versions=submission_files,
            storage=object_storage,
            finding=lambda s_, c_, **kw: _finding(s_, c_, run, **kw),
        )
        from app.models import GeneratedDocument as _GD
        from app.services import package_consistency

        _named: list[tuple[str, str]] = []
        for _v in submission_files:
            if not _readable(object_storage, _v):
                continue
            _d = session.get(_GD, _v.generated_document_id)
            _named.append(
                (
                    getattr(_d, "name", "") or "Document",
                    package_lint.docx_text(object_storage.get(_v.storage_key)),
                )
            )
        package_text = "\n".join(_t for _n, _t in _named)
        # Contradictions hide in the documents that are not in this package as much as in
        # those that are (the assumptions register is not always packaged), so the check
        # reads the newest version of every document the bid has.
        _have = {n for n, _t in _named}
        _foreign = package_lint.other_bid_names(session, ctx, package.bid_id)
        for _doc in session.execute(
            select(_GD).where(_GD.org_id == ctx.org_id, _GD.bid_id == package.bid_id)
        ).scalars():
            _name = getattr(_doc, "name", "") or "Document"
            if _name in _have:
                continue
            # The bid's own documents (assumptions register, matrix) may exist only as review
            # builds, so every document is read at its newest version; anything that names
            # another bid is left out (a retired company document, not part of this package).
            _latest = (
                session.execute(
                    select(GeneratedDocumentVersion)
                    .where(GeneratedDocumentVersion.generated_document_id == _doc.id)
                    .order_by(GeneratedDocumentVersion.version_no.desc())
                )
                .scalars()
                .first()
            )
            if _latest is not None and _readable(object_storage, _latest):
                _body = package_lint.docx_text(object_storage.get(_latest.storage_key))
                if _foreign & set(re.findall(r"[a-z0-9]+", _body.casefold())):
                    continue
                _named.append((_name, _body))
        _prior = []
        try:
            for _pf in session.execute(
                select(ValidationFinding.detail)
                .where(
                    ValidationFinding.org_id == ctx.org_id,
                    ValidationFinding.check_code.in_(
                        ["CROSS_DOCUMENT_CONTRADICTION", "TERMS_DEVIATION", "TERMS_AMBIGUITY"]
                    ),
                    ValidationFinding.entity_type == "bid",
                    ValidationFinding.entity_id == package.bid_id,
                )
                .order_by(ValidationFinding.detected_at.desc())
                .limit(200)
            ).scalars():
                if isinstance(_pf, dict) and _pf.get("first_quote") and _pf.get("second_quote"):
                    _prior.append(_pf)
        except Exception:  # noqa: BLE001 - earlier findings are a bonus
            _prior = []
        _audit: list[dict] = []
        _consistency = package_consistency.review(
            session, ctx, package.bid_id, _named, prior=_prior, audit=_audit
        )
        try:
            package_consistency.save_audit(
                object_storage,
                ctx,
                package.bid_id,
                package.id,
                _audit,
                coverage=[{"document": n, "characters": len(t)} for n, t in _named],
            )
        except Exception:  # noqa: BLE001, S110 - the audit file is a record, not a gate
            pass
        if _consistency is None:
            _finding(
                session,
                ctx,
                run,
                code="CONSISTENCY_CHECK_NOT_RUN",
                message="The cross-document and terms check could not run; read the documents "
                "against each other and against the tender's terms by hand.",
                entity_type="bid",
                entity_id=package.bid_id,
            )
        for _f in _consistency or []:
            _finding(
                session,
                ctx,
                run,
                code=(
                    ("TERMS_AMBIGUITY" if _f.get("ambiguous") else "TERMS_DEVIATION")
                    if _f["kind"] == "TERMS_DEVIATION"
                    else "CROSS_DOCUMENT_CONTRADICTION"
                ),
                message=(
                    f'{_f["issue"]} -- {_f["first_source"]}: "{_f["first_quote"]}" '
                    f'versus {_f["second_source"]}: "{_f["second_quote"]}"'
                ),
                entity_type="bid",
                entity_id=package.bid_id,
                detail={
                    k: _f[k]
                    for k in (
                        "finding_id",
                        "kind",
                        "issue",
                        "first_source",
                        "first_quote",
                        "second_source",
                        "second_quote",
                        "unadjudicated",
                        "ambiguous",
                        "adjudication",
                        "adjudication_reason",
                        "origin",
                        "carried_over",
                    )
                    if k in _f
                },
            )
        residue = package_lint.template_residue(
            session,
            ctx,
            package.bid_id,
            [
                package_lint.docx_text(object_storage.get(v.storage_key))
                for v in submission_files
                if _readable(object_storage, v)
            ],
        )
        if residue:
            _finding(
                session,
                ctx,
                run,
                code="TEMPLATE_RESIDUE",
                message=(
                    "Possible source-template contamination: "
                    + ", ".join(residue[:8])
                    + " appear in the proposal but are used only once or twice in the tender "
                    "and are not part of the customer's name. Confirm with the customer before "
                    "reuse."
                ),
                entity_type="bid",
                entity_id=package.bid_id,
            )
        unfinished = package_lint.tender_unfinished(session, ctx, package.bid_id)
        if unfinished >= package_lint.TENDER_UNFINISHED_AT:
            _finding(
                session,
                ctx,
                run,
                code="TENDER_INCOMPLETE",
                message=(
                    f"The source tender has {unfinished} unfinished fields (blank lines, dotted "
                    "placeholders or template titles). Confirm with the customer that this is "
                    "the issued version and ask for the missing information before submitting."
                ),
                entity_type="bid",
                entity_id=package.bid_id,
            )
        unverified = package_lint.matrix_unverified(session, ctx, package.bid_id)
        if unverified:
            _finding(
                session,
                ctx,
                run,
                code="MATRIX_UNVERIFIED",
                message=(
                    f"{unverified} requirements are not yet fully met: evidence, a signature, a "
                    "confirmation or the customer's input is still outstanding. They are listed "
                    "as missing or pending in the matrix; settle them before submission."
                ),
                entity_type="bid",
                entity_id=package.bid_id,
            )

    # The answers that go in the proposal: not a superseded one, not the
    # internal note of a conflict-resolution task, and not one with no text.
    from app.models import Task
    from app.services.consistency import RESOLUTION_MARK

    has_text = (
        select(ArtifactVersion.artifact_id)
        .join(ArtifactStatement, ArtifactStatement.artifact_version_id == ArtifactVersion.id)
        .where(ArtifactVersion.org_id == ctx.org_id)
    )
    internal = select(Task.id).where(
        Task.org_id == ctx.org_id, Task.grouping_rationale.like(RESOLUTION_MARK + "%")
    )
    artifacts = list(
        session.execute(
            select(ResponseArtifact).where(
                ResponseArtifact.org_id == ctx.org_id,
                ResponseArtifact.bid_id == package.bid_id,
                ResponseArtifact.state != ArtifactState.SUPERSEDED,
                ResponseArtifact.task_id.not_in(internal),
                ResponseArtifact.id.in_(has_text),
            )
        ).scalars()
    )
    for artifact in artifacts:
        if artifact.state is not ArtifactState.APPROVED or not artifact.approved_version_id:
            _finding(
                session,
                ctx,
                run,
                code="MANDATORY_UNANSWERED",
                message=f"Artifact '{artifact.title}' is not approved.",
                entity_type="response_artifact",
                entity_id=artifact.id,
            )
            continue
        statements = list(
            session.execute(
                select(ArtifactStatement).where(
                    ArtifactStatement.org_id == ctx.org_id,
                    ArtifactStatement.artifact_version_id == artifact.approved_version_id,
                )
            ).scalars()
        )
        for statement in statements:
            if PLACEHOLDER.search(statement.text):
                _finding(
                    session,
                    ctx,
                    run,
                    code="PLACEHOLDER_PRESENT",
                    message="A released statement still contains placeholder text.",
                    entity_type="artifact_statement",
                    entity_id=statement.id,
                )
            if statement.statement_class is StatementClass.PROPOSED_COMMITMENT:
                commitment = session.get(Commitment, statement.commitment_id)
                if commitment is None or commitment.approval_status is not ApprovalStatus.APPROVED:
                    _finding(
                        session,
                        ctx,
                        run,
                        code="UNAPPROVED_COMMITMENT",
                        message="A proposed commitment has not been approved.",
                        entity_type="artifact_statement",
                        entity_id=statement.id,
                    )
            if statement.statement_class is StatementClass.EVIDENCE_BACKED_FACT:
                evidence_ids = list(dict.fromkeys(statement.evidence_ids or []))
                evidence = list(
                    session.execute(
                        select(Evidence).where(
                            Evidence.org_id == ctx.org_id,
                            Evidence.id.in_(evidence_ids or [None]),
                        )
                    ).scalars()
                )
                evidence_is_current = len(evidence) == len(evidence_ids) and bool(evidence_ids)
                evidence_is_current = evidence_is_current and all(
                    row.approval_status is ApprovalStatus.APPROVED
                    and row.approved_for_reuse
                    and row.status is KnowledgeStatus.CURRENT_VERIFIED
                    and (row.expiry_date is None or row.expiry_date >= date.today())
                    for row in evidence
                )
                if not evidence_is_current:
                    _finding(
                        session,
                        ctx,
                        run,
                        code="UNSUPPORTED_CLAIM",
                        message=(
                            "An evidence-backed claim lacks current, approved, reusable, "
                            "unexpired evidence."
                        ),
                        entity_type="artifact_statement",
                        entity_id=statement.id,
                    )

                # Material the company classified as not-for-outside-eyes, on
                # its way outside.
                #
                # `RESTRICTED_DISCLOSURE` has been in `NON_WAIVABLE` since the
                # ruleset was written and nothing has ever raised it. A rule
                # nobody can trigger is not a strict rule, it is a comment: the
                # code declared "this can never be waived" about a finding that
                # could never occur, and every release passed that check by
                # having nothing to check.
                #
                # The case it is for is in the fixture tender in as many words
                # -- "Pricing must not appear anywhere in the technical
                # proposal" -- and the company's rate card is classified
                # RESTRICTED. Nothing stopped a drafter citing it, an approver
                # waving it through, and the day rates leaving with the
                # technical submission.
                #
                # Checked on `Evidence.confidentiality_class` rather than on the
                # rendered words. Scanning prose for a number that looks like a
                # rate would be a guess in both directions; the class is a
                # decision a person recorded at ingestion, and the citation says
                # exactly which classified row is being leaned on.
                leaked = sorted(
                    {
                        row.confidentiality_class.value
                        for row in evidence
                        if row.confidentiality_class in UNRELEASABLE
                    }
                )
                if leaked:
                    _finding(
                        session,
                        ctx,
                        run,
                        code="RESTRICTED_DISCLOSURE",
                        message=(
                            "A statement in the outgoing proposal cites "
                            + ", ".join(leaked)
                            + " material. Classified evidence may inform an answer, but it "
                            "cannot be the thing the answer quotes."
                        ),
                        entity_type="artifact_statement",
                        entity_id=statement.id,
                    )

    # Approved answers checked against each other. A contradiction becomes a
    # resolution task, which the checks below then refuse to release past. If
    # the check cannot run, release says so rather than passing silently.
    from app.services import consistency

    try:
        with session.begin_nested():
            consistency.check_bid(
                session,
                ctx,
                bid_id=package.bid_id,
                gateway=gateway,
                package_text=package_text or None,
            )
    except Exception as exc:  # noqa: BLE001 - reported as a finding, never swallowed
        _finding(
            session,
            ctx,
            run,
            code="CONSISTENCY_NOT_CHECKED",
            message=(
                "Approved answers could not be checked against each other "
                f"({type(exc).__name__}). Review the assembled proposal for "
                "contradictions before waiving."
            ),
        )
    _check_the_bid_is_finished(
        session, ctx, run, bid_id=package.bid_id, storage=object_storage, package=package
    )
    run.status = ValidationRunStatus.COMPLETE
    run.finished_at = datetime.now(timezone.utc)
    findings = validation_findings(session, ctx, package_id=package.id)
    open_hard = any(
        row.waivability is FindingWaivability.NON_WAIVABLE and row.resolved_at is None
        for row in findings
    )
    open_waivable = any(row.resolved_at is None for row in findings)
    package.state = (
        PackageState.BLOCKED_HARD
        if open_hard
        else PackageState.BLOCKED_PENDING_EXCEPTION
        if open_waivable
        else PackageState.RELEASE_PENDING_APPROVALS
    )
    session.flush()
    audit.record(
        session,
        ctx,
        action="package.validated",
        entity_type="validation_run",
        entity_id=run.id,
        bid_id=package.bid_id,
        after={"findings": len(findings), "package_state": package.state.value},
    )
    return run


def _check_the_bid_is_finished(
    session: Session,
    ctx: ExecutionContext,
    run: ValidationRun,
    *,
    bid_id: uuid.UUID,
    storage: ObjectStorage | None = None,
    package=None,
) -> None:
    """The release checks that look past the documents to the bid behind them.

    Everything above checks what IS in the package. These check what is still
    open: work nobody finished, questions the customer has not answered,
    mandatory requirements no approved answer covers, sections with nothing in
    them, deliverables not ready, things that need a signature, and prices in
    the technical submission.
    """
    from app.models import (
        ArtifactRequirement,
        ClarificationIssue,
        Deliverable,
        Requirement,
        RequirementAssessment,
        Task,
    )
    from app.models.enums import (
        BidReadiness,
        ClarificationSeverity,
        ClarificationStatus,
        DeliverableStatus,
        ObligationOwner,
        RequirementApplicability,
        TaskState,
    )
    from app.services.requirement_scope import LIVE_REQUIREMENT

    structure = session.execute(
        select(ProposalStructure).where(
            ProposalStructure.org_id == ctx.org_id, ProposalStructure.bid_id == bid_id
        )
    ).scalar_one_or_none()
    approved_artifacts = {
        a.id: a
        for a in session.execute(
            select(ResponseArtifact).where(
                ResponseArtifact.org_id == ctx.org_id,
                ResponseArtifact.bid_id == bid_id,
                ResponseArtifact.state == ArtifactState.APPROVED,
                ResponseArtifact.approved_version_id.is_not(None),
            )
        ).scalars()
    }

    if structure is not None:
        for section in sections(session, ctx, structure_id=structure.id):
            mapped = set(
                session.execute(
                    select(SectionArtifactMap.artifact_id).where(
                        SectionArtifactMap.org_id == ctx.org_id,
                        SectionArtifactMap.section_id == section.id,
                    )
                ).scalars()
            )
            if (
                "executive summary" in section.title.casefold()
                and storage is not None
                and report_edit.summary_text(ctx, bid_id=bid_id, storage=storage)
            ):
                # Written from the approved answers by the joining step and
                # printed in the review copy for the bid manager to read.
                continue
            if section.is_mandatory and not (mapped & set(approved_artifacts)):
                _finding(
                    session,
                    ctx,
                    run,
                    code="SECTION_WITHOUT_CONTENT",
                    message=f"Section '{section.title}' has no approved answer.",
                    entity_type="proposal_section",
                    entity_id=section.id,
                )

        # A price drafted from a demonstration rate card is not a price the
        # company has agreed to charge.
        if _price_unapproved(session, ctx, bid_id, storage or get_storage()):
            _finding(
                session,
                ctx,
                run,
                code="PRICE_NOT_APPROVED",
                message=(
                    "The fixed price in the financial proposal has not been approved: its "
                    "days of effort were estimated by the system (and, where the rate card "
                    "is a demonstration one, so are the rates). The delivery lead must "
                    "confirm the work breakdown and Commercial must approve the figure and "
                    "mark the financial proposal ready."
                ),
            )

        items = list(
            session.execute(
                select(PackageItem, GeneratedDocumentVersion)
                .join(
                    GeneratedDocumentVersion,
                    GeneratedDocumentVersion.id == PackageItem.generated_document_version_id,
                )
                .where(
                    PackageItem.org_id == ctx.org_id,
                    PackageItem.package_id == run.package_id,
                )
            ).all()
        )
        fixtures = sorted(
            {a.title for a in report_attachments.selected(session, ctx, bid_id) if a.fixture}
            | {
                (version.generation_trace or {}).get("title") or ""
                for _item, version in items
                if (version.generation_trace or {}).get("fixture")
            }
            - {""}
        )
        if fixtures:
            _finding(
                session,
                ctx,
                run,
                code="FIXTURE_EVIDENCE",
                message=(
                    "These attachments are test fixtures or are recorded as synthetic (invented "
                    "people, "
                    "customers and results): " + "; ".join(fixtures) + ". Replace them "
                    "with approved company documents before any submission."
                ),
            )
        facts = report_attachments.fixture_only_facts(session, ctx, bid_id)
        if facts:
            held = _tainted_answers(session, ctx, bid_id, facts)
            if held:
                _finding(
                    session,
                    ctx,
                    run,
                    code="FIXTURE_DERIVED_CLAIMS",
                    message=(
                        f"{len(held)} approved answers state names or figures found only in "
                        "fixture documents (for example "
                        + "; ".join(sorted({f for v in held.values() for f in v})[:4])
                        + "). Replace the fixtures, then regenerate or reapprove these answers."
                    ),
                    detail={
                        "answers": {t: sorted(set(v)) for t, v in sorted(held.items())},
                    },
                )

        main = {
            (version.generation_trace or {}).get("stream"): version
            for _item, version in items
            if (version.generation_trace or {}).get("stream")
            in (DocumentStream.TECHNICAL.value, DocumentStream.COMMERCIAL.value)
        }
        signers = report_extras.signatory_names(session, ctx)
        if signers and len(main) > 1:
            named: dict[str, set[str]] = {}
            for stream, version in main.items():
                if not stream:
                    continue
                try:
                    document = WordDocument(
                        io.BytesIO((storage or get_storage()).get(version.storage_key))
                    )
                except Exception:  # noqa: S112 - already a CORRUPTED_ARTIFACT finding
                    continue
                text = (
                    "\n".join(p.text for p in document.paragraphs)
                    + "\n"
                    + "\n".join(c.text for t in document.tables for r in t.rows for c in r.cells)
                )
                named[stream] = report_extras.signers_named(text, signers)
            if report_extras.signer_conflict(named):
                _finding(
                    session,
                    ctx,
                    run,
                    code="SIGNATORY_INCONSISTENT",
                    message=(
                        "The proposal documents name different authorised signatories: "
                        + "; ".join(
                            f"{k.lower()} names {', '.join(sorted(v))}"
                            for k, v in named.items()
                            if v
                        )
                        + ". One signatory must be chosen and used in every document."
                    ),
                )

        for _item, version in items:
            trace = version.generation_trace or {}
            if (
                trace.get("stream") != DocumentStream.SUPPORTING.value
                or trace.get("fixture")
                or trace.get("generated")
            ):
                continue
            if not (
                trace.get("evidence_status") == "APPROVED"
                and trace.get("use_scope") == "EXTERNAL_BID"
            ):
                _finding(
                    session,
                    ctx,
                    run,
                    code="EVIDENCE_NOT_APPROVED",
                    message=(
                        f"Attachment '{trace.get('title') or 'unnamed'}' has not been approved "
                        "as evidence and cleared for a customer by a person."
                    ),
                    entity_type="generated_document_version",
                    entity_id=version.id,
                )

        details = report_extras.tender_details(session, ctx, bid_id)
        if not details.get("customer_verified", True):
            _finding(
                session,
                ctx,
                run,
                code="CUSTOMER_IDENTITY_UNVERIFIED",
                message=(
                    f"The customer recorded for this bid ('{details['customer']}') does not "
                    "appear in the tender documents. Correct it on the opportunity before "
                    "anything is submitted."
                ),
            )
        if not details.get("title_verified", True):
            _finding(
                session,
                ctx,
                run,
                code="FORMAL_TENDER_TITLE_UNVERIFIED",
                message=(
                    f"The tender name recorded for this bid ('{details['tender']}') does not "
                    "appear in the tender documents, which may carry a blank or template title. "
                    "Ask the customer for the formal title; the financial proposal shows it as "
                    "unconfirmed until then."
                ),
            )
        if details.get("reference_missing"):
            _finding(
                session,
                ctx,
                run,
                code="TENDER_REFERENCE_MISSING",
                message="No tender reference number is recorded for this bid. Ask the customer "
                "for it.",
            )

        from app.services import report_compliance as _rc

        try:
            _gaps = _rc.matrix_exclusions(session, ctx, bid_id)
        except Exception as exc:  # noqa: BLE001
            _gaps = []
            _finding(
                session,
                ctx,
                run,
                code="REQUIREMENT_COVERAGE_GAP",
                message=f"The matrix coverage check could not run ({type(exc).__name__}); treat "
                f"every requirement as unchecked.",
                entity_type="bid",
                entity_id=bid_id,
            )
        # Unexplained omissions can hide a bidder obligation, so they block until someone classifies
        # them; a plain customer obligation is listed with its wording for a reviewer to confirm.
        _open = [
            g
            for g in _gaps
            if g[1]
            in ("not shown", "owner needs review", "no requirement was extracted under this number")
        ]
        _plain = [g for g in _gaps if g not in _open]
        if _open:
            _finding(
                session,
                ctx,
                run,
                code="REQUIREMENT_COVERAGE_GAP",
                message=(
                    f"{len(_open)} extracted requirement(s) or numbers are not in the compliance "
                    f"matrix and "
                    "are not yet classified: "
                    + "; ".join(
                        f"{c} ({why}){': ' + t[:140] if t else ''}" for c, why, t in _open[:25]
                    )
                    + ". Classify each as the customer's obligation, a tender condition, a merged "
                    "duplicate or "
                    "a bidder obligation (restore it to the matrix)."
                ),
                entity_type="bid",
                entity_id=bid_id,
            )
        if _plain:
            _finding(
                session,
                ctx,
                run,
                code="REQUIREMENT_EXCLUSION_REVIEW",
                message=(
                    f"{len(_plain)} requirement(s) are left out of the matrix as the customer's "
                    f"own obligation "
                    "or not applicable; confirm each: "
                    + "; ".join(f"{c}: {t[:120]}" for c, why, t in _plain[:25])
                ),
                entity_type="bid",
                entity_id=bid_id,
            )

        # Evidence sufficiency: how many the tender asks for against how many we can show.
        try:
            _need = []
            for _rv in session.execute(
                select(
                    Requirement.code,
                    RequirementVersion.required_count,
                    RequirementVersion.requirement_text,
                )
                .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
                .where(Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id)
            ).all():
                if re.search(
                    r"reference|similar|(?:related|past|previous|recent)\s+projects?|experience"
                    r"|assignment",
                    _rv[2] or "",
                    re.I,
                ):
                    # The extracted number, else the minimum the tender states in words.
                    _count = (
                        _rv[1]
                        if _rv[1] is not None
                        else report_attachments.required_from_text(_rv[2])
                    )
                    if _count is not None:
                        _need.append((_rv[0], int(_count), _rv[2]))
            if _need:
                _supplied = session.execute(
                    select(func.count())
                    .select_from(GeneratedDocument)
                    .where(
                        GeneratedDocument.org_id == ctx.org_id,
                        GeneratedDocument.bid_id == bid_id,
                        GeneratedDocument.name.ilike("%project reference%"),
                    )
                ).scalar_one()
                _rows = report_attachments.reference_rows(session, ctx, bid_id)
                _short = []
                for _c, _n, _t in _need:
                    if _rows:
                        _have, _what = report_attachments.reference_supply(_t, _rows)
                    else:
                        _have, _what = _supplied, "reference document(s) in the package"
                    if _n > _have:
                        _short.append((_c, _n, _have, _what))
                if _short:
                    _finding(
                        session,
                        ctx,
                        run,
                        code="EVIDENCE_SHORTFALL",
                        message=(
                            "INSUFFICIENT, submission blocking: "
                            + "; ".join(
                                f"{c}: {have} of {n} {what}" for c, n, have, what in _short[:8]
                            )
                            + ". Approval of a reference is not the same as sufficient evidence: "
                            "add the missing "
                            "references with their letters, contact details and relevant work, or "
                            "state the shortfall."
                        ),
                        detail={
                            "requirements": [
                                {"requirement": c, "required": n, "supplied": have, "counted": what}
                                for c, n, have, what in _short
                            ]
                        },
                        entity_type="bid",
                        entity_id=bid_id,
                    )
        except Exception:  # noqa: BLE001, S110 - advisory
            pass
        try:
            _unattested = [
                a
                for a in report_attachments.selected(session, ctx, bid_id)
                if not a.fixture and a.reality != "ATTESTED"
            ]
            if _unattested:
                _finding(
                    session,
                    ctx,
                    run,
                    code="PROVENANCE_UNVERIFIED",
                    message=(
                        f"{len(_unattested)} attached document(s) are approved but nobody has "
                        f"attested that "
                        "they are genuine company records: "
                        + "; ".join(a.title for a in _unattested[:8])
                        + ". A document of unknown origin is never treated as real. Record who "
                        "vouches "
                        "for it by attesting it as genuine when approving it."
                    ),
                    detail={"documents": [a.title for a in _unattested]},
                    entity_type="bid",
                    entity_id=bid_id,
                )
        except Exception:  # noqa: BLE001, S110 - advisory
            pass
        try:
            _gap = report_attachments.reference_gaps(session, ctx, bid_id)
            if any(_gap.values()):
                _bits = []
                for _key, _label in (
                    ("letters", "missing reference letters"),
                    ("contacts", "contacts not confirmed or released"),
                    ("none_recorded", "experience the company records as none"),
                ):
                    if _gap[_key]:
                        _bits.append(
                            f"{len(_gap[_key])} {_label} ("
                            + "; ".join(f'{t}: "{q}"' for t, q in _gap[_key][:4])
                            + ")"
                        )
                _finding(
                    session,
                    ctx,
                    run,
                    code="EVIDENCE_INCOMPLETE",
                    message=(
                        "INCOMPLETE, submission blocking: the reference documents record "
                        + "; ".join(_bits)
                        + ". A reference counts toward the tender's minimum only with its letter, "
                        "a released contact and relevant work."
                    ),
                    detail={
                        k: [{"document": t, "quote": q} for t, q in v] for k, v in _gap.items()
                    },
                    entity_type="bid",
                    entity_id=bid_id,
                )
        except Exception:  # noqa: BLE001, S110 - advisory
            pass

        # Prices in the technical submission.
        technical = set(
            session.execute(
                select(SectionArtifactMap.artifact_id)
                .join(ProposalSection, ProposalSection.id == SectionArtifactMap.section_id)
                .join(GeneratedDocument, GeneratedDocument.id == ProposalSection.target_document_id)
                .where(
                    SectionArtifactMap.org_id == ctx.org_id,
                    ProposalSection.structure_id == structure.id,
                    GeneratedDocument.stream == DocumentStream.TECHNICAL,
                )
            ).scalars()
        )
        for artifact_id in technical & set(approved_artifacts):
            for statement in session.execute(
                select(ArtifactStatement).where(
                    ArtifactStatement.org_id == ctx.org_id,
                    ArtifactStatement.artifact_version_id
                    == approved_artifacts[artifact_id].approved_version_id,
                )
            ).scalars():
                found = PRICE.search(statement.text or "")
                if found:
                    _finding(
                        session,
                        ctx,
                        run,
                        code="PRICE_IN_TECHNICAL",
                        message=(
                            f"The technical submission contains a price ({found.group(0)!r}). "
                            "Pricing belongs in the commercial submission only."
                        ),
                        entity_type="artifact_statement",
                        entity_id=statement.id,
                    )

    for task in session.execute(
        select(Task).where(
            Task.org_id == ctx.org_id,
            Task.bid_id == bid_id,
            Task.state.not_in([TaskState.APPROVED, TaskState.COMPLETED]),
        )
    ).scalars():
        # A task replaced by a better-specified one is history, not outstanding work.
        if (task.blocker_note or "").strip().lower().startswith("superseded"):
            continue
        _finding(
            session,
            ctx,
            run,
            code="UNRESOLVED_TASK",
            message=f"Task {task.code} '{task.title}' is {task.state.value}, not approved.",
            entity_type="task",
            entity_id=task.id,
        )

    for issue in session.execute(
        select(ClarificationIssue).where(
            ClarificationIssue.org_id == ctx.org_id,
            ClarificationIssue.bid_id == bid_id,
            ClarificationIssue.status.in_(
                [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
            ),
            ClarificationIssue.recommended_question.is_not(None),
            ClarificationIssue.restates_clarification_id.is_(None),
            ClarificationIssue.severity.in_(
                [ClarificationSeverity.MAJOR, ClarificationSeverity.BLOCKING]
            ),
        )
    ).scalars():
        _finding(
            session,
            ctx,
            run,
            code="OPEN_TENDER_GAP",
            message=(
                "Unresolved gap in the tender, no assumption recorded: "
                f"{issue.recommended_question}"
            ),
            entity_type="clarification_issue",
            entity_id=issue.id,
        )

    answered = set(
        session.execute(
            select(ArtifactRequirement.requirement_id).where(
                ArtifactRequirement.org_id == ctx.org_id,
                ArtifactRequirement.artifact_id.in_(list(approved_artifacts) or [None]),
            )
        ).scalars()
    )
    for requirement in session.execute(
        select(Requirement).where(
            Requirement.org_id == ctx.org_id,
            Requirement.bid_id == bid_id,
            LIVE_REQUIREMENT,
            Requirement.is_mandatory.is_(True),
            Requirement.applicability != RequirementApplicability.NOT_APPLICABLE,
        )
    ).scalars():
        owner = getattr(requirement, "obligation_owner", None) or ObligationOwner.BIDDER
        if owner is not ObligationOwner.BIDDER or requirement.id in answered:
            continue
        _finding(
            session,
            ctx,
            run,
            code="REQUIREMENT_UNANSWERED",
            message=f"Mandatory requirement {requirement.code} has no approved answer.",
            entity_type="requirement",
            entity_id=requirement.id,
        )

    # An item the tender asks to have quoted on its own line must not sit inside the
    # fixed price. The price is one figure, so this stays open until a person records
    # the separate price (or an exception for it).
    priced = report_extras.financial_record(ctx, bid_id=bid_id, storage=storage or get_storage())
    quoted = {k.upper() for k in ((priced or {}).get("separate") or {})}
    for requirement in session.execute(
        select(Requirement).where(
            Requirement.org_id == ctx.org_id,
            Requirement.bid_id == bid_id,
            LIVE_REQUIREMENT,
            Requirement.priced_separately.is_(True),
            Requirement.applicability != RequirementApplicability.NOT_APPLICABLE,
        )
    ).scalars():
        owner = getattr(requirement, "obligation_owner", None) or ObligationOwner.BIDDER
        if owner is not ObligationOwner.BIDDER or requirement.code.upper() in quoted:
            continue
        _finding(
            session,
            ctx,
            run,
            code="SEPARATE_PRICE_MISSING",
            message=(
                f"Requirement {requirement.code} is to be quoted separately, but the "
                "financial proposal gives one fixed price with no separate line for it."
            ),
            entity_type="requirement",
            entity_id=requirement.id,
        )

    # One finding per submission. A row the extraction marked as restating
    # another is the same obligation named less completely, and the same name
    # in different capitals or as a plural is the same thing again.
    seen_deliverables: set[str] = set()
    deliverable_findings: dict[tuple, ValidationFinding] = {}
    for deliverable in session.execute(
        select(Deliverable)
        .where(
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
        .order_by(Deliverable.name)
    ).scalars():
        from app.services import package_lint

        key = re.sub(r"[^a-z0-9]+", " ", deliverable.name.casefold()).strip()
        key = re.sub(r"s\b", "", key)
        if key in seen_deliverables:
            continue
        seen_deliverables.add(key)
        if package_lint.deliverable_stage(deliverable.name) == "POST_AWARD":
            # Answered by a commitment at bid stage; listed in the remediation register, not a
            # reason to hold the bid.
            continue
        row = _finding(
            session,
            ctx,
            run,
            code=_deliverable_code(package_lint.deliverable_stage(deliverable.name), package),
            message=f"Required deliverable '{deliverable.name}' is not marked ready.",
            entity_type="deliverable",
            entity_id=deliverable.id,
        )
        place = _tender_place(deliverable.source_locator)
        if place is not None:
            deliverable_findings.setdefault(place, row)

    for assessment in session.execute(
        select(RequirementAssessment).where(
            RequirementAssessment.org_id == ctx.org_id,
            RequirementAssessment.bid_id == bid_id,
            RequirementAssessment.is_current.is_(True),
            RequirementAssessment.bid_readiness == BidReadiness.REQUIRES_SIGNATURE,
        )
    ).scalars():
        # A requirement that asks for the very thing a deliverable finding already
        # names (same words of the tender) is that finding, not a second problem.
        place = _requirement_place(session, assessment.requirement_id)
        folded = deliverable_findings.get(place) if place is not None else None
        if folded is not None:
            needed = session.get(Requirement, assessment.requirement_id)
            if needed is not None:
                detail = dict(folded.detail or {})
                detail["also_required_by"] = [*detail.get("also_required_by", []), needed.code]
                folded.detail = detail
                folded.message += f" The tender also asks for it in {needed.code}."
            continue
        code, message = _signature_message(session, assessment.requirement_id)
        _finding(
            session,
            ctx,
            run,
            code=code,
            message=message,
            entity_type="requirement",
            entity_id=assessment.requirement_id,
        )


def validation_findings(
    session: Session, ctx: ExecutionContext, *, package_id: uuid.UUID
) -> list[ValidationFinding]:
    run = (
        session.execute(
            select(ValidationRun)
            .where(
                ValidationRun.org_id == ctx.org_id,
                ValidationRun.package_id == package_id,
            )
            .order_by(ValidationRun.started_at.desc())
        )
        .scalars()
        .first()
    )
    if run is None:
        return []
    return list(
        session.execute(
            select(ValidationFinding).where(
                ValidationFinding.org_id == ctx.org_id,
                ValidationFinding.validation_run_id == run.id,
            )
        ).scalars()
    )


def request_exception(
    session: Session,
    ctx: ExecutionContext,
    *,
    finding_id: uuid.UUID,
    justification: str,
) -> ReleaseException:
    finding = session.get(ValidationFinding, finding_id)
    if finding is None or finding.org_id != ctx.org_id:
        raise NotFound("That validation finding does not exist.")
    if finding.waivability is FindingWaivability.NON_WAIVABLE:
        raise StateConflict("NON_WAIVABLE findings have no exception path.")
    if not justification.strip():
        raise ValidationError("An exception needs a justification.")
    row = ReleaseException(
        org_id=ctx.org_id,
        finding_id=finding.id,
        requested_by=ctx.actor_user_id,
        justification=justification.strip(),
        requested_at=datetime.now(timezone.utc),
        state=ExceptionState.REQUESTED,
        scope="THIS_PACKAGE_VERSION",
    )
    session.add(row)
    session.flush()
    return row


def decide_exception(
    session: Session,
    ctx: ExecutionContext,
    *,
    exception_id: uuid.UUID,
    decision: ExceptionState,
    comment: str | None = None,
) -> ReleaseException:
    row = session.get(ReleaseException, exception_id)
    if row is None or row.org_id != ctx.org_id:
        raise NotFound("That release exception does not exist.")
    if row.state is not ExceptionState.REQUESTED:
        raise StateConflict("That exception has already been decided.")
    if decision not in {ExceptionState.APPROVED, ExceptionState.REJECTED}:
        raise ValidationError("An exception decision must be APPROVED or REJECTED.")
    row.state = decision
    row.approved_by = ctx.actor_user_id if decision is ExceptionState.APPROVED else None
    row.approved_at = datetime.now(timezone.utc)
    row.decision_comment = comment
    finding = session.get(ValidationFinding, row.finding_id)
    if finding is None:
        raise StateConflict("The exception's finding no longer exists.")
    if decision is ExceptionState.APPROVED:
        finding.resolved_at = datetime.now(timezone.utc)
        finding.resolved_by = ctx.actor_user_id
    run = session.get(ValidationRun, finding.validation_run_id)
    package = session.get(ReleasePackage, run.package_id) if run else None
    if package and decision is ExceptionState.APPROVED:
        remaining = [
            item
            for item in validation_findings(session, ctx, package_id=package.id)
            if item.resolved_at is None
        ]
        package.state = (
            PackageState.BLOCKED_HARD
            if any(item.waivability is FindingWaivability.NON_WAIVABLE for item in remaining)
            else PackageState.BLOCKED_PENDING_EXCEPTION
            if remaining
            else PackageState.RELEASE_PENDING_APPROVALS
        )
    session.flush()
    return row


def submit_for_release(
    session: Session, ctx: ExecutionContext, *, package_id: uuid.UUID, note: str | None = None
) -> ReleasePackage:
    package = _package(session, ctx, package_id)
    if package.state is not PackageState.RELEASE_PENDING_APPROVALS:
        raise IllegalTransition("Only a clean or fully-excepted package may be submitted.")
    package.submitted_by = ctx.actor_user_id
    package.submitted_at = datetime.now(timezone.utc)
    session.flush()
    audit.record(
        session,
        ctx,
        action="release.submitted",
        entity_type="package",
        entity_id=package.id,
        bid_id=package.bid_id,
        reason=note,
    )
    return package


def approve_release(
    session: Session, ctx: ExecutionContext, *, package_id: uuid.UUID, comment: str | None = None
) -> ReleasePackage:
    package = _package(session, ctx, package_id)
    if package.state is not PackageState.RELEASE_PENDING_APPROVALS or not package.submitted_by:
        raise IllegalTransition("The package has not been submitted for release.")
    if package.submitted_by == ctx.actor_user_id:
        raise StateConflict("Maker-checker separation forbids approving your own release.")
    package.state = PackageState.RELEASED
    package.released_by = ctx.actor_user_id
    package.released_at = datetime.now(timezone.utc)
    package.immutable = True
    session.flush()
    audit.record(
        session,
        ctx,
        action="release.approved",
        entity_type="package",
        entity_id=package.id,
        bid_id=package.bid_id,
        after={"immutable": True, "version": package.version_no},
        reason=comment,
    )
    return package


def archive(
    session: Session, ctx: ExecutionContext, *, package_id: uuid.UUID, reason: str
) -> ReleasePackage:
    package = _package(session, ctx, package_id)
    if package.state is not PackageState.RELEASED:
        raise IllegalTransition("Only a released package can be archived.")
    package.state = PackageState.ARCHIVED
    session.flush()
    audit.record(
        session,
        ctx,
        action="package.archived",
        entity_type="package",
        entity_id=package.id,
        bid_id=package.bid_id,
        reason=reason,
    )
    return package


def record_outcome(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    result: OutcomeResult,
    technical_score: float | None = None,
    commercial_score: float | None = None,
    award_value: float | None = None,
    competitor: str | None = None,
    loss_reason: str | None = None,
    evaluator_feedback: str | None = None,
) -> Outcome:
    existing = session.execute(
        select(Outcome).where(Outcome.org_id == ctx.org_id, Outcome.bid_id == bid_id)
    ).scalar_one_or_none()
    if existing:
        raise StateConflict("This bid already has a recorded outcome.")
    row = Outcome(
        org_id=ctx.org_id,
        bid_id=bid_id,
        result=result,
        technical_score=technical_score,
        commercial_score=commercial_score,
        award_value=award_value,
        competitor=competitor,
        loss_reason=loss_reason,
        evaluator_feedback=evaluator_feedback,
        recorded_by=ctx.actor_user_id,
        recorded_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    from app.models import Bid, Opportunity

    bid = session.get(Bid, bid_id)
    opportunity = session.get(Opportunity, bid.opportunity_id) if bid else None
    if result is OutcomeResult.WON and opportunity is not None:
        # A won bid is a project the company will one day have to prove it
        # delivered. Proposed, not written: a knowledge administrator approves
        # it into company knowledge, where the next tender's "three similar
        # projects" can find it.
        facts: list[dict] = [
            {"predicate": "customer", "value_text": opportunity.customer_name},
            {"predicate": "status", "value_text": "Awarded"},
            {"predicate": "awarded_on", "value_text": date.today().isoformat()},
        ]
        if award_value is not None:
            facts.append({"predicate": "award_value", "value_number": award_value})
        session.add(
            KnowledgeUpdateCandidate(
                org_id=ctx.org_id,
                origin_type="OUTCOME_AWARD",
                origin_entity_id=row.id,
                target_type="KNOWLEDGE_ENTITY",
                proposed_payload={
                    "entity_type": "PROJECT",
                    "name": opportunity.tender_name,
                    "facts": facts,
                },
                rationale=(
                    "Won bid: record the project so future tenders can cite it once "
                    "delivery evidence (completion letter, reference) is added."
                ),
                state=CandidateState.PROPOSED,
                created_by=ctx.actor_user_id,
            )
        )
    if evaluator_feedback:
        session.add(
            KnowledgeUpdateCandidate(
                org_id=ctx.org_id,
                origin_type="OUTCOME_FEEDBACK",
                origin_entity_id=row.id,
                target_type="KNOWLEDGE_FACT",
                proposed_payload={
                    "feedback": evaluator_feedback,
                    "customer": opportunity.customer_name if opportunity else None,
                },
                rationale="Evaluator feedback may improve governed company knowledge.",
                state=CandidateState.PROPOSED,
                created_by=ctx.actor_user_id,
            )
        )
    session.flush()
    audit.record(
        session,
        ctx,
        action="outcome.recorded",
        entity_type="outcome",
        entity_id=row.id,
        bid_id=bid_id,
        after={"result": result.value},
    )
    return row


def decide_candidate(
    session: Session,
    ctx: ExecutionContext,
    *,
    candidate_id: uuid.UUID,
    decision: CandidateState,
    comment: str | None = None,
) -> KnowledgeUpdateCandidate:
    row = session.get(KnowledgeUpdateCandidate, candidate_id)
    if row is None or row.org_id != ctx.org_id:
        raise NotFound("That knowledge update candidate does not exist.")
    if row.state is not CandidateState.PROPOSED:
        raise StateConflict("That candidate has already been decided.")
    if decision not in {CandidateState.APPROVED, CandidateState.REJECTED}:
        raise ValidationError("A candidate decision must be APPROVED or REJECTED.")
    if decision is CandidateState.APPROVED and row.target_type == "KNOWLEDGE_ENTITY":
        # The approval IS the verification, so the facts are recorded as
        # vouched for by the person deciding.
        from app.models.enums import EntityType

        payload = row.proposed_payload
        entity, _created = knowledge.resolve_or_create_entity(
            session, ctx, entity_type=EntityType(payload["entity_type"]), name=payload["name"]
        )
        for fact in payload.get("facts", []):
            knowledge.add_fact(
                session,
                ctx,
                entity_id=entity.id,
                predicate=fact["predicate"],
                value_text=fact.get("value_text"),
                value_number=fact.get("value_number"),
                approved=True,
            )
        row.target_id = entity.id
    if decision is CandidateState.APPROVED and row.target_type == "KNOWLEDGE_FACT":
        payload = row.proposed_payload
        if "feedback" in payload and payload.get("customer"):
            # Evaluator feedback belongs to the customer who gave it. It was
            # stored as {"feedback": ...} with nowhere to go, so approving it
            # created nothing at all.
            from app.models.enums import EntityType

            client, _created = knowledge.resolve_or_create_entity(
                session, ctx, entity_type=EntityType.CLIENT, name=payload["customer"]
            )
            fact = knowledge.add_fact(
                session,
                ctx,
                entity_id=client.id,
                predicate="evaluator_feedback",
                value_text=payload["feedback"],
                approved=True,
            )
            row.target_id = fact.id
        elif {"entity_id", "predicate", "value_text"} <= set(payload):
            fact = knowledge.add_fact(
                session,
                ctx,
                entity_id=uuid.UUID(payload["entity_id"]),
                predicate=payload["predicate"],
                value_text=payload["value_text"],
                extracted_by="human",
            )
            row.target_id = fact.id
    row.state = decision
    row.reviewed_by = ctx.actor_user_id
    row.reviewed_at = datetime.now(timezone.utc)
    row.review_comment = comment
    session.flush()
    audit.record(
        session,
        ctx,
        action="knowledge.candidate_decided",
        entity_type="knowledge_update_candidate",
        entity_id=row.id,
        after={"state": decision.value, "target_id": str(row.target_id) if row.target_id else None},
        reason=comment,
    )
    return row
