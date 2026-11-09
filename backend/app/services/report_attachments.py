"""Company documents the tender asks to be sent with the proposal.

A tender often asks for resumes of key personnel, load test results, a standard set
of terms and conditions and relevant past performance. The company holds each of
them as a document in its knowledge base. They are attached as they are held, word
for word, so nothing in an attachment is something a model wrote and nothing is
trimmed (a notice inside a document stays in it).

Never attached: a document that is withdrawn, one classified RESTRICTED or
PRIVILEGED, or one that says its own contents must be verified before reuse.
Attaching a document does not make the matching deliverable ready; that stays a
decision for a person.

Each document goes out as its own attachment file, with its own versions (see
`release.assemble`). A document is fit for a customer copy only when a person has
approved it (`evidence_status`) and cleared it for outside use (`evidence_use_scope`),
and only if its own text does not say it is a test fixture. The fixture test reads the
text, so no field can override it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.models import (
    Bid,
    Document,
    DocumentBlock,
    DocumentClassification,
    DocumentVersion,
    Opportunity,
)
from app.models.enums import (
    BlockType,
    ConfidentialityClass,
    DocumentEvidenceStatus,
    DocumentSide,
    UseScope,
)

HELD_BACK = {ConfidentialityClass.RESTRICTED, ConfidentialityClass.PRIVILEGED}

# (appendix title, how a document qualifies)
GROUPS: tuple[tuple[str, str], ...] = (
    ("Key personnel curricula vitae", "CV"),
    ("Load test report", "LOAD_TEST"),
    ("Standard terms and conditions", "LEGAL_DOC"),
    ("Project references", "PROJECT_REFERENCE"),
    ("Total cost of ownership schedule", "TCO"),
    ("Work samples", "WORK_SAMPLE"),
    ("Signed NDA", "NDA"),
    ("Board resolution", "BOARD"),
)
LOAD_TEST = re.compile(r"load[\s_-]*test", re.IGNORECASE)
TCO_TITLE = re.compile(r"total[\s_-]*cost[\s_-]*of[\s_-]*ownership", re.IGNORECASE)
SAMPLE_TITLE = re.compile(r"work[\s_-]*samples?", re.IGNORECASE)
NDA_TITLE = re.compile(r"signed[\s_-]*(?:jadwa[\s_-]*)?nda", re.IGNORECASE)
BOARD_TITLE = re.compile(r"board[\s_-]*resolution", re.IGNORECASE)
# A document that says its own contents are not yet cleared for a new bid is not attached.
UNVERIFIED = re.compile(r"must be verified|do not establish|consent for a new bid", re.IGNORECASE)


# What the tender must say before a kind of company document is attached to its answer.
# A load test report has no place in a bid that never asks for one.
ASKED_FOR: dict[str, re.Pattern[str]] = {
    "CV": re.compile(
        r"\b(?:r[eé]sum[eé]s?|CVs?|curricul\w+|key personnel|team members?|biograph\w+)\b",
        re.IGNORECASE,
    ),
    "LOAD_TEST": re.compile(
        r"(?:load|performance|stress)[\s_-]*(?:and[\s_-]*\w+[\s_-]*)?test", re.IGNORECASE
    ),
    "LEGAL_DOC": re.compile(
        r"terms\s+(?:and|&)\s+conditions|standard\s+terms|terms of (?:business|supply|service)",
        re.IGNORECASE,
    ),
    "PROJECT_REFERENCE": re.compile(
        r"(?:client|customer|project)\s+references?|references?\s+(?:from|projects?|clients?"
        r"|customers?)|past performance|previous (?:projects?|experience|work)"
        r"|similar (?:projects?|work)|track record|relevant experience",
        re.IGNORECASE,
    ),
    "TCO": re.compile(r"total\s+cost\s+of\s+ownership|\bTCO\b", re.IGNORECASE),
    "NDA": re.compile(r"\bNDA\b|non-?disclosure", re.IGNORECASE),
    "BOARD": re.compile(
        r"authori[sz]ed\s+(?:representative|signator)|representative\s+authori[sz]ed|signed\s+(?:by|proposal)|authority\s+to\s+(?:sign|commit)",
        re.IGNORECASE,
    ),
    "WORK_SAMPLE": re.compile(
        r"samples?\s+of\s+(?:your\s+|previous\s+)?work|work\s+samples?", re.IGNORECASE
    ),
}

# Kinds that are written for one particular bid: a board resolution authorising one
# submission, a signed NDA with one customer, a CV pack proposed for one project, a cost
# schedule for one tender. They are attached only to that bid.
BID_SPECIFIC = frozenset({"BOARD", "NDA", "CV", "TCO"})
_NAME_NOISE = frozenset(
    "the and for of al el company co llc ltd limited group holding holdings inc plc pjsc psc "
    "bank investment investments capital authority ministry department national general "
    "abu dhabi dubai sharjah saudi arabia uae emirates qatar kuwait oman bahrain".split()
)


def _customer_tokens(name: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", (name or "").casefold())
        if len(t) >= 4 and t not in _NAME_NOISE
    }


def _names_customer(text: str, tokens: set[str]) -> bool:
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    return bool(tokens & words)


def _customers(session: Session, ctx: ExecutionContext, bid_id) -> tuple[set[str], set[str]]:
    """(distinctive name words of this bid's customer, those of every other bid's customer)."""
    own: set[str] = set()
    other: set[str] = set()
    rows = session.execute(
        select(Bid.id, Opportunity.customer_name)
        .join(Opportunity, Opportunity.id == Bid.opportunity_id)
        .where(Bid.org_id == ctx.org_id)
    ).all()
    for b_id, customer in rows:
        (own if b_id == bid_id else other).update(_customer_tokens(customer))
    # Customers with an opportunity but no bid yet are other customers too.
    for (customer,) in session.execute(
        select(Opportunity.customer_name).where(Opportunity.org_id == ctx.org_id)
    ).all():
        other.update(_customer_tokens(customer))
    return own, other - own


def _tender_text(session: Session, ctx: ExecutionContext, bid_id) -> str:
    from app.services.tender_analysis import tender_blocks

    return " ".join(b.text for b in tender_blocks(session, ctx, bid_id=bid_id))


def _kind(title: str, document_type: str) -> str:
    if LOAD_TEST.search(title):
        return "LOAD_TEST"
    if TCO_TITLE.search(title):
        return "TCO"
    if SAMPLE_TITLE.search(title):
        return "WORK_SAMPLE"
    if NDA_TITLE.search(title):
        return "NDA"
    if BOARD_TITLE.search(title):
        return "BOARD"
    return document_type


def _body(blocks: list[DocumentBlock]) -> str:
    out: list[str] = []
    table: list[str] = []

    def flush() -> None:
        if table:
            out.append("\n".join(table))
            table.clear()

    for block in blocks:
        # Line breaks inside a block are kept: they separate a heading from its text and
        # one list item from the next. Only the spaces within each line are tidied.
        text = "\n".join(
            " ".join(line.split()) for line in (block.text or "").splitlines() if line.strip()
        )
        if not text:
            continue
        if block.block_type is BlockType.TABLE:
            flush()
            table.append("| " + " | ".join(c.strip() for c in text.split("|")) + " |")
        elif block.block_type is BlockType.TABLE_ROW:
            cells = [re.sub(r"^[^:|]{1,60}:\s*", "", c.strip()) for c in text.split("|")]
            table.append("| " + " | ".join(cells) + " |")
        else:
            flush()
            out.append(("- " if block.block_type is BlockType.LIST_ITEM else "") + text)
    flush()
    return "\n\n".join(out)


FIXTURE = re.compile(r"\b(?:fictional|synthetic)\s+test\s+fixture\b", re.IGNORECASE)


@dataclass(frozen=True)
class Attachment:
    document_id: object
    version_id: object
    group: str
    title: str
    body: str
    fixture: bool
    status: DocumentEvidenceStatus
    scope: UseScope
    provenance: str | None
    evidence_type: str = ""
    attestation: str = "UNVERIFIED"
    attested_by: object = None
    attested_at: datetime | None = None
    attestation_method: str | None = None
    attested_version_hash: str | None = None
    current_version_hash: str | None = None

    @property
    def attestation_valid(self) -> bool:
        """A person attested this exact file: the stored hash equals the current version's."""
        return self.attestation == "ATTESTED"

    @property
    def reality(self) -> str:
        """SYNTHETIC, UNVERIFIED or ATTESTED: whether a person vouched that it is genuine.

        Taken from the structured attestation on the document, never from prose."""
        if self.fixture or self.attestation == "SYNTHETIC":
            return "SYNTHETIC"
        return "ATTESTED" if self.attestation == "ATTESTED" else "UNVERIFIED"

    @property
    def cleared(self) -> bool:
        """A person approved it, cleared it for a customer, and attested that it is genuine.

        A document of unknown origin is never taken as real: approval says it is good
        evidence if genuine, the attestation says it is genuine.
        """
        return (
            not self.fixture
            and self.status is DocumentEvidenceStatus.APPROVED
            and self.scope is UseScope.EXTERNAL_BID
            and self.reality == "ATTESTED"
        )

    @property
    def submission_status(self) -> str:
        """The one answer a downstream reader needs: can this go to a customer?"""
        if self.fixture:
            return "BLOCKED_FIXTURE"
        if self.status is not DocumentEvidenceStatus.APPROVED:
            return "BLOCKED_NOT_APPROVED"
        if self.scope is not UseScope.EXTERNAL_BID:
            return "BLOCKED_NOT_CLEARED_FOR_CUSTOMER"
        if self.reality != "ATTESTED":
            return "BLOCKED_PROVENANCE_UNVERIFIED"
        return "ELIGIBLE"


