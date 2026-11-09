"""Every row the export needs, read once and shaped for a spreadsheet.

Separate from the workbook code so each half can be wrong on its own. The
queries are the part that has to match the schema; the formatting is the part
that has to match Excel, and debugging them together means never being sure
which one broke.

Two rules run through all of it. Only current assessments are read -- the
partial unique index guarantees one per requirement, and dropping the filter
silently multiplies the matrix by each requirement's assessment history. And
nothing here computes a verdict: every status, score and rationale is read from
a row some part of the pipeline already wrote, so a disagreement between this
workbook and the application is a bug in the export, never a second opinion.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.models.enums import is_self_evidencing
from app.services.requirement_scope import LIVE_REQUIREMENT

MATRIX_HEADERS = [
    "Requirement",  # A
    "Category",  # B  -- Coverage summary counts on this column
    "Mandatory",  # C
    "Owner of obligation",  # D
    "Applicability",  # E
    "Evidence status",  # F
    "Bid readiness",  # G  -- and on this one
    "Evidence items",  # H
    "Rationale",  # I
    "Assessment state",  # J
    "Task",  # K
    "Task owner",  # L
    "Task state",  # M
    "Open requests",  # N
    "Open clarifications",  # O
    "Requirement text",  # P
]


def _name_map(session, ctx, model, field="name"):
    return {
        row.id: getattr(row, field)
        for row in session.execute(select(model).where(model.org_id == ctx.org_id)).scalars().all()
    }


def continued_clause_ref(session: Session, org, version) -> str:
    """The reference a block inherits when a page break cut its clause in two.

    Three bullets of the Core OMS list arrived as a block with no reference of
    its own, starting one character after the block that has it, and the
    requirements read from it showed a blank "Clause ref". The blank is not wrong
    to STORE -- a missing reference is what tells extraction the block is a
    continuation -- but a reader following a requirement back to the tender needs
    the clause it belongs to, so the export shows it, marked as inherited.
    """
    from app.models import DocumentBlock
    from app.services.clause_identity import qualified_clause_ref

    locator = getattr(version, "source_locator", None) or {}
    start = locator.get("char_start")
    version_id = getattr(version, "source_document_version_id", None)
    if start is None or version_id is None:
        return ""
    block = (
        session.execute(
            select(DocumentBlock).where(
                DocumentBlock.org_id == org,
                DocumentBlock.document_version_id == version_id,
                DocumentBlock.char_start == start,
            )
        )
        .scalars()
        .first()
    )
    if block is None or (block.clause_ref or "").strip():
        return ""
    previous = (
        session.execute(
            select(DocumentBlock).where(
                DocumentBlock.org_id == org,
                DocumentBlock.document_version_id == version_id,
                DocumentBlock.char_end + 1 == block.char_start,
            )
        )
        .scalars()
        .first()
    )
    if previous is None or (previous.heading_path or None) != (block.heading_path or None):
        return ""
    reference = qualified_clause_ref(previous)
    return f"{reference} (continued)" if reference else ""


def is_a_gap(*, mandatory: bool, owner: str, applicability: str, readiness: str | None) -> bool:
    """A gap is ours, required, and not answerable.

    Decided on the ASSESSMENT, not on the owner label alone. A clause labelled
    CUSTOMER that hands something over ("Jadwa will provide access to the Core
    OMS for ...") holds work for the bidder; the assessment keeps it in a task
    and asks a person to confirm it, and the Gaps sheet leaving it out told a
    reader the work list had four fewer items than the Matrix did. What is
    excluded is what the assessment itself calls a customer obligation.
    """
    if not mandatory or applicability == "NOT_APPLICABLE":
        return False
    if readiness in ("READY", "NOT_APPLICABLE", "CUSTOMER_OBLIGATION"):
        return False
    if owner == "CUSTOMER" and readiness is None:
        # Not assessed yet, so nothing says it holds work for us.
        return False
    return True


def collect(session: Session, ctx: ExecutionContext, bid) -> dict:
    from app.models import (
        ArtifactStatement,
        ArtifactVersion,
        AssessmentEvidence,
        ClarificationIssue,
        ClarificationRequirement,
        ClauseResolution,
        CrossDepartmentRequest,
        Deliverable,
        Department,
        Document,
        DocumentClassification,
        DocumentVersion,
        Evidence,
        KnowledgeEntity,
        KnowledgeFact,
        KnowledgeRelation,
        Opportunity,
        PersonLink,
        Requirement,
        RequirementAssessment,
        RequirementVersion,
        ResponseArtifact,
        Task,
        TaskRequirement,
        User,
    )

    org = ctx.org_id
    users = _name_map(session, ctx, User, "display_name")
    departments = _name_map(session, ctx, Department, "name")

    # --- tender ---------------------------------------------------------------
    opportunity = session.get(Opportunity, bid.opportunity_id) if bid.opportunity_id else None
    bid_headers = ["Field", "Value"]
    bid_rows = [
        ["Bid id", bid.id],
        ["State", bid.state],
        ["Language", bid.language],
        ["Customer", getattr(opportunity, "customer_name", "")],
        ["Tender", getattr(opportunity, "tender_name", "")],
        ["Reference", getattr(opportunity, "tender_reference", "")],
        ["Deadline", getattr(opportunity, "deadline", "")],
        ["Estimated value", getattr(opportunity, "estimated_value", "")],
        ["Currency", getattr(opportunity, "currency", "")],
        ["Region", getattr(opportunity, "region", "")],
    ]
    # tender_facts and submission_instructions are JSONB the extractor filled in.
    # One row each rather than one column each: the keys differ per tender, and a
    # column layout that changes shape between bids cannot be compared.
    for label, blob in (
        ("Tender fact", bid.tender_facts or {}),
        ("Submission instruction", bid.submission_instructions or {}),
    ):
        for key, value in (blob or {}).items():
            bid_rows.append([f"{label}: {key}", value])

    requirements = session.execute(
        select(Requirement, RequirementVersion)
        .outerjoin(
            RequirementVersion,
            (RequirementVersion.id == Requirement.current_version_id)
            & (RequirementVersion.org_id == Requirement.org_id),
        )
        .where(Requirement.org_id == org, Requirement.bid_id == bid.id, LIVE_REQUIREMENT)
        .order_by(Requirement.code)
    ).all()
    req_headers = [
        "Requirement",
        "Category",
        "Subcategory",
        "Mandatory",
        "Scored",
        "Weight",
        "Owner of obligation",
        "Conditional",
        "Condition",
        "Quoted separately",
        "Applicability",
        "Why not applicable",
        "Risk",
        "Clause ref",
        "Recommended department",
        "Extracted by",
        "Text",
    ]
    requirement_rows = [
        [
            r.code,
            r.category,
            r.subcategory,
            r.is_mandatory,
            r.is_scored,
            r.evaluation_weight,
            r.obligation_owner,
            r.is_conditional,
            r.condition_expression,
            "yes -- price this on its own line" if r.priced_separately else "",
            r.applicability,
            r.applicability_reason,
            r.risk,
            getattr(v, "clause_ref", None) or continued_clause_ref(session, org, v),
            departments.get(r.recommended_department_id, ""),
            getattr(v, "extracted_by", ""),
            getattr(v, "requirement_text", "(no current version -- extraction gap)"),
        ]
        for r, v in requirements
    ]

    clar_links: dict[uuid.UUID, list[str]] = {}
    for issue_id, code in session.execute(
        select(ClarificationRequirement.clarification_id, Requirement.code)
        .join(Requirement, Requirement.id == ClarificationRequirement.requirement_id)
        .where(ClarificationRequirement.org_id == org)
    ).all():
        clar_links.setdefault(issue_id, []).append(code)

    clarifications = (
        session.execute(
            select(ClarificationIssue)
            .where(ClarificationIssue.org_id == org, ClarificationIssue.bid_id == bid.id)
            .order_by(ClarificationIssue.severity, ClarificationIssue.issue_type)
        )
        .scalars()
        .all()
    )
    # Say which question a restatement was folded into.
    #
    # A restatement is CLOSED, with `Why closed` saying it repeats another
    # question; this column says WHICH. It is printed as the canonical
    # question's text, truncated -- a reader following it should search on a
    # prefix, not expect an exact match, and a review of an earlier export
    # reported a truncated reference as a dangling one.
    by_id = {c.id: c for c in clarifications}
    clar_headers = [
        # Who this finding is FOR. A review counted 64 open questions and found
        # 15 of them were identity-drift notes about our own re-extraction --
        # real QA, nothing a customer could answer. They are not hidden (a
        # detector that can be visible or honest, not both, is the failure this
        # sheet already learned once); they are labelled.
        # A short, stable handle for the row, so a reference to it can be
        # FOUND. `Same question as` printed the canonical's question text,
        # truncated to 200 characters -- and a reviewer searching for that
        # string reported 18 dangling references and 7 ambiguous ones, all of
        # them artefacts of the truncation rather than the data.
        "Question id",
        "Audience",
        "Type",
        "Severity",
        "Status",
        "Blocks generation",
        "Description",
        "Suggested question to customer",
        "Customer response",
        "Why closed",
        "Requirements affected",
        "Detected by",
        "Same question as",
        "Why the same",
    ]
    clarification_rows = [
        [
            str(c.id)[:8],
            "customer" if (c.recommended_question or "").strip() else "internal QA",
            c.issue_type,
            c.severity,
            c.status,
            c.blocks_generation,
            c.description,
            c.recommended_question,
            c.client_response,
            c.resolution_note or "",
            sorted(clar_links.get(c.id, [])),
            c.detected_by,
            # The id first, then as much of the question as fits: the id is
            # what makes the reference exact, the text is what makes the row
            # readable without following it.
            (
                str(c.restates_clarification_id)[:8]
                + " -- "
                + " ".join((by_id[c.restates_clarification_id].recommended_question or "").split())[
                    :160
                ]
                if c.restates_clarification_id in by_id
                else ""
            ),
            c.equivalence_reason or "",
        ]
        for c in clarifications
    ]

    deliverables = (
        session.execute(
            select(Deliverable)
            .where(Deliverable.org_id == org, Deliverable.bid_id == bid.id)
            .order_by(Deliverable.name)
        )
        .scalars()
        .all()
    )
    # Which submission a duplicate was folded into, by NAME.
    #
    # The clarification sheet had the mirror of this problem: it rendered the
    # canonical question's TEXT where a reader needed to find the row, and a
    # review reported working deduplication as broken because it could not
    # follow the reference. A name is what a person assembling a pack can
    # actually look up in this sheet, so the name is what is printed -- the
    # database stores the id.
    by_deliverable_id = {d.id: d for d in deliverables}
    deliv_headers = [
        # The checklist answer, first, because that is what the sheet is FOR.
        #
        # A folded row keeps IDENTIFIED and mandatory -- the tender really does
        # require that submission -- so a reader building a checklist from
        # `Status` alone gets the same document twice. A review flagged exactly
        # that risk. `Duplicate of` already says which row carries it, three
        # columns to the right; this says what to DO about it, where the eye
        # lands.
        "Submit?",
        "Deliverable",
        "Type",
        "Format",
        "Filename",
        "Mandatory",
        "Deadline",
        "Status",
        "Owner",
        # A folded row keeps IDENTIFIED and mandatory, because the tender still
        # requires that submission. These three columns are what make it a
        # duplicate rather than a second obligation.
        "Duplicate of",
        "Why the same",
        "Judged by",
    ]
    deliverable_rows = [
        [
            "no -- another row carries it" if d.duplicate_of_deliverable_id else "yes",
            d.name,
            d.deliverable_type,
            d.required_format,
            d.required_filename,
            d.is_mandatory,
            d.deadline,
            d.status,
            users.get(d.owner_user_id, ""),
            (
                (by_deliverable_id[d.duplicate_of_deliverable_id].name or "")[:200]
                if d.duplicate_of_deliverable_id in by_deliverable_id
                else ""
            ),
            d.equivalence_reason or "",
            d.equivalence_by or "",
        ]
        for d in deliverables
    ]

    clauses = (
        session.execute(
            select(ClauseResolution)
            .where(
                ClauseResolution.org_id == org,
                ClauseResolution.bid_id == bid.id,
                ClauseResolution.is_current.is_(True),
            )
            .order_by(ClauseResolution.clause_ref)
        )
        .scalars()
        .all()
    )
    clause_headers = ["Clause", "Method", "Confidence", "Rationale", "Resolved by", "Resolved at"]
    clause_rows = [
        [c.clause_ref, c.method, c.confidence, c.rationale, c.resolved_by, c.resolved_at]
        for c in clauses
    ]

    def _proves_capability(evidence_type, is_substantiated) -> str:
        """The verdict as a reader of the workbook needs it, from BOTH axes.

        This existed twice -- once on the Evidence sheet and once on
        Requirement x Evidence -- computed from the type alone, and both said
        "yes" for all 610 rows. A review of that export reported the corpus as
        proving capability wholesale, which included 235 rows whose own text
        reads `Certificate no.: [REPLACE] | Evidence status: NOT EVIDENCED`. A
        column that is confidently wrong is worse than a blank one, because it
        is believed.

        One function rather than two expressions, for the reason the 25
        dangling requirement codes taught: the same logic written in two places
        is the same logic until somebody fixes one of them.

        Order matters. Absence outranks kind -- "this does not exist yet" is
        more use to a drafter than "this is a template" when a row is both --
        and NULL is reported as unjudged rather than as yes, because nobody has
        looked at it. Every row reads `unjudged` today, and that is the honest
        state until the classifier runs.
        """
        reasons = []
        if is_substantiated is False:
            reasons.append("not substantiated")
        if not is_self_evidencing(evidence_type):
            reasons.append("form or template")
        if reasons:
            return "no -- " + "; ".join(reasons)
        return "unjudged" if is_substantiated is None else "yes"

    def documents_for(side: str, bid_id=None):
        query = (
            select(Document, DocumentVersion, DocumentClassification)
            .outerjoin(
                DocumentVersion,
                (DocumentVersion.id == Document.current_version_id)
                & (DocumentVersion.org_id == Document.org_id),
            )
            .outerjoin(
                DocumentClassification,
                (DocumentClassification.document_version_id == DocumentVersion.id)
                & (DocumentClassification.org_id == Document.org_id),
            )
            .where(Document.org_id == org, Document.side == side)
            .order_by(Document.title)
        )
        if bid_id is not None:
            query = query.where(Document.bid_id == bid_id)
        return session.execute(query).all()

    doc_headers = [
        "Title",
        "Original filename",
        # 28 document records exist for 14 files: one live record and one
        # WITHDRAWN record of each. Without this column the sheet lists every
        # file twice with nothing to tell the copies apart, and a reader counts
        # 28 company documents where there are 14. Evidence hanging off the
        # withdrawn half stopped being admissible in `assessment._admissible`,
        # so a reader also needs to know which rows that was.
        "Withdrawn",
        "Classified as",
        "Classifier confidence",
        "Version",
        "State",
        "Pages",
        "Blocks",
        "Chunks",
        "Issue date",
        "Confidentiality",
        "Parse error",
    ]

    def doc_rows(rows):
        return [
            [
                d.title,
                d.original_filename,
                "" if d.withdrawn_at is None else (d.withdrawn_reason or "yes"),
                getattr(c, "document_type", ""),
                getattr(c, "confidence", ""),
                getattr(v, "version_no", ""),
                getattr(v, "state", ""),
                getattr(v, "page_count", ""),
                getattr(v, "block_count", ""),
                getattr(v, "chunk_count", ""),
                getattr(v, "issue_date", ""),
                d.confidentiality_class,
                getattr(v, "parse_error", ""),
            ]
            for d, v, c in rows
        ]

    # --- company --------------------------------------------------------------
    # Named columns, never the whole Evidence entity.
    #
    # `excerpt_vector` is declared as a pgvector column or a float array
    # depending on `settings.vector_backend`, which is resolved at import time
    # from an environment variable that `run_local.py` sets and a standalone
    # script does not. Loading the entity therefore hands pgvector's result
    # processor a plain list and it dies on `.split()`. Selecting columns
    # sidesteps the mismatch entirely -- and an export has no use for a
    # 768-float embedding on every row regardless of which backend is right.
    evidence_rows_raw = session.execute(
        select(
            Evidence.excerpt,
            Evidence.evidence_type,
            Evidence.approval_status,
            Evidence.status,
            Evidence.approved_for_reuse,
            Evidence.effective_date,
            Evidence.expiry_date,
            Evidence.confidentiality_class,
            Evidence.applicable_region,
            Evidence.applicable_product,
            Evidence.applicable_service,
            Document.title,
            Evidence.is_substantiated,
            Evidence.substantiation_note,
            Document.withdrawn_at,
        )
        .outerjoin(
            DocumentVersion,
            (DocumentVersion.id == Evidence.source_document_version_id)
            & (DocumentVersion.org_id == Evidence.org_id),
        )
        .outerjoin(
            Document,
            (Document.id == DocumentVersion.document_id) & (Document.org_id == Evidence.org_id),
        )
        .where(Evidence.org_id == org)
        .order_by(Evidence.created_at)
    ).all()
    ev_headers = [
        "Excerpt",
        "Evidence type",
        "Proves capability",
        # Why not, in the words a person can act on: "the register marks this
        # credential NOT EVIDENCED" says what to go and get, where
        # "unsubstantiated" says nothing.
        "Why not",
        # Evidence on a withdrawn document is no longer admissible. It stays in
        # this sheet because it is still in the corpus, and the reader has to be
        # able to see which rows stopped counting.
        "Source withdrawn",
        "Approval",
        "Status",
        "Approved for reuse",
        "Effective",
        "Expires",
        "Source document",
        "Confidentiality",
        "Region / product / service",
    ]
    evidence_rows = [
        [
            (excerpt or "")[:600],
            evidence_type,
            # Both axes, through the one helper. Computed from the type alone,
            # this column read "yes" for all 610 rows -- including 235 whose own
            # text says the evidence does not exist yet.
            _proves_capability(evidence_type, is_substantiated),
            (substantiation_note or ""),
            "" if withdrawn_at is None else "yes -- no longer admissible",
            approval,
            status,
            # What the flag MEANS for this row, not the stored boolean.
            #
            # `approved_for_reuse` records a past decision and is not cleared
            # when a later version supersedes the excerpt, so 304 superseded
            # rows read "yes" beside a status saying they are no longer
            # admissible. Retrieval already excludes them; the column was the
            # only thing claiming otherwise, and a review read it as a
            # governance failure -- reasonably.
            (
                "no -- superseded"
                if str(getattr(status, "value", status)) == "SUPERSEDED"
                else ("no -- source withdrawn" if withdrawn_at is not None else reusable)
            ),
            effective,
            expires,
            title or "",
            confidentiality,
            [*(region or []), *(product or []), *(service or [])],
        ]
        for (
            excerpt,
            evidence_type,
            approval,
            status,
            reusable,
            effective,
            expires,
            confidentiality,
            region,
            product,
            service,
            title,
            is_substantiated,
            substantiation_note,
            withdrawn_at,
        ) in evidence_rows_raw
    ]

    entities = (
        session.execute(
            select(KnowledgeEntity)
            .where(KnowledgeEntity.org_id == org)
            .order_by(KnowledgeEntity.entity_type, KnowledgeEntity.canonical_name)
        )
        .scalars()
        .all()
    )
    entity_names = {e.id: e.canonical_name for e in entities}
    ent_headers = ["Type", "Name", "Aliases", "Status", "Approval", "Classification"]
    entity_rows = [
        [e.entity_type, e.canonical_name, e.aliases, e.status, e.approval_status, e.classification]
        for e in entities
    ]

    facts = (
        session.execute(
            select(KnowledgeFact)
            .where(KnowledgeFact.org_id == org)
            .order_by(KnowledgeFact.predicate)
        )
        .scalars()
        .all()
    )
    fact_headers = [
        "Entity",
        "Predicate",
        "Value",
        "Unit",
        "Valid from",
        "Valid to",
        "Status",
        "Approval",
        "Confidence",
        "Extracted by",
    ]
    fact_rows = [
        [
            entity_names.get(f.entity_id, ""),
            f.predicate,
            f.value_text or f.value_number or f.value_date or f.value_json,
            f.unit,
            f.valid_from,
            f.valid_to,
            f.status,
            f.approval_status,
            f.confidence,
            f.extracted_by,
        ]
        for f in facts
    ]

    relations = (
        session.execute(select(KnowledgeRelation).where(KnowledgeRelation.org_id == org))
        .scalars()
        .all()
    )
    rel_headers = ["Subject", "Predicate", "Object", "Status", "Approval", "Confidence"]
    relation_rows = [
        [
            entity_names.get(r.subject_entity_id, ""),
            r.predicate,
            entity_names.get(r.object_entity_id, ""),
            r.status,
            r.approval_status,
            r.confidence,
        ]
        for r in relations
    ]

    links = session.execute(select(PersonLink).where(PersonLink.org_id == org)).scalars().all()
    plink_headers = [
        "Person in documents",
        "Linked account",
        "Status",
        "Method",
        "Confidence",
        "Note",
    ]
    person_link_rows = [
        [
            entity_names.get(p.entity_id, ""),
            users.get(p.user_id) or users.get(p.proposed_user_id, ""),
            p.status,
            p.method,
            p.confidence,
            p.note,
        ]
        for p in links
    ]

    # --- the matrix -----------------------------------------------------------
    assessments = {
        a.requirement_id: a
        for a in session.execute(
            select(RequirementAssessment).where(
                RequirementAssessment.org_id == org,
                RequirementAssessment.bid_id == bid.id,
                RequirementAssessment.is_current.is_(True),
            )
        )
        .scalars()
        .all()
    }

    task_by_requirement: dict[uuid.UUID, object] = {}
    for task, requirement_id in session.execute(
        select(Task, TaskRequirement.requirement_id)
        .join(TaskRequirement, TaskRequirement.task_id == Task.id)
        .where(Task.org_id == org, Task.bid_id == bid.id)
    ).all():
        task_by_requirement.setdefault(requirement_id, task)

    # A request belongs to the requirements ITS OWN TASK covers. Joining tasks
    # to requirements on bid_id instead would count every open request on the
    # bid against every requirement in it -- one request would read as forty.
    open_requests: dict[uuid.UUID, int] = {}
    for requirement_id, count in session.execute(
        select(TaskRequirement.requirement_id, func.count(CrossDepartmentRequest.id))
        .join(
            CrossDepartmentRequest,
            CrossDepartmentRequest.origin_task_id == TaskRequirement.task_id,
        )
        .where(
            TaskRequirement.org_id == org,
            CrossDepartmentRequest.bid_id == bid.id,
            CrossDepartmentRequest.state.in_(["PENDING", "ACKNOWLEDGED"]),
        )
        .group_by(TaskRequirement.requirement_id)
    ).all():
        open_requests[requirement_id] = count

    open_clar: dict[uuid.UUID, int] = {}
    for requirement_id, count in session.execute(
        select(ClarificationRequirement.requirement_id, func.count(ClarificationIssue.id))
        .join(
            ClarificationIssue, ClarificationIssue.id == ClarificationRequirement.clarification_id
        )
        .where(
            ClarificationRequirement.org_id == org,
            ClarificationIssue.status.in_(["OPEN", "SENT_TO_CLIENT"]),
            # Restatements hold no links, so this is belt and braces -- but the
            # matrix column is what a reviewer counts, and it must never say a
            # row has seven open questions when it has one asked seven ways.
            ClarificationIssue.restates_clarification_id.is_(None),
            # Questions for the CUSTOMER only. A finding with no
            # `recommended_question` is a note about our own processing --
            # identity drift, a coverage gap -- and the assessment already
            # refuses to let one hold a row. The column did count them, so a
            # review read 64 open questions where 49 were the customer's and 15
            # were our own QA. Same rule in both places, or the matrix and the
            # readiness state disagree about the same row.
            ClarificationIssue.recommended_question.is_not(None),
            func.length(func.trim(ClarificationIssue.recommended_question)) > 0,
        )
        .group_by(ClarificationRequirement.requirement_id)
    ).all():
        open_clar[requirement_id] = count

    evidence_count: dict[uuid.UUID, int] = {}
    for assessment_id, count in session.execute(
        select(AssessmentEvidence.assessment_id, func.count(AssessmentEvidence.id))
        .where(AssessmentEvidence.org_id == org)
        .group_by(AssessmentEvidence.assessment_id)
    ).all():
        evidence_count[assessment_id] = count

    matrix = []
    gaps = []
    for r, v in requirements:
        a = assessments.get(r.id)
        task = task_by_requirement.get(r.id)
        row = [
            r.code,
            r.category,
            r.is_mandatory,
            r.obligation_owner,
            r.applicability,
            getattr(a, "evidence_status", "(not assessed)"),
            getattr(a, "bid_readiness", "(not assessed)"),
            evidence_count.get(getattr(a, "id", None), 0),
            getattr(a, "rationale", ""),
            getattr(a, "assessment_state", ""),
            getattr(task, "code", ""),
            users.get(getattr(task, "owner_user_id", None), ""),
            getattr(task, "state", ""),
            open_requests.get(r.id, 0),
            open_clar.get(r.id, 0),
            getattr(v, "requirement_text", "(no current version -- extraction gap)"),
        ]
        matrix.append(row)
        owner = getattr(r.obligation_owner, "value", r.obligation_owner) or "BIDDER"
        readiness = getattr(a, "bid_readiness", None)
        readiness_value = getattr(readiness, "value", readiness)
        applicability = getattr(r.applicability, "value", r.applicability)
        if is_a_gap(
            mandatory=r.is_mandatory,
            owner=owner,
            applicability=applicability,
            readiness=readiness_value,
        ):
            gaps.append(row)

    rxe_headers = [
        "Requirement",
        "Role",
        "Weight",
        "Retrieval rank",
        "Rerank score",
        "Evidence type",
        "Proves capability",
        "Why not",
        "Evidence status",
        "Source document",
        "Excerpt",
        "Contribution note",
    ]
    # Columns again rather than entities -- same vector-backend reason as above.
    # The requirement code is joined in rather than fetched per row: the old
    # shape issued one SELECT per attachment, which is invisible on a demo bid
    # and a real wait on a bid with hundreds of requirements.
    rxe = [
        [
            code,
            role,
            weight,
            rank,
            rerank,
            evidence_type,
            _proves_capability(evidence_type, is_substantiated),
            (substantiation_note or ""),
            status,
            title or "",
            (excerpt or "")[:600],
            note,
        ]
        for (
            code,
            role,
            weight,
            rank,
            rerank,
            evidence_type,
            status,
            excerpt,
            note,
            title,
            is_substantiated,
            substantiation_note,
        ) in session.execute(
            select(
                Requirement.code,
                AssessmentEvidence.role,
                AssessmentEvidence.weight,
                AssessmentEvidence.retrieval_rank,
                AssessmentEvidence.rerank_score,
                Evidence.evidence_type,
                Evidence.status,
                Evidence.excerpt,
                AssessmentEvidence.contribution_note,
                Document.title,
                Evidence.is_substantiated,
                Evidence.substantiation_note,
            )
            .join(Evidence, Evidence.id == AssessmentEvidence.evidence_id)
            .join(
                RequirementAssessment,
                RequirementAssessment.id == AssessmentEvidence.assessment_id,
            )
            .join(Requirement, Requirement.id == RequirementAssessment.requirement_id)
            .outerjoin(
                DocumentVersion,
                DocumentVersion.id == Evidence.source_document_version_id,
            )
            .outerjoin(Document, Document.id == DocumentVersion.document_id)
            .where(
                AssessmentEvidence.org_id == org,
                RequirementAssessment.bid_id == bid.id,
                RequirementAssessment.is_current.is_(True),
                # Withdrawn requirements keep their assessments -- that is the
                # point of withdrawing rather than deleting -- but their evidence
                # links must not be exported as current. Without this the sheet
                # listed 88 attachments for a matrix of 42 requirements, 16 of
                # them belonging to rows that no longer appear anywhere else in
                # the workbook. A reader reconciling the two sheets finds
                # evidence attached to a requirement that does not exist.
                LIVE_REQUIREMENT,
            )
            .order_by(Requirement.code, AssessmentEvidence.role)
        ).all()
    ]

    # Only tasks that still cover a live requirement.
    #
    # A task raised against a requirement that has since been withdrawn is
    # work nobody should do, and it was being exported as though it were
    # outstanding: 25 tasks for a matrix that referenced 19. A task covering
    # NOTHING (no TaskRequirement rows at all) is kept, because that is a
    # different problem and hiding it would lose it.
    covered_by_live = set(
        session.execute(
            select(TaskRequirement.task_id)
            .join(Requirement, Requirement.id == TaskRequirement.requirement_id)
            .where(TaskRequirement.org_id == org, LIVE_REQUIREMENT)
        )
        .scalars()
        .all()
    )
    has_any_requirement = set(
        session.execute(select(TaskRequirement.task_id).where(TaskRequirement.org_id == org))
        .scalars()
        .all()
    )
    tasks = [
        task
        for task in session.execute(
            select(Task).where(Task.org_id == org, Task.bid_id == bid.id).order_by(Task.code)
        )
        .scalars()
        .all()
        if task.id in covered_by_live or task.id not in has_any_requirement
    ]
    task_headers = [
        "Task",
        "Title",
        "Department",
        "Owner",
        "State",
        "Priority",
        "Due",
        "Blocker",
        "Why grouped this way",
    ]
    task_rows = [
        [
            t.code,
            t.title,
            departments.get(t.department_id, ""),
            users.get(t.owner_user_id, "(unassigned)"),
            t.state,
            t.priority,
            t.due_date,
            t.blocker_note,
            t.grouping_rationale,
        ]
        for t in tasks
    ]

    requests = (
        session.execute(
            select(CrossDepartmentRequest)
            .where(CrossDepartmentRequest.org_id == org, CrossDepartmentRequest.bid_id == bid.id)
            .order_by(CrossDepartmentRequest.created_at)
        )
        .scalars()
        .all()
    )
    request_headers = [
        "Asked of",
        "Type",
        "State",
        "What was asked",
        "Business reason",
        "Answer",
        "Rejection reason",
        "Raised by",
        "Due",
        "Resolved at",
    ]
    request_rows = [
        [
            departments.get(q.target_department_id, ""),
            q.request_type,
            q.state,
            q.request_text,
            q.business_reason,
            q.resolution_text,
            q.rejection_reason,
            users.get(q.requester_user_id, ""),
            q.due_at,
            q.resolved_at,
        ]
        for q in requests
    ]

    stmt_headers = [
        "Task",
        "Artifact state",
        "Version",
        "Order",
        "Statement class",
        "Approval",
        "Text",
    ]
    statements = []
    for statement, version, artifact, task in session.execute(
        select(ArtifactStatement, ArtifactVersion, ResponseArtifact, Task)
        .join(ArtifactVersion, ArtifactVersion.id == ArtifactStatement.artifact_version_id)
        .join(ResponseArtifact, ResponseArtifact.id == ArtifactVersion.artifact_id)
        .outerjoin(Task, Task.id == ResponseArtifact.task_id)
        .where(ArtifactStatement.org_id == org, ResponseArtifact.bid_id == bid.id)
        .order_by(ArtifactVersion.version_no, ArtifactStatement.order_index)
    ).all():
        statements.append(
            [
                getattr(task, "code", ""),
                artifact.state,
                version.version_no,
                statement.order_index,
                statement.statement_class,
                statement.approval_state,
                statement.text,
            ]
        )

    categories = sorted({str(getattr(r.category, "value", r.category)) for r, _ in requirements})

    return {
        "bid_headers": bid_headers,
        "bid": bid_rows,
        "req_headers": req_headers,
        "requirements": requirement_rows,
        "clar_headers": clar_headers,
        "clarifications": clarification_rows,
        "deliv_headers": deliv_headers,
        "deliverables": deliverable_rows,
        "clause_headers": clause_headers,
        "clauses": clause_rows,
        "tdoc_headers": doc_headers,
        "tender_documents": doc_rows(documents_for("TENDER", bid.id)),
        "cdoc_headers": doc_headers,
        "company_documents": doc_rows(documents_for("COMPANY")),
        "ev_headers": ev_headers,
        "evidence": evidence_rows,
        "ent_headers": ent_headers,
        "entities": entity_rows,
        "fact_headers": fact_headers,
        "facts": fact_rows,
        "rel_headers": rel_headers,
        "relations": relation_rows,
        "plink_headers": plink_headers,
        "person_links": person_link_rows,
        "matrix_headers": MATRIX_HEADERS,
        "matrix": matrix,
        "gaps": gaps,
        "rxe_headers": rxe_headers,
        "rxe": rxe,
        "task_headers": task_headers,
        "tasks": task_rows,
        "request_headers": request_headers,
        "requests": request_rows,
        "stmt_headers": stmt_headers,
        "statements": statements,
        "categories": categories,
    }
