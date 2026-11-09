"""Checks run on the finished files of a package, not on the records behind them.

A package can be assembled from approved records and still go out wrong: another bid's
customer name carried in from a shared company document, an internal "test" label in a
title, a bracketed blank the signatory has not filled in, a commercial proposal with no
price in it, or a matrix that tells the customer "Comply" for things still waiting on
evidence. Every one of those was found by a reviewer reading the files. The records
looked fine, so the checks read the files.

These apply to the submission copy only; a review copy is allowed to show its gaps.
"""

from __future__ import annotations

import re
import uuid
import zipfile
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext

TAG = re.compile(r"<[^>]+>")
PARAGRAPH = re.compile(r"</w:p>")
TEXT_RUN = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.DOTALL)

# Words in an opportunity's customer name that identify nobody.
NAME_NOISE = frozenset(
    "the and for of al el company co llc ltd limited group holding holdings inc plc pjsc psc "
    "bank investment investments capital authority ministry department national general "
    "abu dhabi dubai sharjah saudi arabia uae emirates qatar kuwait oman bahrain test tests "
    "testing demo sample example".split()
)
INTERNAL_LABEL = re.compile(
    r"template[\s-]*test|\btest\s+fixture\b|\bfictional\b|\bsynthetic\b|\blorem\s+ipsum\b"
    r"|\bdummy\b|\bdemonstration\s+rate\s+card\b"
    r"|\bno\s+approved\s+evidence\b",
    re.IGNORECASE,
)
# The system's own working notes ("no approved evidence is held", "supplied with this tender
# pack"). They describe the review, not the proposal, and must never reach the customer.
INTERNAL_WORDING = re.compile(
    r"(?:no\s+)?approved\s+(?:company\s+)?evidence|evidence\s+(?:available|supplied|sources?|pack"
    r"|on\s+file)"
    r"|(?:has|have|is|was|been)\s+(?:not\s+)?(?:been\s+)?(?:held|supplied|provided)\s+for\s+this"
    r"|supplied\s+with\s+this\s+(?:task|response|tender\s+pack)|\bthis\s+draft\b|\bthe\s+draft\b"
    r"|\bknowledge\s+base\b|\bevidence\s+records?\b|\binternal\s+(?:drafting|review)\s+note\b"
    r"|\bat\s+this\s+stage\b[^.]{0,60}\bevidence\b",
    re.IGNORECASE,
)
# Capitalised words that are headings or stamps in any tender, not names.
HEADING_WORDS = frozenset(
    "OPEN COPY ORIGINAL NOTE NONE PART ANNEX TABLE SCOPE TOTAL ITEM UNIT PRICE DATE NAME TITLE "
    "FORM TENDER EVALUATION COMMITTEE EXCEPT PRESENCE WARNING DOCUMENT ENVELOPE TECHNICAL "
    "PROPOSAL COMMERCIAL SEALED STAMPED CONFIDENTIAL REQUIRED OPTIONAL MANDATORY".split()
)
ACRONYM = re.compile(r"\b[A-Z]{4,6}\b")
BLANK = re.compile(r"\[[^\]\n]*[A-Za-z][^\]\n]*\]")
MONEY = re.compile(
    r"(?:AED|USD|SAR|EUR|GBP|QAR|KWD|OMR|BHD|\$)\s?\d[\d,]{2,}|\d[\d,]{3,}(?:\.\d+)?\s?(?:AED|USD"
    r"|SAR|EUR|GBP)",
    re.IGNORECASE,
)
# A stated total or price, not just an amount that happens to appear (a bond percentage, a
# year, a figure quoted from the tender).
TOTAL_PRICE = re.compile(
    r"(?:grand\s+total|total\s+(?:price|cost|amount|fee|bid)?|offer\s+price|bid\s+price"
    r"|lump[\s-]*sum)[^\n]{0,80}?" + MONEY.pattern,
    re.IGNORECASE,
)
UNFINISHED_FIELD = re.compile(
    r"\.{6,}|…{2,}|_{5,}|\[\s*\.{2,}\s*\]|\bname\s+of\s+rfp\b", re.IGNORECASE
)
TENDER_UNFINISHED_AT = 4

# Deliverables that are physical or logistical (copies, media, envelopes, notices) rather than
# the substance of the bid. An authorised exception is conceivable for these; never for the
# technical and commercial documents, the signed letters, the CVs or the references.
OPERATIONAL_DELIVERABLE = re.compile(
    r"soft[\s-]*cop|\bCDs?\b|\bDVDs?\b|\bUSB\b|envelope|sealed|stamped|\bcopies\b|"
    r"pages\s+numbered|loosely\s+bound|notice\s+of\s+intention|hard[\s-]*cop",
    re.IGNORECASE,
)

