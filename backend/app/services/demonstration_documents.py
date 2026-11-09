"""A document that says it is not real makes none of its excerpts proof.

`evidence_markers` reads one excerpt at a time, which is right for a register
whose rows declare their own status -- "Notes: Demo value" is a fact about that
row. It is wrong for the statement this module is about.

A test pack was uploaded whose every document opened with:

    FICTIONAL TEST FIXTURE. TTS Technology Solutions LLC does not exist. The
    people, customers, certificate numbers and registrar named here are
    invented.

One excerpt per document carried that sentence and was marked unsubstantiated.
Every other excerpt from the same file was clean text about encryption, load
testing and bilingual portals, so it stayed unjudged -- and a requirement went
READY on seven of them. The disclaimer is a statement about the DOCUMENT, and
it was being read as a statement about a paragraph.

The same shape appears outside test packs, which is why this is not a fixture
workaround: vendors circulate sample proposals, specimen certificates, training
copies and "for illustration only" rate cards, and each announces itself once,
on page one. A corpus that mines page four of a specimen certificate as proof
of certification is the failure this product exists to prevent.

So the scope of the finding follows the scope of the claim. One declaration
anywhere in a document version marks every excerpt drawn from it.

## What it does not do

It never marks anything substantiated, and it never touches a row a person
judged. Same rule as `evidence_markers`: the column has three states and code
only ever writes the safe one.

It does not delete, withdraw or hide. The excerpts stay searchable and stay
attached to the requirements they match -- a demonstration document is still
the right thing to read when writing a first draft, and the drafter is told
what it is. What changes is that it cannot back a claim, and cannot make a
requirement READY.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select, update

from app.core.context import ExecutionContext
from app.core.logging import get_logger
from app.models import Document, DocumentChunk, DocumentVersion, Evidence
from app.models.enums import DocumentSide

log = get_logger("demonstration_documents")

# Bump when the patterns change, so a verdict always names the reading that
# produced it -- the same discipline as a prompt version.
MARKER_VERSION = "code:demonstration_documents@v1"

# A document declaring ITSELF not to be a real record. Deliberately demanding:
# each pattern needs the self-reference, so a security policy that discusses
# "sample data in test environments" is not caught, and neither is a project
# reference describing a demonstration it gave to a customer.
_DECLARATIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "fictional",
        re.compile(
            r"\bfictional\s+(?:test\s+)?(?:fixture|document|company|entity|example)\b"
            r"|\bthis\s+(?:document|company|entity)\s+(?:is|are)\s+fictional\b"
            r"|\bdoes\s+not\s+exist\b.{0,80}\binvented\b"
            r"|\bnot\s+a\s+record\s+of\s+any\s+real\s+(?:organisation|organization|company)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "specimen",
        re.compile(
            r"\b(?:specimen|sample|dummy|mock)\s+(?:document|certificate|proposal|"
            r"report|copy|pack)\b"
            r"|\bfor\s+(?:illustration|demonstration|training|evaluation)\s+(?:purposes\s+)?only\b"
            r"|\bnot\s+for\s+(?:use\s+in\s+)?(?:real|live|actual)\s+(?:bids?|tenders?|"
            r"proposals?|submissions?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "test_data",
        re.compile(
            r"\b(?:test|demonstration)\s+(?:fixture|corpus|pack)\b"
            r"|\bcontains?\s+(?:only\s+)?(?:dummy|fabricated|invented)\s+data\b",
            re.IGNORECASE,
        ),
    ),
)

REASON = (
    "the document states of itself that it is {kind} material, so nothing drawn "
    "from it proves anything about the company"
)
_KIND = {
    "fictional": "fictional",
    "specimen": "specimen or illustrative",
    "test_data": "test",
}


def declaration_in(text: str | None) -> str | None:
    """The kind of declaration this text carries, or None."""
    for name, pattern in _DECLARATIONS:
        if pattern.search(text or ""):
            return name
    return None


def mark_version(session, ctx: ExecutionContext, *, version_id: uuid.UUID) -> int:
    """Mark every excerpt of this version if the document declares itself unreal.

    Reads the CHUNKS, not the excerpts: the declaration is usually the first
    paragraph, and whether an excerpt happened to be drawn from it is an
    accident of chunking. Returns how many excerpts were marked.
    """
    version = session.get(DocumentVersion, version_id)
    if version is None or version.org_id != ctx.org_id:
        return 0
    document = session.get(Document, version.document_id)
    if document is None or document.side is not DocumentSide.COMPANY:
        return 0

    kind = None
    for (text,) in session.execute(
        select(DocumentChunk.text).where(
            DocumentChunk.org_id == ctx.org_id,
            DocumentChunk.document_version_id == version_id,
        )
    ).all():
        kind = declaration_in(text)
        if kind:
            break
    if not kind:
        return 0

    reason = REASON.format(kind=_KIND[kind])
    result = session.execute(
        update(Evidence)
        .where(
            Evidence.org_id == ctx.org_id,
            Evidence.source_document_version_id == version_id,
            # A person's judgement outranks this, exactly as it outranks the
            # document classifier.
            (Evidence.classified_by.is_(None))
            | (~Evidence.classified_by.startswith("human:")),
            (Evidence.is_substantiated.is_(None)) | (Evidence.is_substantiated.is_(True)),
        )
        .values(
            is_substantiated=False,
            substantiation_note=reason,
            classified_by=MARKER_VERSION,
        )
    )
    marked = int(result.rowcount or 0)
    if marked:
        session.flush()
        log.info(
            "demonstration_document_marked",
            document=document.original_filename,
            kind=kind,
            excerpts=marked,
        )
    return marked