def _current_hash(session: Session, document: Document) -> str | None:
    version = (
        session.get(DocumentVersion, document.current_version_id)
        if document.current_version_id
        else None
    )
    return getattr(version, "sha256", None)


def attestation_state(session: Session, document: Document) -> str:
    """ATTESTED, SYNTHETIC or UNVERIFIED, from the structured fields on the document.

    ATTESTED needs a person (attested_by), a method, and a hash equal to the current
    version, so editing the file after attestation drops it back to UNVERIFIED.
    Wording in the provenance never grants genuine status."""
    reality = (getattr(document, "evidence_reality", None) or "UNVERIFIED").upper()
    if reality == "SYNTHETIC":
        return "SYNTHETIC"
    if reality != "GENUINE" or not document.attested_by or not document.attestation_method:
        return "UNVERIFIED"
    version = (
        session.get(DocumentVersion, document.current_version_id)
        if document.current_version_id
        else None
    )
    if (
        version is None
        or not document.attested_version_hash
        or version.sha256 != document.attested_version_hash
    ):
        return "UNVERIFIED"
    return "ATTESTED"


# Dev test data: documents a team member invented to exercise the system. They say so in
# their own closing note, and the person who approved them recorded it in the provenance.
TEST_DATA = re.compile(
    r"\bdev(?:elopment)?\s+test\s+data\b"
    r"|\bnames,?\s+dates\s+and\s+figures\s+are\s+invented\b"
    r"|\bsynthetic\s+(?:test\s+)?data\b",
    re.IGNORECASE,
)