# Marks the system leaves in a review copy where something is still to come.
REVIEW_ANNOTATION = re.compile(
    r"\[(?:provided\s+by\s+a\s+contributor|no\s+approved\s+answer[^\]]*|to\s+be\s+(?:completed"
    r"|confirmed)[^\]]*)\]",
    re.IGNORECASE,
)


def is_operational_deliverable(name: str) -> bool:
    return bool(OPERATIONAL_DELIVERABLE.search(name or ""))


# Things the customer asks for that exist only once the contract does. A bid answers them with
# a commitment; the thing itself is not a document to have in hand when the bid goes out.
POST_AWARD_DELIVERABLE = re.compile(
    r"(?:performance|advance[\s-]*payment|warranty)\s+bond|inception\s+report|post[\s-]*award|"
    r"after\s+(?:the\s+)?(?:award|contract)|hand[\s-]*over|as[\s-]*built|acceptance\s+(?:test"
    r"|report|certificate)|"
    r"progress\s+report|final\s+report|vendor\s+representatives?|go[\s-]*live\s+report|"
    r"training\s+(?:material|completion)|knowledge\s+transfer\s+(?:plan|report)",
    re.IGNORECASE,
)


# Needed in the first days of a tender, long before the documents are: the notice that the
# bidder intends to respond, a request to attend a briefing, a deadline for questions.
PRE_BID_ADMIN = re.compile(
    r"notice\s+of\s+intention|intention\s+to\s+(?:respond|bid|submit)|intent\s+to\s+(?:respond"
    r"|bid)|"
    r"confirm\w*\s+(?:of\s+)?(?:your\s+)?participation|register\s+(?:your\s+)?interest",
    re.IGNORECASE,
)

# The bid itself contains these even when the thing they describe happens after award.
BID_SUBMISSION_CUE = re.compile(
    r"\bsubmit\b|\binclude[sd]?\s+(?:in|with)\s+(?:the\s+|your\s+)?(?:proposal|bid|offer|response)|"
    r"\b(?:proposal|bid)\s+(?:shall|must|should)\s+(?:contain|include)\b|\bwell[\s-]organi[sz]ed\b",
    re.IGNORECASE,
)


# When the clause itself fixes the delivery in time after award, "submit" names the later act,
# not a document that must be inside the bid.
POST_AWARD_TIMING = re.compile(
    r"\bwithin\s+(?:\w+\s+){0,2}(?:\(\d+\)\s+)?(?:days?|weeks?|months?)\s+(?:of|from"
    r"|after)\s+(?:the\s+)?"
    r"(?:award|contract|signing|signature|notification|commencement|effective\s+date"
    r"|purchase\s+order|"
    r"start|kick[\s-]*off)\b|\b(?:after|upon)\s+(?:the\s+)?(?:contract\s+)?(?:award\w*|signing"
    r"|signature)\b",
    re.IGNORECASE,
)
STRONG_BID_CUE = re.compile(
    r"\binclude[sd]?\s+(?:in|with)\s+(?:the\s+|your\s+)?(?:proposal|bid|offer|response)|"
    r"\b(?:proposal|bid)\s+(?:shall|must|should)\s+(?:contain|include)\b|\bwell[\s-]organi[sz]ed\b|"
    r"\bwith\s+(?:the\s+|your\s+)?(?:proposal|bid|offer)\b",
    re.IGNORECASE,
)


def stage_of(text: str) -> str:
    """One decision, used for deliverables and for requirements alike.

    PRE_BID (early administration), BID (must exist to submit), DISPATCH (physical packing at
    the last step) or POST_AWARD (answered by a commitment now, delivered after award).
    """
    t = text or ""
    if PRE_BID_ADMIN.search(t):
        return "PRE_BID"
    if POST_AWARD_DELIVERABLE.search(t) and not BID_SUBMISSION_CUE.search(t):
        return "POST_AWARD"
    if (
        POST_AWARD_DELIVERABLE.search(t)
        and POST_AWARD_TIMING.search(t)
        and not STRONG_BID_CUE.search(t)
    ):
        return "POST_AWARD"
    if OPERATIONAL_DELIVERABLE.search(t):
        return "DISPATCH"
    return "BID"


def deliverable_stage(name: str) -> str:
    return stage_of(name)


