"""Enumerations for the Phase E foundation slice.

Python 3.10 target, so these are (str, Enum) rather than StrEnum.
"""

from __future__ import annotations

from enum import Enum


class _Str(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class ActorKind(_Str):
    USER = "USER"
    SYSTEM = "SYSTEM"
    WORKER = "WORKER"


class ConfidentialityClass(_Str):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"
    PRIVILEGED = "PRIVILEGED"


class DepartmentCode(_Str):
    SECURITY = "SECURITY"
    HR = "HR"
    PMO = "PMO"
    SALES = "SALES"
    COMMERCIAL = "COMMERCIAL"
    FINANCE = "FINANCE"
    LEGAL = "LEGAL"
    BID_MANAGEMENT = "BID_MANAGEMENT"
    PRODUCT = "PRODUCT"
    SOLUTION_ARCHITECTURE = "SOLUTION_ARCHITECTURE"
    QUALITY = "QUALITY"


# What each department is called on screen, in a prompt, and in a notification.
#
# These used to be derived as code.value.replace("_", " ").title(), which is
# right for eight of the eleven and wrong for every acronym: HR became "Hr" and
# PMO became "Pmo". That name is not decoration -- it goes into the copilot's
# authorized context ("Asked Hr: ..."), into the department picker on a
# cross-department request, and into every notification title. A rule that is
# right most of the time is worse here than a list that is right every time,
# because the exceptions are exactly the departments people name out loud.
DEPARTMENT_DISPLAY_NAMES: dict[str, str] = {
    DepartmentCode.SECURITY: "Security",
    DepartmentCode.HR: "HR",
    DepartmentCode.PMO: "PMO",
    DepartmentCode.SALES: "Sales",
    DepartmentCode.COMMERCIAL: "Commercial",
    DepartmentCode.FINANCE: "Finance",
    DepartmentCode.LEGAL: "Legal",
    DepartmentCode.BID_MANAGEMENT: "Bid Management",
    DepartmentCode.PRODUCT: "Product",
    DepartmentCode.SOLUTION_ARCHITECTURE: "Solution Architecture",
    DepartmentCode.QUALITY: "Quality",
}


def department_display_name(code: DepartmentCode | str) -> str:
    """The department's name for people. Falls back to the old rule, so an
    unnamed future code degrades to something readable rather than raising."""
    value = getattr(code, "value", code)
    return DEPARTMENT_DISPLAY_NAMES.get(value) or str(value).replace("_", " ").title()


class GrantPermission(_Str):
    READ = "READ"
    READ_EXPORT = "READ_EXPORT"


class GrantState(_Str):
    """Effective state. Derived from expires_at/revoked_at, never authoritative in a column."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class GrantResourceType(_Str):
    EVIDENCE = "EVIDENCE"
    DOCUMENT_VERSION = "DOCUMENT_VERSION"
    KNOWLEDGE_ENTITY = "KNOWLEDGE_ENTITY"
    KNOWLEDGE_FACT = "KNOWLEDGE_FACT"
    KNOWLEDGE_RELATION = "KNOWLEDGE_RELATION"


class ParticipantRole(_Str):
    CONTRIBUTOR = "CONTRIBUTOR"
    REVIEWER = "REVIEWER"
    APPROVER = "APPROVER"
    OBSERVER = "OBSERVER"


# --- Phase F: documents, ingestion, knowledge --------------------------------


class DocumentSide(_Str):
    """Tender documents define requirements; company documents provide evidence.

    One ingestion pipeline serves both; this is what keeps them from being
    confused downstream.
    """

    TENDER = "TENDER"
    COMPANY = "COMPANY"


class DocumentType(_Str):
    # tender side
    RFP = "RFP"
    RFQ = "RFQ"
    AMENDMENT = "AMENDMENT"
    CLARIFICATION = "CLARIFICATION"
    TECHNICAL_SPECIFICATION = "TECHNICAL_SPECIFICATION"
    BOQ = "BOQ"
    COMMERCIAL_TEMPLATE = "COMMERCIAL_TEMPLATE"
    RESPONSE_TEMPLATE = "RESPONSE_TEMPLATE"
    TERMS_AND_CONDITIONS = "TERMS_AND_CONDITIONS"
    APPENDIX = "APPENDIX"
    FORM = "FORM"
    QUESTIONNAIRE = "QUESTIONNAIRE"
    # --- company side ---------------------------------------------------
    # Grouped to match how a bid team actually files things. The distinctions
    # that look pedantic are the ones that change what can be claimed: a
    # certificate carries an expiry, a policy does not; a past RFP records what
    # a customer once asked for, while the response records what this company
    # actually offered.
    #
    # 1. Company profile
    COMPANY_PROFILE = "COMPANY_PROFILE"
    CAPABILITY_STATEMENT = "CAPABILITY_STATEMENT"
    ORG_STRUCTURE = "ORG_STRUCTURE"
    # 2. Legal and registration
    TRADE_LICENCE = "TRADE_LICENCE"
    INCORPORATION_CERTIFICATE = "INCORPORATION_CERTIFICATE"
    TAX_REGISTRATION = "TAX_REGISTRATION"
    OWNERSHIP_DOCUMENT = "OWNERSHIP_DOCUMENT"
    SIGNATORY_AUTHORITY = "SIGNATORY_AUTHORITY"
    INSURANCE_CERTIFICATE = "INSURANCE_CERTIFICATE"
    LEGAL_DOC = "LEGAL_DOC"
    # 3. Certifications and accreditations
    CERTIFICATE = "CERTIFICATE"
    ACCREDITATION = "ACCREDITATION"
    PARTNER_CERTIFICATE = "PARTNER_CERTIFICATE"
    # 4/5. People
    CV = "CV"
    PROFESSIONAL_CERTIFICATE = "PROFESSIONAL_CERTIFICATE"
    # 6. Policies
    POLICY = "POLICY"
    # 7. Support and service
    SUPPORT_DOC = "SUPPORT_DOC"
    SLA_DOC = "SLA_DOC"
    ESCALATION_MATRIX = "ESCALATION_MATRIX"
    WARRANTY_TERMS = "WARRANTY_TERMS"
    # 8. Historical bid files. PREVIOUS_TENDER is what a customer asked for;
    # PREVIOUS_PROPOSAL is what this company answered. Only the second is
    # evidence of the company's own capability, which is why they are separate
    # types rather than one "old bid" bucket.
    PREVIOUS_TENDER = "PREVIOUS_TENDER"
    PREVIOUS_PROPOSAL = "PREVIOUS_PROPOSAL"
    PREVIOUS_COMMERCIAL_RESPONSE = "PREVIOUS_COMMERCIAL_RESPONSE"
    PREVIOUS_CLARIFICATION = "PREVIOUS_CLARIFICATION"
    # 9. Project proof
    PROJECT_REFERENCE = "PROJECT_REFERENCE"
    CASE_STUDY = "CASE_STUDY"
    CONTRACT = "CONTRACT"
    PURCHASE_ORDER = "PURCHASE_ORDER"
    COMPLETION_CERTIFICATE = "COMPLETION_CERTIFICATE"
    REFERENCE_LETTER = "REFERENCE_LETTER"
    AWARD_NOTICE = "AWARD_NOTICE"
    # 10/11. Commercial and financial
    FINANCIAL_STATEMENT = "FINANCIAL_STATEMENT"
    BANK_LETTER = "BANK_LETTER"
    PRICING_TEMPLATE = "PRICING_TEMPLATE"
    COMMERCIAL_DOC = "COMMERCIAL_DOC"
    RATE_CARD = "RATE_CARD"
    # 12. Other reusable material
    METHODOLOGY = "METHODOLOGY"
    ARCHITECTURE = "ARCHITECTURE"
    PRODUCT_DOC = "PRODUCT_DOC"
    SECURITY_DOC = "SECURITY_DOC"
    COMPLETED_QUESTIONNAIRE = "COMPLETED_QUESTIONNAIRE"
    STANDARD_FORM = "STANDARD_FORM"
    PARTNER_DOC = "PARTNER_DOC"
    OTHER = "OTHER"


# Document types that are not, on their own, evidence that this company has
# done anything.
#
# The distinction is already in the taxonomy above -- "PREVIOUS_TENDER is what a
# customer asked for; PREVIOUS_PROPOSAL is what this company answered. Only the
# second is evidence of the company's own capability" -- and until now nothing
# read it. `Evidence.evidence_type` was stamped on every extracted excerpt at
# ingestion and then never consulted again: not in matching, not in scoring, not
# in what the copilot was shown.
#
# The cost was visible and unattributable. Asked about personnel, the copilot
# reported twelve approved evidence items covering "CV update forms for roles
# like Director, Software Tester and Team Lead, and financial pack templates" --
# blank forms, attached to a mandatory requirement about key personnel, and
# indistinguishable in the prompt from a signed reference letter.
#
# These types are not excluded. A blank pricing template genuinely is the
# closest material a company holds, and hiding it would leave a drafter with an
# empty screen and no idea why. They are LABELLED, so a reader -- human or
# model -- can tell the shape of an answer from the answer.
NOT_SELF_EVIDENCING: frozenset[str] = frozenset(
    {
        DocumentType.COMMERCIAL_TEMPLATE,
        DocumentType.RESPONSE_TEMPLATE,
        DocumentType.PRICING_TEMPLATE,
        DocumentType.FORM,
        DocumentType.STANDARD_FORM,
        DocumentType.QUESTIONNAIRE,
        # The customer's wish list, not this company's record.
        DocumentType.PREVIOUS_TENDER,
        # Tender-side material generally: it states what was asked for.
        DocumentType.RFP,
        DocumentType.RFQ,
        DocumentType.BOQ,
        DocumentType.TECHNICAL_SPECIFICATION,
        DocumentType.TERMS_AND_CONDITIONS,
    }
)


def is_self_evidencing(document_type: DocumentType | str | None) -> bool:
    """Whether material of this type can, by itself, show what the company has
    done. Unknown types are treated as evidence: a new type nobody classified
    should not be silently downgraded."""
    if document_type is None:
        return True
    return getattr(document_type, "value", document_type) not in NOT_SELF_EVIDENCING


class DocumentVersionState(_Str):
    UPLOADED = "UPLOADED"
    SCANNING = "SCANNING"
    SCAN_FAILED = "SCAN_FAILED"
    QUARANTINED = "QUARANTINED"
    PARSING = "PARSING"
    PARSE_FAILED = "PARSE_FAILED"
    PARSED = "PARSED"
    CLASSIFIED = "CLASSIFIED"
    INDEXED = "INDEXED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"


class BlockType(_Str):
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST_ITEM = "LIST_ITEM"
    TABLE = "TABLE"
    TABLE_ROW = "TABLE_ROW"
    TABLE_CELL = "TABLE_CELL"
    FIGURE_CAPTION = "FIGURE_CAPTION"
    FORM_FIELD = "FORM_FIELD"
    FOOTNOTE = "FOOTNOTE"
    PAGE_HEADER = "PAGE_HEADER"
    PAGE_FOOTER = "PAGE_FOOTER"


class DocumentRelationType(_Str):
    AMENDS = "AMENDS"
    CLARIFIES = "CLARIFIES"
    SUPERSEDES = "SUPERSEDES"
    REPLACES_CLAUSE = "REPLACES_CLAUSE"
    ATTACHMENT_OF = "ATTACHMENT_OF"
    TRANSLATION_OF = "TRANSLATION_OF"


class EntityType(_Str):
    PERSON = "PERSON"
    PROJECT = "PROJECT"
    CERTIFICATION = "CERTIFICATION"
    CLIENT = "CLIENT"
    PRODUCT = "PRODUCT"
    METHODOLOGY = "METHODOLOGY"
    POLICY = "POLICY"
    LEGAL_ENTITY = "LEGAL_ENTITY"
    OFFICE = "OFFICE"
    RATE_CARD = "RATE_CARD"
    PARTNER = "PARTNER"
    ACCREDITATION_BODY = "ACCREDITATION_BODY"
    TECHNOLOGY = "TECHNOLOGY"


class RelationPredicate(_Str):
    HAS_CERTIFICATION = "HAS_CERTIFICATION"
    WORKED_ON = "WORKED_ON"
    EMPLOYED_BY = "EMPLOYED_BY"
    DELIVERED_FOR = "DELIVERED_FOR"
    HOLDS_ROLE = "HOLDS_ROLE"
    ISSUED_BY = "ISSUED_BY"
    SUBCONTRACTOR_OF = "SUBCONTRACTOR_OF"
    REFERENCES = "REFERENCES"
    PART_OF = "PART_OF"
    USES_TECHNOLOGY = "USES_TECHNOLOGY"
    GOVERNED_BY = "GOVERNED_BY"
    SUPERSEDED_BY = "SUPERSEDED_BY"


class KnowledgeStatus(_Str):
    """Currentness. Distinct from approval: a fact can be approved but expired."""

    CURRENT_VERIFIED = "CURRENT_VERIFIED"
    HISTORICAL_VERIFIED = "HISTORICAL_VERIFIED"
    PROPOSED = "PROPOSED"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    CONFLICTING = "CONFLICTING"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class ApprovalStatus(_Str):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class EvidenceLinkTarget(_Str):
    KNOWLEDGE_FACT = "KNOWLEDGE_FACT"
    KNOWLEDGE_RELATION = "KNOWLEDGE_RELATION"
    KNOWLEDGE_ENTITY = "KNOWLEDGE_ENTITY"
    REQUIREMENT_ASSESSMENT = "REQUIREMENT_ASSESSMENT"
    ARTIFACT_STATEMENT = "ARTIFACT_STATEMENT"


class EvidenceLinkRole(_Str):
    SUPPORTS = "SUPPORTS"
    PARTIALLY_SUPPORTS = "PARTIALLY_SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    CONTEXT = "CONTEXT"


class ReviewQueueKind(_Str):
    ENTITY_MATCH = "ENTITY_MATCH"
    CONFLICT = "CONFLICT"
    EXPIRING = "EXPIRING"
    PROPOSED = "PROPOSED"


# --- Phase G: tender intelligence --------------------------------------------


class BidState(_Str):
    DRAFT = "DRAFT"
    INTAKE_IN_PROGRESS = "INTAKE_IN_PROGRESS"
    UNDERSTANDING_COMPLETE = "UNDERSTANDING_COMPLETE"
    ASSESSMENT_COMPLETE = "ASSESSMENT_COMPLETE"
    BID_NO_BID_DECIDED = "BID_NO_BID_DECIDED"
    IN_PROGRESS = "IN_PROGRESS"
    ASSEMBLY = "ASSEMBLY"
    VALIDATION = "VALIDATION"
    RELEASE_PENDING = "RELEASE_PENDING"
    RELEASED = "RELEASED"
    ARCHIVED = "ARCHIVED"
    NO_BID = "NO_BID"
    CANCELLED = "CANCELLED"


class PrecedenceMethod(_Str):
    """How a clause winner was decided, strongest authority first."""

    EXPLICIT_SUPERSESSION = "EXPLICIT_SUPERSESSION"
    CUSTOMER_STATED_RULE = "CUSTOMER_STATED_RULE"
    ISSUE_DATE = "ISSUE_DATE"
    VERSION_LABEL = "VERSION_LABEL"
    DOCUMENT_TYPE_DEFAULT = "DOCUMENT_TYPE_DEFAULT"
    MANUAL = "MANUAL"


class RequirementCategory(_Str):
    TECHNICAL = "TECHNICAL"
    FUNCTIONAL = "FUNCTIONAL"
    NON_FUNCTIONAL = "NON_FUNCTIONAL"
    SECURITY = "SECURITY"
    IMPLEMENTATION = "IMPLEMENTATION"
    PROJECT_MANAGEMENT = "PROJECT_MANAGEMENT"
    SUPPORT = "SUPPORT"
    TRAINING = "TRAINING"
    DOCUMENTATION = "DOCUMENTATION"
    STAFFING = "STAFFING"
    ELIGIBILITY = "ELIGIBILITY"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    SUBMISSION = "SUBMISSION"
    LEGAL = "LEGAL"
    COMMERCIAL = "COMMERCIAL"
    CONTRACTUAL = "CONTRACTUAL"


class RequirementLifecycle(_Str):
    """Deliberately thin.

    Coverage and readiness live in the assessment, tasks and artifacts that
    reference a requirement, not on the requirement itself, so that requirement
    state and task state cannot contradict each other.
    """

    DRAFT_EXTRACTED = "DRAFT_EXTRACTED"
    CONFIRMED = "CONFIRMED"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"


class RequirementApplicability(_Str):
    ACTIVE = "ACTIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    CONDITIONAL_PENDING = "CONDITIONAL_PENDING"


class RiskLevel(_Str):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ScopeConfidenceLevel(_Str):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ClarificationIssueType(_Str):
    BLANK = "BLANK"
    PLACEHOLDER = "PLACEHOLDER"
    CONTRADICTORY_DATE = "CONTRADICTORY_DATE"
    CONTRADICTORY_QUANTITY = "CONTRADICTORY_QUANTITY"
    CONFLICTING_INSTRUCTION = "CONFLICTING_INSTRUCTION"
    UNDEFINED_FIELD = "UNDEFINED_FIELD"
    INCOMPLETE_SCOPE = "INCOMPLETE_SCOPE"
    PRECEDENCE_UNRESOLVED = "PRECEDENCE_UNRESOLVED"
    OUTDATED_TEMPLATE = "OUTDATED_TEMPLATE"
    COVERAGE_GAP = "COVERAGE_GAP"


class ClarificationSeverity(_Str):
    INFO = "INFO"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    BLOCKING = "BLOCKING"


class ClarificationStatus(_Str):
    OPEN = "OPEN"
    SENT_TO_CLIENT = "SENT_TO_CLIENT"
    ANSWERED = "ANSWERED"
    CLOSED = "CLOSED"
    WITHDRAWN = "WITHDRAWN"


class GeneratedFormat(_Str):
    """Output formats a tender can require. Shared with later assembly phases."""

    DOCX = "DOCX"
    XLSX = "XLSX"
    PDF = "PDF"
    CSV = "CSV"
    ZIP = "ZIP"


class DeliverableStatus(_Str):
    IDENTIFIED = "IDENTIFIED"
    IN_PROGRESS = "IN_PROGRESS"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# --- Phase H: assessment and readiness ----------------------------------------


class EvidenceStatus(_Str):
    """What the evidence says. Orthogonal to bid readiness.

    Collapsing these two axes into one status is the mistake that makes
    "historical evidence read as current" invisible: a requirement can be fully
    evidenced and still not ready because the proof expired.
    """

    CURRENT_VERIFIED = "CURRENT_VERIFIED"
    HISTORICAL_VERIFIED = "HISTORICAL_VERIFIED"
    PARTIAL = "PARTIAL"
    CONFLICTING = "CONFLICTING"
    NONE = "NONE"
    EXPIRED = "EXPIRED"


class PersonLinkStatus(_Str):
    """Where a proposed CV-to-account link has got to.

    PROPOSED is the matcher's suggestion and means nothing on its own.
    CONFIRMED is a person saying yes. REJECTED is a person saying no, and is
    remembered so the matcher does not propose the same wrong pair again.
    """

    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class ObligationOwner(_Str):
    """Who the clause actually binds.

    A tender is not a list of things the bidder must do. A large part of it is
    the customer reserving rights for itself — to waive a non-conformity, to
    reject any offer, to disqualify, to decide finally. Those clauses are still
    requirements worth registering and worth a person reading, but no company
    document can ever evidence one, so treating them like bidder obligations
    parks them at BLOCKED forever and buries the real gaps underneath.

    BIDDER is the default and the safe answer. A clause only moves off it when
    the text plainly puts the obligation on the customer: mislabelling a bidder
    obligation as the customer's would take a genuine no-bid risk off the red
    list, which is the one failure this distinction must never cause.
    """

    BIDDER = "BIDDER"
    CUSTOMER = "CUSTOMER"
    MUTUAL = "MUTUAL"


class BidReadiness(_Str):
    """What has to happen next. Orthogonal to evidence status."""

    READY = "READY"
    DRAFTABLE = "DRAFTABLE"
    NEEDS_REFRESH = "NEEDS_REFRESH"
    NEEDS_COMPANY_CONFIRMATION = "NEEDS_COMPANY_CONFIRMATION"
    NEEDS_CLIENT_CLARIFICATION = "NEEDS_CLIENT_CLARIFICATION"
    REQUIRES_SIGNATURE = "REQUIRES_SIGNATURE"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    # The clause binds the customer. The bidder has nothing to evidence, confirm
    # or ask about it; it is recorded so it can be read, not worked.
    CUSTOMER_OBLIGATION = "CUSTOMER_OBLIGATION"


class AssessmentState(_Str):
    PENDING = "PENDING"
    COMPUTED = "COMPUTED"
    HUMAN_ADJUSTED = "HUMAN_ADJUSTED"
    STALE = "STALE"


class AssessmentEvidenceRole(_Str):
    PRIMARY = "PRIMARY"
    SUPPORTING = "SUPPORTING"
    PARTIAL = "PARTIAL"
    CONTRADICTING = "CONTRADICTING"
    REFERENCE = "REFERENCE"


class BidNoBidResult(_Str):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


# --- Phase I / J / K: tasks, conversations, cross-department requests ---------


class TaskState(_Str):
    """Where a unit of contributor work stands.

    `AI_READY_FOR_REVIEW` is not `COMPLETED`. The system may have drafted
    something worth reading, and a human has still not read it. Collapsing the
    two is how a generated draft reaches a customer unreviewed.
    """

    NOT_STARTED = "NOT_STARTED"
    AI_READY_FOR_REVIEW = "AI_READY_FOR_REVIEW"
    NEEDS_INPUT = "NEEDS_INPUT"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_FOR_DEPENDENCY = "WAITING_FOR_DEPENDENCY"
    WAITING_FOR_ACCESS = "WAITING_FOR_ACCESS"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    REVALIDATION_REQUIRED = "REVALIDATION_REQUIRED"


class TaskPriority(_Str):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TaskDependencyType(_Str):
    BLOCKS = "BLOCKS"
    WAITS_FOR = "WAITS_FOR"


class ChatRole(_Str):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM_SUMMARY = "SYSTEM_SUMMARY"
    SYSTEM_EVENT = "SYSTEM_EVENT"


class RequestType(_Str):
    """Offered in least-privilege order: an answer grants nothing at all."""

    REQUEST_ANSWER = "REQUEST_ANSWER"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    REQUEST_ACCESS = "REQUEST_ACCESS"


class RequestState(_Str):
    PENDING = "PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ANSWERED = "ANSWERED"
    EVIDENCE_SHARED = "EVIDENCE_SHARED"
    ACCESS_GRANTED = "ACCESS_GRANTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


# --- Phase L: statements, artifacts, reviews, approvals ----------------------


class StatementClass(_Str):
    """What kind of claim a sentence in a response is.

    This is the distinction the whole drafting model rests on. "We hold ISO
    27001" is a fact somebody can check; "we will staff two OSCP testers" is a
    promise the company has not yet made; "we assume the client provides VPN
    access" is neither. A response that cannot tell them apart cannot be
    reviewed, because the reviewer has no way to know which sentences bind the
    company.
    """

    EVIDENCE_BACKED_FACT = "EVIDENCE_BACKED_FACT"
    TENDER_DERIVED = "TENDER_DERIVED"
    PROPOSED_COMMITMENT = "PROPOSED_COMMITMENT"
    ASSUMPTION = "ASSUMPTION"
    HUMAN_PROVIDED_INPUT = "HUMAN_PROVIDED_INPUT"
    # A sentence that says what a diagram shows, written for a reader who has the
    # picture beside it. It claims nothing about the company and asks nothing of the
    # tender, so it is neither a fact, a quotation nor a promise; forcing it into one of
    # those is how a description of a drawing ended up labelled "from the tender".
    DIAGRAM_DESCRIPTION = "DIAGRAM_DESCRIPTION"


class StatementApproval(_Str):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ArtifactState(_Str):
    DRAFT = "DRAFT"
    SUBMITTED_FOR_REVIEW = "SUBMITTED_FOR_REVIEW"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    REVALIDATION_REQUIRED = "REVALIDATION_REQUIRED"
    SUPERSEDED = "SUPERSEDED"


class ArtifactVersionState(_Str):
    """A version is mutable only while DRAFT. There is no update path after."""

    DRAFT = "DRAFT"
    SUBMITTED_FOR_REVIEW = "SUBMITTED_FOR_REVIEW"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    SUPERSEDED = "SUPERSEDED"


class ReviewDecision(_Str):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ApprovalStepState(_Str):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"


class AssumptionStatus(_Str):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    CARRIED_INTO_SUBMISSION = "CARRIED_INTO_SUBMISSION"
    WITHDRAWN = "WITHDRAWN"


# --- Phases M-P: assembly, validation, release and learning -----------------


class StructureDerivationMethod(_Str):
    TENDER_SPECIFIED = "TENDER_SPECIFIED"
    TEMPLATE = "TEMPLATE"
    MANUAL = "MANUAL"


class InclusionMode(_Str):
    FULL = "FULL"
    EXCERPT = "EXCERPT"
    REFERENCE = "REFERENCE"


class OutputMode(_Str):
    REVIEW = "REVIEW"
    SUBMISSION = "SUBMISSION"


class DocumentStream(_Str):
    TECHNICAL = "TECHNICAL"
    COMMERCIAL = "COMMERCIAL"
    SUPPORTING = "SUPPORTING"


class DocumentEvidenceStatus(_Str):
    """A person's decision about a company document offered as proof in a bid."""

    UNREVIEWED = "UNREVIEWED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class UseScope(_Str):
    """Where an evidence document may go. A decision for a person, never inferred."""

    INTERNAL_ONLY = "INTERNAL_ONLY"
    EXTERNAL_BID = "EXTERNAL_BID"


class PackageState(_Str):
    ASSEMBLING = "ASSEMBLING"
    REVIEW_MODE_READY = "REVIEW_MODE_READY"
    VALIDATION_RUNNING = "VALIDATION_RUNNING"
    BLOCKED_HARD = "BLOCKED_HARD"
    BLOCKED_PENDING_EXCEPTION = "BLOCKED_PENDING_EXCEPTION"
    RELEASE_PENDING_APPROVALS = "RELEASE_PENDING_APPROVALS"
    RELEASED = "RELEASED"
    ARCHIVED = "ARCHIVED"


class ValidationRunStatus(_Str):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class FindingSeverity(_Str):
    BLOCKING = "BLOCKING"
    WARNING = "WARNING"
    INFO = "INFO"


class FindingWaivability(_Str):
    NON_WAIVABLE = "NON_WAIVABLE"
    WAIVABLE = "WAIVABLE"


class ExceptionState(_Str):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class OutcomeResult(_Str):
    WON = "WON"
    LOST = "LOST"
    NO_DECISION = "NO_DECISION"
    WITHDRAWN = "WITHDRAWN"


class CandidateState(_Str):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# --- Diagrams ---------------------------------------------------------------


class DiagramType(_Str):
    """The kinds of picture a response may need.

    A closed list, because "which diagrams does this section need" is a
    question with an answer the platform can check, and because each type has a
    drawing the platform knows how to produce. A model may propose a type from
    this list; it may not invent one.
    """

    FLOW = "FLOW"  # a process or workflow, optionally in swimlanes
    ARCHITECTURE = "ARCHITECTURE"  # the proposed solution's components
    CUSTOMER_CONTEXT = "CUSTOMER_CONTEXT"  # the customer's side and how it connects
    DATA_FLOW = "DATA_FLOW"  # what data moves where
    NETWORK = "NETWORK"  # zones, links, boundaries
    ORG_CHART = "ORG_CHART"  # project team and reporting lines
    SEQUENCE = "SEQUENCE"  # who says what to whom, in order


class DiagramOrigin(_Str):
    AI_GENERATED = "AI_GENERATED"
    AI_REVISED = "AI_REVISED"
    STYLE_CHANGED = "STYLE_CHANGED"
    HUMAN_EDITED = "HUMAN_EDITED"
    IMAGE_IMPORTED = "IMAGE_IMPORTED"


class DiagramNeedStatus(_Str):
    SUGGESTED = "SUGGESTED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