def is_fixture(text: str) -> bool:
    return bool(FIXTURE.search(text))


def is_test_data(text: str, provenance: str | None = None) -> bool:
    """A document that is invented test data, by its own text or by the recorded provenance.

    Approval cannot clear it for a customer: it may sit in a review copy so a reader
    sees what the attachment would be, and every submission is blocked while it is there.
    """
    return bool(TEST_DATA.search(text or "") or TEST_DATA.search(provenance or ""))


def document_text(session: Session, ctx: ExecutionContext, document: Document) -> str:
    if document.current_version_id is None:
        return ""
    blocks = list(
        session.execute(
            select(DocumentBlock)
            .where(
                DocumentBlock.org_id == ctx.org_id,
                DocumentBlock.document_version_id == document.current_version_id,
            )
            .order_by(DocumentBlock.ordinal)
        ).scalars()
    )
    return _body(blocks)


def fixture_version_ids(session: Session, ctx: ExecutionContext) -> set:
    """Versions of company documents whose own text says they are test fixtures.

    Evidence taken from such a document is not a source for a proposal, whatever an
    "approved" line inside the document says: the system's own fields decide, and
    they refuse to approve a fixture.
    """
    out = set()
    for document in session.execute(
        select(Document).where(
            Document.org_id == ctx.org_id,
            Document.side == DocumentSide.COMPANY,
            Document.withdrawn_at.is_(None),
            Document.current_version_id.is_not(None),
        )
    ).scalars():
        if is_fixture(document_text(session, ctx, document)):
            out.add(document.current_version_id)
    return out


def superseded_version_ids(session: Session, ctx: ExecutionContext) -> set:
    """Versions of company documents that no longer speak for the company.

    A version replaced by a newer one, or any version of a withdrawn document. Their
    excerpts stay searchable history, but they must not back a claim: uploading the
    company's real document has to replace what the earlier one said.
    """
    from app.models import DocumentVersion

    out = set()
    rows = session.execute(
        select(DocumentVersion.id, Document.current_version_id, Document.withdrawn_at)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.org_id == ctx.org_id, Document.side == DocumentSide.COMPANY)
    ).all()
    for version_id, current_id, withdrawn_at in rows:
        if withdrawn_at is not None or version_id != current_id:
            out.add(version_id)
    return out


def _first_line(session: Session, ctx: ExecutionContext, document: Document) -> str:
    """The document's own heading: the first line of its first block, markdown marks removed."""
    block = session.execute(
        select(DocumentBlock.text)
        .where(
            DocumentBlock.org_id == ctx.org_id,
            DocumentBlock.document_version_id == document.current_version_id,
            DocumentBlock.text.is_not(None),
        )
        .order_by(DocumentBlock.ordinal)
        .limit(1)
    ).scalar_one_or_none()
    first = (block or "").strip().split("\n", 1)[0]
    return first.lstrip("#-* ").strip()[:160]