# What the customer sees against a requirement, by what is still outstanding.
UNVERIFIED_STATUS = (
    "Open",
    "Present in text; evidence not verified",
    "Evidence attached; sufficiency not verified",
    "Evidence attached, incomplete (see the reference gaps in validation)",
    "Evidence attached, but it is test data and cannot go to a customer",
    "Missing before submission",
    "Evidence attached",
    "Missing before submission: signed copy",
    "Pending company confirmation",
    "Clarification required",
    "Not yet answerable",
)


def docx_text(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 - not a readable docx; other checks report it
        return ""

    # A table row is one line, cells joined by " | ": a fact in one cell stays tied to the
    # other cells of its row (a review that reads cell by cell loses whose project it is).
    def _row(match: re.Match) -> str:
        cells = []
        for cell in re.findall(r"<w:tc[ >].*?</w:tc>", match.group(0), re.DOTALL):
            cells.append(
                " ".join(
                    TAG.sub("", "".join(TEXT_RUN.findall(c))) for c in PARAGRAPH.split(cell)
                ).strip()
            )
        return "<w:p><w:r><w:t>" + " | ".join(c for c in cells if c) + "</w:t></w:r></w:p>"

    xml = re.sub(r"<w:tr[ >].*?</w:tr>", _row, xml, flags=re.DOTALL)
    paragraphs = []
    for chunk in PARAGRAPH.split(xml):
        runs = TEXT_RUN.findall(chunk)
        if runs:
            paragraphs.append(TAG.sub("", "".join(runs)))
    return "\n".join(paragraphs)


def _tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", (text or "").casefold())
        if len(t) >= 4 and t not in NAME_NOISE and not t.isdigit()
    }


def bid_name_sets(
    session: Session, ctx: ExecutionContext, bid_id: uuid.UUID
) -> tuple[set[str], set[str]]:
    """(words naming this bid's customer or reference, words naming any other bid's)."""
    from app.models import Bid, Opportunity

    own: set[str] = set()
    other: set[str] = set()
    rows = session.execute(
        select(Bid.id, Opportunity.customer_name, Opportunity.tender_reference)
        .join(Opportunity, Opportunity.id == Bid.opportunity_id)
        .where(Bid.org_id == ctx.org_id)
    ).all()
    for b_id, customer, reference in rows:
        bag = _tokens(customer) | _tokens(reference)
        (own if b_id == bid_id else other).update(bag)
    # A customer the company has an opportunity with counts even when no bid was opened
    # for it yet: a document written for that customer's proposal is still another
    # customer's document.
    for customer, reference in session.execute(
        select(Opportunity.customer_name, Opportunity.tender_reference).where(
            Opportunity.org_id == ctx.org_id
        )
    ).all():
        other.update(_tokens(customer) | _tokens(reference))
    return own, other - own