# What a company document IS, finer than the library's document type. A screening policy and
# a conflict statement are both filed as "legal", but they answer different clauses.
EVIDENCE_TYPES: tuple[tuple[str, str, re.Pattern[str], re.Pattern[str] | None], ...] = (
    # (type, appendix group, how the document names itself, which requirement wording it answers)
    (
        "EMPLOYEE_SCREENING_POLICY",
        "Employee screening",
        re.compile(r"employee\s+screening|background\s+check|police\s+clearance", re.I),
        re.compile(
            r"screening|background\s+checks?|criminal\s+history|police\s+clearance|personnel\s+who\s+will\s+be\s+working",
            re.I,
        ),
    ),
    (
        "CONFLICT_DECLARATION",
        "Conflict of interest declaration",
        re.compile(r"conflicts?\s+of\s+interest", re.I),
        re.compile(
            r"conflicts?\s+of\s+interest|interest\b[^.]{0,60}\bconflicts?\b|\bconflicts?\b[^.]{0,60}\binterest",
            re.I,
        ),
    ),
    (
        "SUBCONTRACTING_DECLARATION",
        "Sub-contracting declaration",
        re.compile(r"sub-?\s?contract", re.I),
        re.compile(r"sub-?\s?contract", re.I),
    ),
    (
        "RESOURCE_COMMITMENTS",
        "Other commitments",
        re.compile(r"other\s+commitments", re.I),
        re.compile(r"other\s+commitments", re.I),
    ),
    (
        "REFERENCE_LETTER",
        "Project references",
        re.compile(r"reference\s+letter|letter\s+of\s+reference|testimonial", re.I),
        re.compile(
            r"letters?\s+of\s+reference|reference\s+letters?|testimonials?|\bletters,?\s+(?:or\s+)?testimonials?",
            re.I,
        ),
    ),
    (
        "CUSTOMER_REFERENCE_TABLE",
        "Project references",
        re.compile(r"customers?\s+for\s+reference|reference\s+(?:list|table)|table\s+a9", re.I),
        re.compile(r"list\s+of\s+customers|table\s+a9|(?:current\s+)?corporate\s+references", re.I),
    ),
    # Past work: asked for as references, similar assignments or projects. A requirement that merely
    # contains the word "reference" (a tender reference, an envelope marking) is not one.
    (
        "CLIENT_REFERENCE",
        "Project references",
        re.compile(r"project\s+reference", re.I),
        re.compile(
            r"(?:client|customer|past|previous|corporate)\s+references?|references?\s+from\s+(?:past|current|clients?|customers?)"
            r"|similar\s+(?:projects?|assignments?|work)|(?:related|past|previous|recent)\s+projects?|past\s+performance",
            re.I,
        ),
    ),
    (
        "STAFF_CV",
        "Key personnel curricula vitae",
        re.compile(r"curricul|\bCVs?\b|r[eé]sum[eé]", re.I),
        re.compile(r"\bCV'?s\b|r[eé]sum[eé]s?|curricul\w+|key\s+(?:personnel|team)", re.I),
    ),
    (
        "STANDARD_TERMS",
        "Standard terms and conditions",
        re.compile(r"standard\s+terms|terms\s+and\s+conditions", re.I),
        re.compile(
            r"standard\s+terms|bidder'?s\s+terms|terms\s+(?:and|&)\s+conditions\s+of\s+(?:supply|sale|business)",
            re.I,
        ),
    ),
    (
        "PROFESSIONAL_CERTIFICATE",
        "Certifications",
        re.compile(r"certificat", re.I),
        re.compile(r"certificat\w+|accreditation|ISO\s?\d{4,5}", re.I),
    ),
)


def evidence_type(title: str, text: str = "") -> str:
    """The finer kind of a company document, by how it names itself (title first, then its
    opening lines). Empty when it is one of the library's own kinds already."""
    head = f"{title}\n{' '.join((text or '').split()[:40])}"
    for kind, _group, names_itself, _answers in EVIDENCE_TYPES:
        if names_itself.search(title or ""):
            return kind
    for kind, _group, names_itself, _answers in EVIDENCE_TYPES:
        if names_itself.search(head):
            return kind
    return ""


def type_group(kind: str) -> str:
    return next((g for k, g, _n, _a in EVIDENCE_TYPES if k == kind), "")


def answers_requirement(kind: str, requirement_text: str) -> bool:
    pattern = next((a for k, _g, _n, a in EVIDENCE_TYPES if k == kind), None)
    return bool(pattern and pattern.search(requirement_text or ""))


def selected(
    session: Session, ctx: ExecutionContext, bid_id=None, withheld: list | None = None
) -> list[Attachment]:
    """Every attachable company document, in appendix order.

    With a bid, only the kinds that bid's tender asks for (see `ASKED_FOR`). When
    `withheld` is given, each document that was considered and left out is appended to
    it as (title, appendix group, reason).
    """

    def skip(document, kind, reason):
        if withheld is not None:
            withheld.append((document.title, groups.get(kind, str(kind)), reason))

    asked = _tender_text(session, ctx, bid_id) if bid_id is not None else ""
    own_names, other_names = (
        _customers(session, ctx, bid_id) if bid_id is not None else (set(), set())
    )
    rows = session.execute(
        select(Document, DocumentClassification.document_type)
        .join(
            DocumentClassification,
            DocumentClassification.document_version_id == Document.current_version_id,
        )
        .where(
            Document.org_id == ctx.org_id,
            Document.side == DocumentSide.COMPANY,
            Document.withdrawn_at.is_(None),
        )
        .order_by(Document.title)
    ).all()
    groups = dict((kind, title) for title, kind in GROUPS)
    order = {kind: n for n, (_t, kind) in enumerate(GROUPS)}
    found: list[tuple[int, Attachment]] = []
    for document, document_type in rows:
        kind = _kind(document.title, getattr(document_type, "value", str(document_type)))
        if kind not in groups:
            continue
        if document.confidentiality_class in HELD_BACK or document.current_version_id is None:
            skip(document, kind, "restricted or privileged, never attached")
            continue
        if document.evidence_status is DocumentEvidenceStatus.REJECTED:
            skip(document, kind, "a person rejected it as evidence")
            continue  # a person turned this document down, so it is never attached
        if asked and not ASKED_FOR[kind].search(asked):
            skip(document, kind, "the tender does not ask for this kind of document")
            continue
        text = document_text(session, ctx, document)
        if not text or UNVERIFIED.search(text):
            skip(document, kind, "empty, or says its contents must be verified before reuse")
            continue
        if bid_id is not None and kind in BID_SPECIFIC:
            # Written for another customer's bid, so never sent to this one.
            haystack = f"{document.title} {text}"
            if _names_customer(haystack, other_names) and not _names_customer(haystack, own_names):
                skip(document, kind, "written for another customer's bid")
                continue
        title = _first_line(session, ctx, document) or text.split("\n", 1)[0].strip().lstrip("- ")
        title = title or document.title
        etype = evidence_type(title, text)
        found.append(
            (
                order[kind],
                Attachment(
                    document_id=document.id,
                    version_id=document.current_version_id,
                    group=type_group(etype) or groups[kind],
                    evidence_type=etype,
                    title=title,
                    body=text,
                    fixture=(
                        is_fixture(text)
                        or is_test_data(text, document.evidence_provenance)
                        or attestation_state(session, document) == "SYNTHETIC"
                    ),
                    status=document.evidence_status,
                    scope=document.evidence_use_scope,
                    provenance=document.evidence_provenance,
                    attestation=attestation_state(session, document),
                    attested_by=document.attested_by,
                    attested_at=document.attested_at,
                    attestation_method=document.attestation_method,
                    attested_version_hash=document.attested_version_hash,
                    current_version_hash=_current_hash(session, document),
                ),
            )
        )
    return [a for _n, a in sorted(found, key=lambda pair: pair[0])]


def withheld_documents(
    session: Session, ctx: ExecutionContext, bid_id
) -> list[tuple[str, str, str]]:
    """Company documents of an attachable kind that were considered and not attached."""
    out: list = []
    selected(session, ctx, bid_id, withheld=out)
    # A title that names another customer must not carry that name into this bid's files.
    _own, other = _customers(session, ctx, bid_id)

    def clean(title: str) -> str:
        for token in sorted(other, key=len, reverse=True):
            # Not \b: a file name such as 36_Signed_Jadwa_NDA joins words with underscores.
            title = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
                "[another customer]",
                title,
                flags=re.IGNORECASE,
            )
        return title

    merged: dict[tuple[str, str], list[str]] = {}
    for t, g, r in sorted(set(out)):
        reasons = merged.setdefault((clean(t), g), [])
        if r not in reasons:
            reasons.append(r)
    return [(t, g, "; ".join(rs)) for (t, g), rs in sorted(merged.items())]


def fixture_titles(session: Session, ctx: ExecutionContext, bid_id=None) -> list[str]:
    """Titles of attachable documents that declare themselves test fixtures.

    Such evidence is invented. It can sit in a review copy so a reader sees what the
    attachment would be, but it must never go to a customer as if it were fact.
    """
    return [a.title for a in selected(session, ctx, bid_id) if a.fixture]


def uncleared_titles(session: Session, ctx: ExecutionContext, bid_id=None) -> list[str]:
    """Attachable documents that are not fixtures but no person has cleared for a customer."""
    return [a.title for a in selected(session, ctx, bid_id) if not a.fixture and not a.cleared]


# --- claims that rest on fixtures -------------------------------------------------

NAMES = re.compile(r"\b[A-Z][a-z]+(?:[ -][A-Z][A-Za-z]+)+\b")
# A figure counts when it carries a thousands separator or a percent sign; bare integers,
# years and section or version numbers (768, 2020, 1.12) are not facts a document owns.
FIGURES = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|\b\d+(?:\.\d+)?%")
# Capitalised phrases that name a job or a generic thing, not a person or an organisation.
GENERIC = frozenset(
    "director manager lead officer security transport professional report test database "
    "administrator architect developer engineer portal performance standard terms "
    "conditions certified solution application server".split()
)


def facts_in(text: str) -> set[str]:
    """Names and figures a sentence asserts: 'Rania Haddad', 'Al Waha Capital', '14,200'."""
    return {m.group(0).strip(",.") for m in NAMES.finditer(text)} | {
        m.group(0).strip(",.") for m in FIGURES.finditer(text)
    }


def _shingles(text: str, size: int = 5) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def copies_fixture(text: str, shingles: frozenset[str] | set[str], minimum: int = 6) -> bool:
    """Does the text repeat runs of words from a fixture document (a paraphrase of
    its terms or record that names no distinctive person or figure)?"""
    return bool(shingles) and len(_shingles(text) & shingles) >= minimum


def fixture_shingles(session: Session, ctx: ExecutionContext, bid_id) -> set[str]:
    """Five-word runs found in fixture documents and not in the tender."""
    out: set[str] = set()
    for document in session.execute(
        select(Document).where(
            Document.org_id == ctx.org_id,
            Document.side == DocumentSide.COMPANY,
            Document.current_version_id.is_not(None),
        )
    ).scalars():
        text = document_text(session, ctx, document)
        if is_fixture(text):
            out |= _shingles(text)
    return out - _shingles(_tender_text(session, ctx, bid_id))