def other_bid_names(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> set[str]:
    """Words that name another bid's customer or reference, and not this one's."""
    return bid_name_sets(session, ctx, bid_id)[1]


def written_for_another_bid(
    session: Session, ctx: ExecutionContext, bid_id: uuid.UUID, text: str
) -> bool:
    """Does this company record name another bid's customer and not this bid's?

    A reference number, a signing record or a CV pack written for one customer's bid
    is not a fact about the next customer's bid, even though it sits in the same
    company library.
    """
    own, other = bid_name_sets(session, ctx, bid_id)
    words = set(re.findall(r"[a-z0-9]+", (text or "").casefold()))
    return bool(other & words) and not (own & words)


def lint(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    versions,
    storage,
    finding,
) -> None:
    """Raise findings for the submission files in `versions` (an iterable of version rows)."""
    from app.models import GeneratedDocument

    foreign = other_bid_names(session, ctx, bid_id)
    collected: list[str] = []
    for version in versions:
        mode = getattr(
            getattr(version, "output_mode", None), "value", getattr(version, "output_mode", "")
        )
        is_submission = str(mode) == "SUBMISSION"
        try:
            text = docx_text(storage.get(version.storage_key))
        except Exception:  # noqa: BLE001, S112 - reported as corrupted elsewhere
            continue
        if not text:
            continue
        collected.append(text)
        document = session.get(GeneratedDocument, version.generated_document_id)
        name = getattr(document, "name", "") or "A document"
        words = set(re.findall(r"[a-z0-9]+", text.casefold()))
        hit = sorted(foreign & words)
        if hit:
            finding(
                session,
                ctx,
                code="CROSS_BID_CONTENT",
                message=f"'{name}' mentions another bid ({', '.join(hit[:4])}). "
                "A document written for one customer must not reach another.",
                entity_type="generated_document_version",
                entity_id=version.id,
            )
        lines = [ln.strip() for ln in text.split("\n") if INTERNAL_WORDING.search(ln)]
        if lines:
            finding(
                session,
                ctx,
                code="INTERNAL_WORDING",
                message=(
                    f"'{name}' contains the system's own review wording in {len(lines)} place(s), "
                    f"for example: {lines[0][:140]!r}"
                ),
                entity_type="generated_document_version",
                entity_id=version.id,
            )
        marks = REVIEW_ANNOTATION.findall(text)
        if marks:
            finding(
                session,
                ctx,
                code="REVIEW_ANNOTATION",
                message=(
                    f"'{name}' carries {len(marks)} review annotation(s) such as {marks[0]!r}. "
                    "A review copy may show them; a submission copy cannot be built while any "
                    "remain."
                ),
                entity_type="generated_document_version",
                entity_id=version.id,
            )
        if not is_submission:
            # A review copy shows its gaps on purpose; only the checks that matter on any
            # copy (another bid's content, the system's own working notes) apply.
            continue
        label = INTERNAL_LABEL.search(text)
        if label:
            finding(
                session,
                ctx,
                code="TEST_LABEL_PRESENT",
                message=f"'{name}' carries an internal label ({label.group(0)!r}) that must not "
                f"reach a customer.",
                entity_type="generated_document_version",
                entity_id=version.id,
            )
        blanks = BLANK.findall(text)
        if blanks:
            finding(
                session,
                ctx,
                code="PLACEHOLDER_PRESENT",
                message=f"'{name}' still has {len(blanks)} blank(s) to fill in, for example "
                f"{blanks[0]!r}.",
                entity_type="generated_document_version",
                entity_id=version.id,
            )
        lowered = name.casefold()
        if (
            "commercial proposal" in lowered or "financial proposal" in lowered
        ) and not MONEY.search(text):
            finding(
                session,
                ctx,
                code="COMMERCIAL_WITHOUT_PRICE",
                message=f"'{name}' contains no price. A commercial proposal without a price "
                f"cannot be submitted.",
                entity_type="generated_document_version",
                entity_id=version.id,
            )
        elif (
            "commercial proposal" in lowered or "financial proposal" in lowered
        ) and not TOTAL_PRICE.search(text):
            finding(
                session,
                ctx,
                code="COMMERCIAL_TOTAL_MISSING",
                message=f"'{name}' states no total price next to an amount. Confirm the offer "
                f"price is stated.",
                entity_type="generated_document_version",
                entity_id=version.id,
            )


def tender_unfinished(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> int:
    """How many blocks of the source tender are blank fields, dotted lines or template titles."""
    from app.services.tender_analysis import tender_blocks

    return sum(
        1
        for b in tender_blocks(session, ctx, bid_id=bid_id)
        if UNFINISHED_FIELD.search(b.text or "")
    )


def matrix_unverified(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID) -> int:
    """Requirements the matrix would show as answered while evidence, a signature, a
    confirmation or the customer's input is still outstanding."""
    from app.services import report_compliance

    rows = report_compliance.matrix_rows(session, ctx, bid_id=bid_id)
    return sum(1 for _r, _t, status, _w in rows if status.startswith(UNVERIFIED_STATUS))


def template_residue(
    session: Session, ctx: ExecutionContext, bid_id: uuid.UUID, texts: list[str]
) -> list[str]:
    """Acronyms that the tender uses once or twice, never in lower case and never as part of
    the customer's or tender's name, and that the proposal then repeats.

    A tender built from another project's template (an ADX tender that still mentions
    ADSIC and an SSCP project) leaves such names behind. Repeating them as if they were
    this customer's is how a proposal ends up answering a different tender.
    """
    from app.models import Bid, Opportunity
    from app.services.tender_analysis import tender_blocks

    bid = session.get(Bid, bid_id)
    opportunity = session.get(Opportunity, bid.opportunity_id) if bid else None
    own = " ".join(
        filter(
            None,
            [getattr(opportunity, "customer_name", ""), getattr(opportunity, "tender_name", "")],
        )
    )
    own_words = {w.upper() for w in re.findall(r"[A-Za-z]+", own)}
    tender = " ".join(b.text or "" for b in tender_blocks(session, ctx, bid_id=bid_id))
    lower = set(re.findall(r"\b[a-z]{4,6}\b", tender))
    counts: dict[str, int] = {}
    for m in ACRONYM.findall(tender):
        counts[m] = counts.get(m, 0) + 1
    rare = {
        a
        for a, n in counts.items()
        if n <= 3 and a.lower() not in lower and a not in own_words and a not in HEADING_WORDS
    }
    blob = " ".join(texts)
    return sorted(a for a in rare if re.search(rf"\b{a}\b", blob))