def fixture_only_facts(session: Session, ctx: ExecutionContext, bid_id) -> dict[str, str]:
    """Facts found in fixture documents and nowhere the company or the tender vouches for.

    Mined from every company document that says it is a test fixture, withdrawn ones
    included, so that replacing a fixture does not make the claims quoted from it look
    clean. A fact also found in the tender or in a company document that is not a
    fixture is left out: it is not invented. The result maps each fact to the fixture
    that holds it.
    """
    from app.services.tender_analysis import tender_blocks

    documents = list(
        session.execute(
            select(Document).where(
                Document.org_id == ctx.org_id,
                Document.side == DocumentSide.COMPANY,
                Document.current_version_id.is_not(None),
            )
        ).scalars()
    )
    fixture_facts: dict[str, str] = {}
    vouched = " ".join(b.text for b in tender_blocks(session, ctx, bid_id=bid_id)).casefold()
    clean: list[str] = []
    for document in documents:
        text = document_text(session, ctx, document)
        if is_fixture(text):
            for fact in facts_in(text):
                fixture_facts.setdefault(fact, document.title)
        elif document.withdrawn_at is None:
            clean.append(text.casefold())
    vouched += " " + " ".join(clean)
    return {f: t for f, t in fixture_facts.items() if f.casefold() not in vouched}


def _strong(fact: str) -> bool:
    """A figure, or a name with no job-title or generic word in it."""
    if FIGURES.fullmatch(fact):
        return True
    return not any(w.casefold() in GENERIC for w in re.split(r"[ -]", fact))


def tainted(text: str, facts: dict[str, str], minimum: int = 2) -> list[str]:
    """The fixture-only facts a statement asserts, if it asserts enough of them.

    At least `minimum` facts, and at least one of them a figure or a person's or
    organisation's name, so two tool names or job titles are not enough.
    """
    hit = sorted(f for f in facts_in(text) if f in facts)
    # A long name (a project or customer) is often written short: "Al Waha" for
    # "Al Waha Capital Client Onboarding Portal". Its first two words stand for it.
    for short in sorted({" ".join(f.split()[:2]) for f in facts if len(f.split()) >= 3}):
        if not any(w.casefold() in GENERIC for w in short.split()) and re.search(
            rf"\b{re.escape(short)}\b", text
        ):
            # Naming the customer once is one shared fact, not two: the short form of a
            # name already counted in full adds nothing unless something else is asserted.
            owners = {f for f in facts if f.startswith(short + " ")}
            if not (set(hit) <= owners) or not hit:
                return sorted({*hit, short})
    # A figure that only a fixture holds ("14,200 applications") is enough on its own.
    if any(FIGURES.fullmatch(f) for f in hit):
        return hit
    return hit if len(hit) >= minimum and any(_strong(f) for f in hit) else []


# --- what the reference documents say about themselves ------------------------------

_LETTER_GAP = re.compile(
    r"\bno\s+(?:signed\s+)?letter\b[^|\n.]{0,30}|\bletter\s+(?:requested|not\s+(?:yet\s+)?(?:held|received|signed))"
    r"|\b(?:is|are)\s+not\s+held\b",
    re.IGNORECASE,
)
_CONTACT_GAP = re.compile(
    r"\bcontact\s+(?:person\s+)?(?:not\s+(?:yet\s+)?(?:confirmed|released|provided|available)"
    r"|released\s+after|to\s+be\s+(?:confirmed|released))[^|\n.]{0,40}",
    re.IGNORECASE,
)
_NONE_RECORDED = re.compile(r"(?:[^\s.|]+[ \t]+){0,10}none[ \t]+recorded\b", re.IGNORECASE)


def reference_gaps(
    session: Session, ctx: ExecutionContext, bid_id
) -> dict[str, list[tuple[str, str]]]:
    """What the attached reference documents admit they lack, in their own words.

    Approving a reference is not evidence that it is usable: a customer list that says
    'No letter held' or 'Contact not yet confirmed' has told us the row is incomplete.
    The result maps 'letters', 'contacts' and 'none_recorded' to (document title, quoted
    wording). Wording is matched by code; nothing here is model-written.
    """
    out: dict[str, list[tuple[str, str]]] = {"letters": [], "contacts": [], "none_recorded": []}
    for attachment in selected(session, ctx, bid_id):
        if attachment.group != "Project references":
            continue
        text = attachment.body
        seen: set[str] = set()
        for key, pattern in (
            ("letters", _LETTER_GAP),
            ("contacts", _CONTACT_GAP),
            ("none_recorded", _NONE_RECORDED),
        ):
            for match in pattern.finditer(text):
                quote = " ".join(match.group(0).split()).strip(" .,;|")
                if quote and quote.casefold() not in seen:
                    seen.add(quote.casefold())
                    out[key].append((attachment.title, quote))
    return out


# --- per-requirement reference scoring -----------------------------------------------

_CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\+?\d[\d ()-]{7,}\d")
_WAF = re.compile(r"web\s+application\s+firewall|\bWAF\b", re.I)
_NO_WAF = re.compile(r"\bno\s+(?:web\s+application\s+firewall|WAF)\b", re.I)


def reference_rows(session: Session, ctx: ExecutionContext, bid_id) -> list[dict]:
    """One record per customer in the attached reference table, with what each row proves.

    A row counts toward a minimum only for what it actually shows: a contact someone can
    call, a signed letter that is held, work of the kind the tender asks about.
    """
    rows: list[dict] = []
    for a in selected(session, ctx, bid_id):
        if a.evidence_type != "CUSTOMER_REFERENCE_TABLE":
            continue
        for line in a.body.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3 or not cells[0].isdigit():
                continue
            text = " | ".join(cells[1:])
            rows.append(
                {
                    "customer": cells[1].split(",")[0].strip(),
                    "contact_complete": bool(_CONTACT.search(text))
                    and not _CONTACT_GAP.search(text),
                    "letter_held": bool(re.search(r"\bletter\s+held\b", text, re.I))
                    and not _LETTER_GAP.search(text),
                    "relevant_work": bool(_WAF.search(text)) and not _NO_WAF.search(text),
                }
            )
    return rows


def reference_supply(requirement_text: str, rows: list[dict]) -> tuple[int, str]:
    """(how many rows satisfy this requirement, what was counted)."""
    t = requirement_text or ""
    if re.search(r"letters?|testimonials?", t, re.I):
        return sum(r["letter_held"] for r in rows), "with a signed letter held"
    if re.search(
        r"similar|related|regional|firewall|\bWAF\b|stock\s+exchange|financial\s+institution",
        t,
        re.I,
    ):
        return sum(
            r["relevant_work"] for r in rows
        ), "of the kind the tender asks for (web application firewall work)"
    if re.search(r"contact|telephone|address", t, re.I):
        return sum(r["contact_complete"] for r in rows), "with a contact someone can call"
    return len(rows), "listed"


# --- the structured evidence manifest -------------------------------------------------


def evidence_manifest(session: Session, ctx: ExecutionContext, bid_id) -> list[dict]:
    """Every company document the bid considered: selected or withheld, and why.

    Rows are plain data (the CSV and JSON exports are written from this), so a reviewer
    can see which document answered which requirement without reading prose.
    """
    own, other = _customers(session, ctx, bid_id)
    mapping = evidence_mapping(session, ctx, bid_id)
    out: list[dict] = []
    for a in selected(session, ctx, bid_id):
        words = set(re.findall(r"[a-z0-9]+", f"{a.title} {a.body}".casefold()))
        codes = sorted(c for c, hits in mapping.items() if any(h["title"] == a.title for h in hits))
        incomplete = any(
            h["incomplete"] for c in codes for h in mapping[c] if h["title"] == a.title
        )
        out.append(
            {
                "document_id": str(a.document_id),
                "version_id": str(a.version_id),
                "title": a.title,
                "evidence_type": a.evidence_type or "",
                "appendix_group": a.group,
                "bid_scope": "BID_SPECIFIC" if (other & words) else "COMPANY_GLOBAL",
                "evidence_reality": a.reality,
                "fixture": bool(a.fixture),
                "content_review_status": a.status.value,
                "intended_use_scope": a.scope.value if a.scope else "",
                "attestation_valid": bool(a.attestation_valid),
                "attested_by": str(a.attested_by) if a.attested_by else "",
                "attested_at": a.attested_at.isoformat() if a.attested_at else "",
                "attestation_method": a.attestation_method or "",
                "attested_version_hash": a.attested_version_hash or "",
                "current_version_hash": a.current_version_hash or "",
                "effective_submission_status": a.submission_status,
                "included_in_review_package": True,
                "submission_eligible": a.submission_status == "ELIGIBLE",
                "rejection_reason": "",
                "completeness": "INCOMPLETE"
                if incomplete or states_incomplete(a.body)
                else "NOT_ASSESSED",
                "requirements_mapped": codes,
                "requirements_satisfied": sorted(
                    c
                    for c in codes
                    if any(h["title"] == a.title and h["role"] == "satisfies" for h in mapping[c])
                ),
            }
        )
    # Withheld documents carry their reason; a title that names another customer is redacted.
    for title, group, reason in withheld_documents(session, ctx, bid_id):
        out.append(
            {
                "document_id": "",
                "version_id": "",
                "title": title,
                "evidence_type": "",
                "appendix_group": group,
                "bid_scope": "ANOTHER_CUSTOMER" if "another customer" in reason else "",
                "evidence_reality": "",
                "fixture": False,
                "content_review_status": "",
                "intended_use_scope": "",
                "attestation_valid": False,
                "attested_by": "",
                "attested_at": "",
                "attestation_method": "",
                "attested_version_hash": "",
                "current_version_hash": "",
                "effective_submission_status": "WITHHELD",
                "included_in_review_package": False,
                "submission_eligible": False,
                "rejection_reason": reason,
                "completeness": "",
                "requirements_mapped": [],
                "requirements_satisfied": [],
            }
        )
    return out


# --- one canonical requirement-to-evidence mapping -------------------------------------

_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
_PARTIAL = re.compile(
    rf"\b{_NUM}\s+of\s+(?:the\s+)?{_NUM}\b(?!\s+(?:years|months|days|weeks))"
    r"|\bnot\s+(?:yet\s+)?(?:evidenced|completed|held|available)\b|\bbeing\s+renewed\b|\brenewal\b|\bpending\b|\bvacant\b",
    re.IGNORECASE,
)


def states_incomplete(text: str) -> bool:
    """Does the document itself say part of what it covers is not yet in place?"""
    return bool(_PARTIAL.search(text or ""))


_NO_SUBCONTRACTING = re.compile(
    r"\bno\s+sub-?\s?contractors?\b|\bnot\s+(?:propos|us|engag)\w*[^.]{0,40}sub-?\s?contract|\bperform\s+all\s+(?:the\s+)?services\b",
    re.IGNORECASE,
)
# Clauses that only apply once a sub-contractor is proposed.
_SUBCONTRACTOR_DETAILS = re.compile(
    r"(?:profiles?|references?|team\s+details|names?\s+of|identity)\b[^.]{0,80}sub-?\s?contract"
    r"|sub-?\s?contract\w*[^.]{0,80}(?:profiles?|references?|team\s+details)"
    r"|person(?:\(s\)|s)?\s+to\s+whom[^.]{0,60}sub-?\s?contract|precise\s+services[^.]{0,80}sub-?\s?contract",
    re.IGNORECASE,
)


def evidence_mapping(session: Session, ctx: ExecutionContext, bid_id) -> dict[str, list[dict]]:
    """{requirement code: attached documents that answer it}, the one table the matrix and
    the manifest both read. A document answers a requirement only by kind (see
    EVIDENCE_TYPES); a shared word is not enough.

    Each hit carries a role: 'satisfies' only when the document can go to a customer, is
    complete and meets any stated minimum; otherwise it is a 'candidate'. A clause that only
    applies if a sub-contractor is proposed is 'not_applicable' while a declaration says none is.
    """
    from app.models import Requirement, RequirementVersion

    attachments = selected(session, ctx, bid_id)
    gaps = reference_gaps(session, ctx, bid_id)
    gap_titles = {t for v in gaps.values() for t, _q in v}
    rows = reference_rows(session, ctx, bid_id)
    none_proposed = any(
        a.evidence_type == "SUBCONTRACTING_DECLARATION" and _NO_SUBCONTRACTING.search(a.body)
        for a in attachments
    )
    out: dict[str, list[dict]] = {}
    for code, text, extracted in session.execute(
        select(
            Requirement.code, RequirementVersion.requirement_text, RequirementVersion.required_count
        )
        .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
        .where(Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id)
    ).all():
        text = text or ""
        needed = extracted if extracted is not None else required_from_text(text)
        for a in attachments:
            if not (a.evidence_type and answers_requirement(a.evidence_type, text)):
                continue
            shortfall = ""
            if (
                a.evidence_type
                in ("CLIENT_REFERENCE", "CUSTOMER_REFERENCE_TABLE", "REFERENCE_LETTER")
                and needed is not None
                and rows
            ):
                have, what = reference_supply(text, rows)
                if have < int(needed):
                    shortfall = f"qualifying evidence is {have} of {int(needed)} ({what})"
            incomplete = a.title in gap_titles or states_incomplete(a.body)
            not_applicable = (
                none_proposed
                and a.evidence_type == "SUBCONTRACTING_DECLARATION"
                and bool(_SUBCONTRACTOR_DETAILS.search(text))
            )
            role = (
                "not_applicable"
                if not_applicable
                else "satisfies"
                if a.submission_status == "ELIGIBLE" and not incomplete and not shortfall
                else "candidate"
            )
            out.setdefault(code, []).append(
                {
                    "title": a.title,
                    "evidence_type": a.evidence_type,
                    "group": a.group,
                    "submission_status": a.submission_status,
                    "incomplete": incomplete,
                    "shortfall": shortfall,
                    "role": role,
                }
            )
    return out


_WORDS = {
    "one": 1,
    "a": 1,
    "an": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_AT_LEAST = re.compile(
    r"\bat\s+least\s+(?:(\d+)|(one|two|three|four|five|six|seven|eight|nine|ten))(?:\s*\((\d+)\))?",
    re.IGNORECASE,
)


def required_from_text(text: str) -> int | None:
    """The minimum a tender states in words ('at least five related projects', 'at least 1
    similar assignment'), when extraction did not record a number."""
    m = _AT_LEAST.search(text or "")
    if not m:
        return None
    # "at least 5 years of experience" is a duration, not a count of projects.
    if re.match(
        r"\s*(?:years?|months?|days?|weeks?|%|percent)", (text or "")[m.end() :], re.IGNORECASE
    ):
        return None
    if m.group(3):
        return int(m.group(3))
    if m.group(1):
        return int(m.group(1))
    return _WORDS.get((m.group(2) or "").lower())
