"""Tender facts, ambiguity analysis, deliverables, and the Scope Confidence gate.

Every function here reads TENDER content only. The gate at the end measures how
well we understand what the customer asked for — never how well we could answer
it.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import StateConflict, ValidationError
from app.core.logging import get_logger
from app.gateway.gateway import ModelGateway, build_gateway
from app.gateway.registry import get as get_call_type
from app.gateway.registry import wrap_untrusted
from app.models import (
    Deliverable,
    DeliverableRequirement,
    Document,
    DocumentBlock,
    DocumentVersion,
    Opportunity,
    ScopeConfidence,
)
from app.models.enums import (
    BidState,
    ClarificationIssueType,
    ClarificationSeverity,
    DeliverableStatus,
    DocumentSide,
    DocumentVersionState,
    RequirementApplicability,
    ScopeConfidenceLevel,
)
from app.schemas.tender_extraction import (
    AmbiguityAnalysisResult,
    DeliverableExtractionResult,
    PrecedenceHintResult,
    TenderFactsResult,
)
from app.services import audit, bids, clarifications, precedence, requirements

log = get_logger("tender_analysis")

# The model is told to report at most MAX_ISSUES_PER_BATCH issues, "most serious
# first", so a batch is also a ceiling on how much of the document's trouble one
# call can describe. At 25 blocks and 20,000 characters the live tender fitted
# into TWO batches, which made 24 the largest number of findings any single run
# could produce -- and 24 is what it produced, twice over, exactly at the cap.
#
# That ceiling was invisible until a rule was written that treated "this run did
# not mention it" as "this run no longer says it". It retired 44 real questions,
# the submission deadline among them, because the model had run out of room
# rather than changed its mind.
#
# Smaller batches give every part of the document its own allowance. The cost is
# more calls, and a narrower window for the contradiction types, which need both
# conflicting blocks in view -- `document_outline` still ships with every batch,
# so a disagreement with something out of view is still reportable as
# INCOMPLETE_SCOPE or UNDEFINED_FIELD, which is what the prompt already says.
MAX_BATCH_BLOCKS = 10
MAX_BATCH_CHARACTERS = 8_000

# Must match the number in the ambiguity prompt. A batch that returns exactly
# this many issues was truncated: the model stopped because it was told to, not
# because it had finished, so silence about anything else in that batch means
# nothing. `analyze_ambiguity` uses this to decide which blocks a run is
# entitled to draw conclusions about.
MAX_ISSUES_PER_BATCH = 12

# Scope Confidence thresholds. Deliberately explicit constants rather than
# magic numbers buried in the function: this gate decides whether the product
# will let anyone draft a solution.
HIGH_COVERAGE = 0.95
MEDIUM_COVERAGE = 0.80
HIGH_MAX_MAJOR_ISSUES = 2
MEDIUM_MAX_MAJOR_ISSUES = 8


def tender_blocks(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> list[DocumentBlock]:
    """All indexed tender-side blocks for a bid.

    The ``side == TENDER`` predicate is the authority boundary in SQL: a company
    document in the same organization is not a candidate, so it cannot influence
    any tender-side extraction.
    """
    return list(
        session.execute(
            select(DocumentBlock)
            .join(DocumentVersion, DocumentVersion.id == DocumentBlock.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentBlock.org_id == ctx.org_id,
                Document.bid_id == bid_id,
                Document.side == DocumentSide.TENDER,
                Document.withdrawn_at.is_(None),
                DocumentVersion.state.in_(
                    [DocumentVersionState.INDEXED, DocumentVersionState.ACTIVE]
                ),
            )
            .order_by(DocumentBlock.ordinal)
        )
        .scalars()
        .all()
    )


def _batches(blocks: list[DocumentBlock]):
    batch: list[DocumentBlock] = []
    characters = 0
    for block in blocks:
        if batch and (
            len(batch) >= MAX_BATCH_BLOCKS or characters + len(block.text) > MAX_BATCH_CHARACTERS
        ):
            yield batch
            batch, characters = [], 0
        batch.append(block)
        characters += len(block.text)
    if batch:
        yield batch


def _outline(blocks: list[DocumentBlock]) -> list[dict]:
    """Every section in the document, in order, with where each one starts.

    Sent with EVERY batch, and it is the fix for the worst class of false
    positive this analysis produced. The model sees fifteen blocks at a time. It
    also sees the table of contents, notices that "Technical Requirements" is
    listed there and not in front of it, and reports the section as absent --
    faithfully, because the prompt used to invite exactly that. Six of eight
    BLOCKING clarifications on the live bid were of this kind, including
    "Technical Requirements ... not present in the provided text" on a document
    that yielded sixteen technical requirements.

    With the outline present, "not in my window" and "not in the document" are
    distinguishable, which they were not before.
    """
    seen: dict[tuple, int] = {}
    for block in blocks:
        key = tuple(h for h in (block.heading_path or []) if h)
        if key and key not in seen:
            seen[key] = block.ordinal
    return [
        {"section": " > ".join(key), "starts_at_ordinal": ordinal}
        for key, ordinal in sorted(seen.items(), key=lambda item: item[1])
    ]


# --- absence claims that the document itself refutes ------------------------

# The two issue types that assert something is not there. Everything else is a
# statement about text the model can see, and is checkable by a reader.
ABSENCE_TYPES = (ClarificationIssueType.BLANK, ClarificationIssueType.INCOMPLETE_SCOPE)

# The wording that turns a description into a claim of absence. Kept narrow on
# purpose: "the deadline is not stated anywhere" is an absence claim, "the two
# deadlines do not agree" is not.
_ABSENCE_WORDING = re.compile(
    r"\b(?:not\s+(?:present|provided|included|given|visible|attached|supplied|shown|"
    r"found|listed|stated|specified|defined|detailed|described|available)"
    r"|(?:is|are|was|were)\s+missing|missing\s+from|absent|omitted|no\s+such\s+section"
    r"|nowhere\s+in)\b",
    re.IGNORECASE,
)

# What the claim is ABOUT. Three shapes, all of them the model's own words:
# quoted names, the members of an "(e.g. A, B, C)" list, and Title Case runs.
# Nothing is inferred -- a term only counts if the description named it.
_QUOTED = re.compile(r"[\"“‘']([A-Za-z][^\"”’']{2,79})[\"”’']")
_EG_LIST = re.compile(r"\((?:e\.g\.|eg|i\.e\.|such as)[,:]?\s*([^)]{3,400})\)", re.IGNORECASE)
# "&" joins a title without breaking it: "Submission Guidelines & Requirements"
# is one name, and half of it is a different claim.
_TITLE_RUN = re.compile(r"\b[A-Z][\w/+-]*(?:\s+(?:[A-Z][\w/+-]*|&)){1,5}")

# Words that make a Title Case run a sentence opener or a piece of our own
# vocabulary rather than a name in the tender.
_NOT_A_SUBJECT = {
    "the",
    "this",
    "these",
    "those",
    "there",
    "it",
    "they",
    "however",
    "but",
    "and",
    "section",
    "sections",
    "document",
    "tender",
    "rfp",
    "note",
    "raised",
    "while",
    "reading",
}


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _absence_subjects(description: str) -> list[str]:
    """The named things a description says are missing, in its own words."""
    subjects: list[str] = []

    def add(raw: str) -> None:
        term = _normalize(raw).strip(" .,:;-–—&")
        # One word is not a subject: "Email" or "SSL" matches half a document by
        # accident, and dropping a real finding on that would be worse than
        # keeping a false one.
        if len(term.split()) < 2 or len(term) < 6:
            return
        if term.split()[0] in _NOT_A_SUBJECT:
            term = " ".join(term.split()[1:])
            if len(term.split()) < 2:
                return
        if term not in subjects:
            subjects.append(term)

    for match in _EG_LIST.finditer(description):
        for part in re.split(r",|;|\band\b", match.group(1)):
            add(part)
    for match in _QUOTED.finditer(description):
        add(match.group(1))
    for match in _TITLE_RUN.finditer(description):
        add(match.group(0))
    return subjects


def _absence_is_refuted(
    description: str,
    *,
    unseen_text: str,
) -> tuple[bool, list[str]]:
    """Does the rest of the tender contain what this finding says is missing?

    The model is shown ten blocks at a time. When it reports that a section or
    a list of services "is not present in the visible text", it is describing
    its window, not the document -- and on the live tender it did exactly that
    for material sitting a few pages further on. The severity guard already
    stops such a claim from blocking generation; this drops it, because a
    question the document answers is not a question to put to the customer.

    Only text the call could NOT see counts as a refutation. If the term is in
    the same batch, the model read it and meant something else -- that finding
    stands.
    """
    if not _ABSENCE_WORDING.search(description or ""):
        return False, []
    subjects = _absence_subjects(description or "")
    if not subjects:
        return False, []
    found = [s for s in subjects if s in unseen_text]
    # One named subject must be found outright; a list is refuted when half of
    # what it names is elsewhere in the document.
    enough = len(found) == len(subjects) if len(subjects) == 1 else len(found) * 2 >= len(subjects)
    return (bool(found) and enough), found


def _payload(
    blocks: list[DocumentBlock],
    outline: list[dict] | None = None,
    requirements_by_ordinal: dict[int, list[dict]] | None = None,
    requirements_in_document: list[dict] | None = None,
) -> str:
    def entry(b: DocumentBlock) -> dict:
        item: dict[str, Any] = {
            "ordinal": b.ordinal,
            "block_type": b.block_type.value,
            "clause_ref": b.clause_ref,
            "heading_path": b.heading_path or [],
            "table_name": b.table_name,
            "page_from": b.page_from,
            "text": b.text,
        }
        # Only for the calls that must say WHICH requirement they mean; every
        # other caller's payload is byte-for-byte what it was, so their cached
        # answers stay valid.
        if requirements_by_ordinal is not None:
            item["requirements"] = requirements_by_ordinal.get(b.ordinal, [])
        return item

    body = {
        "document_outline": outline or [],
        "visible_ordinals": [b.ordinal for b in blocks],
        "blocks": [entry(b) for b in blocks],
    }
    # What has already been read out of the WHOLE document, not just this
    # window. `document_outline` says which sections exist; this says what was
    # found in them, which is what an absence claim is actually about. Added
    # only for the caller that needs it, so every other caller's payload is
    # byte-for-byte what it was and their cached answers stay valid.
    if requirements_in_document is not None:
        body["requirements_already_extracted"] = requirements_in_document

    return wrap_untrusted(
        json.dumps(
            body,
            ensure_ascii=False,
            default=str,
        )
    )


def _locator(block: DocumentBlock) -> dict:
    from app.schemas.source_locator import SourceLocator

    return SourceLocator(
        document_version_id=block.document_version_id,
        heading_path=list(block.heading_path or []),
        clause_ref=block.clause_ref,
        table_name=block.table_name,
        page_from=block.page_from,
        page_to=block.page_to,
        char_start=block.char_start,
        char_end=block.char_end,
    ).as_json()


# --- precedence hints -------------------------------------------------------


def extract_precedence_hints(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> list[str]:
    """Read the tender's own statement about which document prevails."""
    blocks = tender_blocks(session, ctx, bid_id=bid_id)
    if not blocks:
        return []
    model_gateway = gateway or build_gateway()
    priority: list[str] = []
    for batch in _batches(blocks):
        result = model_gateway.generate_structured(
            "precedence_hint_extraction", _payload(batch), ctx=ctx, session=session
        )
        if not isinstance(result, PrecedenceHintResult):
            raise ValidationError("precedence_hint_extraction returned an unexpected schema.")
        for hint in result.hints:
            for document_type in hint.priority_document_types:
                token = document_type.strip().upper()
                if token and token not in priority:
                    priority.append(token)
    return priority


# --- tender facts -----------------------------------------------------------


def extract_tender_facts(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> dict:
    blocks = tender_blocks(session, ctx, bid_id=bid_id)
    if not blocks:
        return {}
    model_gateway = gateway or build_gateway()

    merged = TenderFactsResult()
    for batch in _batches(blocks):
        result = model_gateway.generate_structured(
            "tender_fact_extraction", _payload(batch), ctx=ctx, session=session
        )
        if not isinstance(result, TenderFactsResult):
            raise ValidationError("tender_fact_extraction returned an unexpected schema.")
        # First non-null wins for scalars; lists accumulate. Later batches
        # cannot quietly overwrite a fact an earlier one established.
        for scalar in (
            "customer_name",
            "tender_title",
            "tender_reference",
            "submission_deadline",
            "currency",
        ):
            if getattr(merged, scalar) is None:
                setattr(merged, scalar, getattr(result, scalar))
        merged.contacts.extend(result.contacts)
        merged.evaluation_criteria.extend(result.evaluation_criteria)
        merged.submission_rules.extend(result.submission_rules)

    facts = merged.model_dump(mode="json", exclude_none=True)
    bid = bids.get_bid(session, ctx, bid_id=bid_id)
    bid.tender_facts = facts
    bid.submission_instructions = {"rules": facts.get("submission_rules", [])}
    session.flush()

    audit.record(
        session,
        ctx,
        action="tender.facts_extracted",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        after={
            "contacts": len(merged.contacts),
            "evaluation_criteria": len(merged.evaluation_criteria),
            "submission_rules": len(merged.submission_rules),
            "has_deadline": merged.submission_deadline is not None,
        },
    )
    return facts


# --- ambiguity --------------------------------------------------------------


class AmbiguityRun(int):
    """How many issues were raised, and how many batches stopped at the cap.

    An `int` so every caller that reads it as a count still works. The cap
    matters because a batch that returns the maximum it was allowed stopped
    because it was told to: what it did not mention may be issue thirteen, and
    a tender dense enough to cap every batch has been read partially. Scope
    confidence must not be graded HIGH on a partial reading.
    """

    capped_batches: int

    def __new__(cls, issues: int, capped_batches: int = 0) -> AmbiguityRun:
        value = super().__new__(cls, issues)
        value.capped_batches = capped_batches
        return value


def analyze_ambiguity(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> AmbiguityRun:
    blocks = tender_blocks(session, ctx, bid_id=bid_id)
    if not blocks:
        return AmbiguityRun(0, 0)
    model_gateway = gateway or build_gateway()
    call_type = get_call_type("ambiguity_analysis")
    detected_by = f"llm:{call_type.name}@{call_type.prompt_version}"
    # Retire what an earlier prompt version said before saying anything new.
    #
    # Without this, changing a prompt leaves its old findings open for ever
    # under the old `detected_by`. When v1's blockers survived the move to v2 --
    # eight of them, still claiming the tender had no technical requirements --
    # the export showed "8 blocking" unchanged and the fix read as ineffective.
    # The old run's answers are not evidence about the document; they are
    # evidence about a question the system has stopped asking.
    clarifications.close_superseded_detector_versions(
        session,
        ctx,
        bid_id=bid_id,
        detector=f"llm:{call_type.name}",
        current=detected_by,
        reason=(
            "Raised by an earlier version of this detector; superseded by "
            f"{call_type.prompt_version}."
        ),
    )
    by_ordinal = {b.ordinal: b for b in blocks}
    raised = 0
    capped_batches = 0

    outline = _outline(blocks)
    batches = list(_batches(blocks))
    # Whether any single call saw the whole document. It decides what a batch is
    # allowed to assert, below.
    whole_document = len(batches) == 1

    # What this run said, and which blocks it actually read. Both are needed to
    # retire what it no longer says without touching findings about pages it
    # never opened -- `covered` is filled per batch, after the call returns, so
    # a run that fails partway through closes nothing about the rest.
    seen_fingerprints: set[str] = set()
    covered_blocks: set[tuple] = set()

    narrowed = widened = 0
    # Normalized text per block, so "what this call could not see" is one join
    # away for every batch. Built once: it is read for every absence claim.
    # Every requirement already drawn from this tender, sent with every batch.
    #
    # The outline says which sections exist; this says what was FOUND in them,
    # and that is what an absence claim is really about. "The evaluation
    # criteria are not stated" survived the outline rule because the model was
    # reasoning about content, not headings -- and R-073 lists all seven
    # criteria. With this in view the same finding becomes the narrower and
    # correct one: the criteria are there, their WEIGHTS are not.
    extracted = _extracted_requirements(session, ctx, bid_id=bid_id)
    # Code -> id for every live requirement, so a finding that NAMES its
    # requirements in prose can be linked even when the block it cites
    # produced none of them. See the fallback below.
    code_index = _requirement_code_index(session, ctx, bid_id=bid_id)
    linked_by_name = 0
    text_by_ordinal = {b.ordinal: _normalize(b.text) for b in blocks}
    refuted = 0
    miscited = 0
    for batch in batches:
        in_batch = {b.ordinal for b in batch}
        unseen_text = " \n".join(
            text for ordinal, text in text_by_ordinal.items() if ordinal not in in_batch
        )
        # What each block has already been turned into, so an issue can say
        # which of them it is about. Code and a short text: enough to tell
        # "develop in C#" from "third-party controls must be licensed".
        shown = {
            b.ordinal: [
                {"code": requirement.code, "text": " ".join((text or "").split())[:240]}
                for requirement, text in _requirement_rows_of(session, ctx, bid_id=bid_id, block=b)
            ]
            for b in batch
        }
        result = model_gateway.generate_structured(
            "ambiguity_analysis",
            _payload(
                batch,
                outline,
                requirements_by_ordinal=shown,
                requirements_in_document=extracted,
            ),
            ctx=ctx,
            session=session,
        )
        if not isinstance(result, AmbiguityAnalysisResult):
            raise ValidationError("ambiguity_analysis returned an unexpected schema.")
        allowed = {b.ordinal for b in batch}
        # A truncated batch is not evidence about the blocks it did not reach.
        #
        # This is the same rule the severity downgrade below already applies to
        # BLANK and INCOMPLETE_SCOPE -- a partial view cannot establish an
        # absence -- and it is needed here for the same reason. When a call
        # returns the maximum it was allowed, the model stopped because it was
        # told to. Anything it did not mention may simply be issue thirteen.
        #
        # Without this the retirement pass read "not mentioned" as "withdrawn"
        # and closed 44 real questions on the live bid, including the submission
        # deadline, because both of that document's two batches came back
        # exactly at the cap.
        if len(result.issues) < MAX_ISSUES_PER_BATCH:
            covered_blocks.update((b.document_version_id, b.char_start, b.char_end) for b in batch)
        else:
            capped_batches += 1
            log.info(
                "ambiguity_batch_capped",
                bid_id=str(bid_id),
                blocks=len(batch),
                issues=len(result.issues),
            )

        for issue in result.issues:
            # A citation outside the batch is the model's mistake, not the
            # run's end.
            #
            # This used to raise, and one stray ordinal then threw away every
            # finding in every batch -- the live run died on "cited block
            # ordinals outside its batch: 3" after reading the whole document.
            # The rule it was enforcing is still right: a finding is anchored
            # to text the call actually read, and nothing here will invent an
            # anchor. So the finding is dropped, or trimmed to the side that
            # was in view, and the rest of the run stands.
            if issue.source_block_ordinal not in allowed:
                miscited += 1
                log.info(
                    "ambiguity_issue_miscited",
                    bid_id=str(bid_id),
                    ordinal=issue.source_block_ordinal,
                    issue_type=issue.issue_type.value,
                )
                continue
            out_of_view = (
                issue.conflicting_block_ordinal is not None
                and issue.conflicting_block_ordinal not in allowed
            )
            if out_of_view:
                # The second side was never read, so it cannot be cited as one.
                # The issue keeps what it observed and loses the locator it
                # cannot support -- and, like any claim about text out of
                # view, it is not allowed to block generation.
                miscited += 1
                issue.conflicting_block_ordinal = None

            # An absence the document refutes is not raised at all.
            #
            # Deliberately BEFORE the fingerprint is recorded: a finding of
            # this kind raised by an earlier run is then one the current run
            # does not repeat, so the retirement pass below closes it. The
            # false questions already sitting open on a bid clear themselves
            # the next time analysis runs.
            if issue.issue_type in ABSENCE_TYPES:
                is_refuted, terms = _absence_is_refuted(issue.description, unseen_text=unseen_text)
                if is_refuted:
                    refuted += 1
                    log.info(
                        "ambiguity_absence_claim_refuted",
                        bid_id=str(bid_id),
                        issue_type=issue.issue_type.value,
                        ordinal=issue.source_block_ordinal,
                        found_elsewhere=terms[:5],
                    )
                    continue

            primary = by_ordinal[issue.source_block_ordinal]
            secondary = (
                by_ordinal[issue.conflicting_block_ordinal]
                if issue.conflicting_block_ordinal is not None
                else None
            )
            fingerprint = _fingerprint(
                issue.issue_type.value,
                str(issue.source_block_ordinal),
                str(issue.conflicting_block_ordinal),
                issue.description,
            )
            seen_fingerprints.add(fingerprint)
            # A partial view cannot establish that something is absent.
            #
            # BLANK and INCOMPLETE_SCOPE are the two types that assert absence,
            # and a batch holds fifteen blocks. Six of the eight BLOCKING
            # clarifications on the live bid were absence claims about sections
            # that exist -- integrations, technical requirements, evaluation
            # factors, submission dates -- and BLOCKING is not a label: it stops
            # generation. So one is only allowed when a single call saw the
            # whole document. The issue is still raised, and still says what the
            # model saw; it just cannot halt the bid on the strength of a
            # window.
            #
            # The contradiction types keep whatever severity they were given,
            # because the schema already requires both conflicting blocks to be
            # visible -- those claims are local and checkable.
            severity = issue.severity
            description = issue.description
            # `outline` empty means no headings were detected at all -- normal
            # for a scanned or densely typeset document. The prompt then tells
            # the model "every section is listed in document_outline", which is
            # false, and the guard that stopped six false "this section is
            # missing" blockers silently disappears. So an absence claim cannot
            # block generation when there was no outline to check it against.
            if (
                severity is ClarificationSeverity.BLOCKING
                and (not whole_document or not outline)
                and issue.issue_type
                in (ClarificationIssueType.BLANK, ClarificationIssueType.INCOMPLETE_SCOPE)
            ):
                severity = ClarificationSeverity.MAJOR
                why = (
                    f"reading part of the document ({len(batch)} of {len(blocks)} blocks)"
                    if not whole_document
                    else "reading a document whose sections could not be listed"
                )
                description = (
                    f"{description} [Raised while {why}; an absence cannot be "
                    "confirmed from that, so this does not block generation.]"
                )
            # A contradiction whose second side was never in view.
            #
            # The contradiction types keep their severity because the schema
            # guarantees both blocks were read. When the model cites one that
            # was not, that guarantee is gone, and the finding is in the same
            # position as any other claim about text out of view.
            if out_of_view:
                description = (
                    f"{description} [The block this was said to conflict with was "
                    "not in view when the finding was made, so only one side of it "
                    "has been read.]"
                )
                if severity is ClarificationSeverity.BLOCKING:
                    severity = ClarificationSeverity.MAJOR
            candidates = _requirement_candidates(
                session, ctx, bid_id=bid_id, blocks=[primary, secondary]
            )
            linked_ids, was_narrowed = concerned_requirements(candidates, issue.concerns)
            if not linked_ids:
                # The cited block yielded no requirements -- a heading, a
                # contents entry, a preamble -- so there was nothing to link
                # to, and the finding reached no row at all. Twelve open
                # questions on the live bid were in that state, and most of
                # them NAMED their requirements in the description: "the
                # penetration testing requirement (R-072) does not state...".
                #
                # Those codes are the model's own explicit answer to "what is
                # this about", and they are checked against the bid's real
                # requirements before anything is linked, so a hallucinated
                # code links nothing. This is the same division as everywhere
                # else here: the model names, the code decides.
                named = [
                    code_index[code]
                    for code in codes_named_in(
                        f"{issue.description} {issue.recommended_question or ''}"
                    )
                    if code in code_index
                ]
                if named:
                    linked_ids = named
                    linked_by_name += 1
            narrowed += was_narrowed
            widened += bool(candidates) and not was_narrowed
            # Linked to the requirements it CONCERNS, among those drawn from the
            # blocks it cites -- see `concerned_requirements`.
            #
            # Without this the finding is correct and unreachable: 918
            # clarifications on the live bid, 328 open, and not one of them
            # appears on a matrix row, so the column reads zero for every
            # requirement while the tender workbook carries hundreds of real
            # questions. A reviewer working from the matrix cannot see that the
            # clause in front of them is the one nobody understood.
            #
            # The blocks are where the two sides meet: the issue cites block
            # ordinals, and requirements record the block they came from, so the
            # join needs no matching and cannot drift.
            #
            # Whether this GATES the row is a separate question, decided by
            # `recommended_question` in the assessment. An ambiguity with a
            # question to ask puts the row in NEEDS_CLIENT_CLARIFICATION, which
            # is exactly right; one without is visible and does not stop work.
            clarifications.raise_issue(
                session,
                ctx,
                bid_id=bid_id,
                issue_type=issue.issue_type,
                description=description,
                severity=severity,
                blocks_generation=severity is ClarificationSeverity.BLOCKING,
                recommended_question=issue.recommended_question,
                source_a=_locator(primary),
                source_b=_locator(secondary) if secondary else None,
                detected_by=detected_by,
                fingerprint=fingerprint,
                requirement_ids=linked_ids,
            )
            raised += 1

    # Retire what this run no longer says, before linking what it does.
    #
    # Order matters: the backfill below reaches every open finding, so a stale
    # one closed after it would keep the links it was just given. Closing first
    # means the link pass only ever sees the current picture.
    retired = clarifications.close_findings_this_run_did_not_repeat(
        session,
        ctx,
        bid_id=bid_id,
        detected_by=detected_by,
        seen=seen_fingerprints,
        covered=covered_blocks,
        reason=(
            "Not raised by the latest run of this detector over the same blocks; "
            "superseded by that run."
        ),
    )
    if retired:
        log.info("clarifications_superseded", bid_id=str(bid_id), closed=retired)
    # Reported every run: the fallback is the old over-linking, so how often it
    # fires is how much of the fix actually applied.
    log.info(
        "clarification_links_scoped",
        bid_id=str(bid_id),
        narrowed_by_the_model=narrowed,
        fell_back_to_whole_block=widened,
        absence_claims_refuted=refuted,
        linked_by_the_codes_they_name=linked_by_name,
        citations_outside_the_batch=miscited,
    )

    # Group the phrasings before deciding what reaches a row.
    #
    # Runs before the link pass for the same reason the retirement does: the
    # backfill reaches every open finding and runs once, so a restatement judged
    # after it would keep the links it had just been given.
    from app.services import clarification_dedupe

    restatements = clarification_dedupe.dedupe_open_questions(
        session, ctx, bid_id=bid_id, model_gateway=model_gateway
    )
    if restatements:
        log.info("clarifications_deduped", bid_id=str(bid_id), marked=restatements)

    # Every OPEN finding, not just the ones this run happened to re-raise.
    #
    # Links are written at raise time, and a rerun re-raises only what the model
    # produced that time -- 22 of 304 on the live bid. The rest were raised by
    # earlier runs and nothing revisited them, so the findings that carried the
    # customer's real questions stayed detached from the rows they concern.
    # Backfilling here means any analysis run heals the whole set.
    linked = link_open_issues_to_their_blocks(session, ctx, bid_id=bid_id)
    if linked:
        log.info("clarifications_linked", bid_id=str(bid_id), links_created=linked)

    # Now that links exist, settle whose each open clarification is. Never allowed
    # to fail the analysis: a clarification without a department is still a
    # clarification.
    try:
        routed = clarifications.route_open_issues(
            session, ctx, bid_id=bid_id, gateway=model_gateway
        )
        log.info("clarifications_routed", bid_id=str(bid_id), **routed)
    except Exception as exc:  # noqa: BLE001
        log.warning("clarifications_routing_failed", reason=type(exc).__name__)
    return AmbiguityRun(raised, capped_batches)


def link_open_issues_to_their_blocks(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
) -> int:
    """Attach every open clarification to the requirements of the block it cites.

    Linking happens when a finding is RAISED, and a rerun only re-raises what
    the model produced that time. On the live bid that meant 22 of 304 open
    ambiguity findings got links: the other 282 were raised by earlier runs and
    nothing ever revisited them. A backfill that only reaches this run's output
    is not a backfill.

    Worse for the reader, the 22 that did link happened to carry no customer
    question, while 122 open findings do -- so the rows that should have read
    NEEDS_CLIENT_CLARIFICATION were precisely the ones still unlinked, and the
    matrix reported none at all.

    Everything needed is already stored: each issue keeps the locator of the
    block it was raised against, and requirements record the span they came
    from. So this is a join over data that exists, with no model call and no
    guessing.

    Runs over OPEN and SENT_TO_CLIENT issues only. A closed or answered finding
    is history; linking it now would put a resolved question back onto a live
    row. Returns the number of links created.

    And only over issues with NO links yet. This pass exists for findings that
    were never linked at all; an issue that already has links was scoped when
    it was raised (`concerned_requirements`), and re-linking it to its whole
    block here would undo that on the same run -- the v3 narrowing would be
    written and then silently widened back a few lines later.
    """
    from app.models import ClarificationIssue, ClarificationRequirement
    from app.models.enums import ClarificationStatus

    issues = (
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid_id,
                ClarificationIssue.status.in_(
                    [ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT]
                ),
                # A restatement is the same question in other words; its
                # canonical carries the row. Without this the backfill hands the
                # links straight back after dedupe has removed them, and the
                # matrix counts one question four times.
                ClarificationIssue.restates_clarification_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    already_linked = set(
        session.execute(
            select(ClarificationRequirement.clarification_id).where(
                ClarificationRequirement.org_id == ctx.org_id,
                ClarificationRequirement.clarification_id.in_([i.id for i in issues]),
            )
        ).scalars()
    )
    issues = [issue for issue in issues if issue.id not in already_linked]
    if not issues:
        return 0

    blocks = {
        (block.document_version_id, block.char_start, block.char_end): block
        for block in session.execute(
            select(DocumentBlock).where(DocumentBlock.org_id == ctx.org_id)
        ).scalars()
    }

    linked = 0
    for issue in issues:
        cited = []
        for locator in (issue.source_a, issue.source_b):
            if not isinstance(locator, dict):
                continue
            version_id = locator.get("document_version_id")
            if isinstance(version_id, str):
                try:
                    version_id = uuid.UUID(version_id)
                except ValueError:
                    continue
            start, end = locator.get("char_start"), locator.get("char_end")
            if not isinstance(version_id, uuid.UUID):
                continue
            if not (isinstance(start, int) and isinstance(end, int)):
                continue
            block = blocks.get((version_id, start, end))
            if block is not None:
                cited.append(block)
        if not cited:
            continue
        requirement_ids = _requirements_of(session, ctx, bid_id=bid_id, blocks=cited)
        if not requirement_ids:
            continue
        linked += clarifications.attach_requirements(session, ctx, issue, requirement_ids)
    return linked


def concerned_requirements(
    candidates: list[tuple[uuid.UUID, str]], concerns: list[str]
) -> tuple[list[uuid.UUID], bool]:
    """Which of the cited blocks' requirements an issue actually holds up.

    `candidates` are (id, code) for every live requirement drawn from the
    blocks the issue cites; `concerns` is what the model said the issue is
    about. Returns the ids to link and whether the model's answer was used.

    The model NAMES; this BOUNDS. A code the model was not shown under those
    blocks is ignored, so it can narrow the link but never move it somewhere
    else. If nothing it named survives -- it left `concerns` empty, or named
    only codes it was not shown -- the link falls back to the whole block, as
    before: a question that reaches too many rows is visible and can be
    argued with, while one that reaches none is the 918-unreachable failure
    this linking exists to prevent.
    """
    if not candidates:
        return [], False
    wanted = {code.strip().upper() for code in concerns if code and code.strip()}
    chosen = [rid for rid, code in candidates if (code or "").strip().upper() in wanted]
    if chosen:
        return list(dict.fromkeys(chosen)), True
    return list(dict.fromkeys(rid for rid, _ in candidates)), False


# A requirement code as the model writes it in prose: R-072, R-12, r-081.
_CODE_IN_TEXT = re.compile(r"\bR-(\d{1,4})\b", re.IGNORECASE)


def codes_named_in(text: str) -> list[str]:
    """Requirement codes the finding names in its own words, normalised."""
    return list(
        dict.fromkeys(f"R-{int(m.group(1)):03d}" for m in _CODE_IN_TEXT.finditer(text or ""))
    )


# Enough of each requirement to recognise its subject, and no more. The list
# ships with every batch, so its size is paid for once per call: 74
# requirements at this length is about 6KB.
_SUBJECT_CHARS = 90


def _requirement_code_index(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    """Code -> id for every live requirement of this bid."""
    from app.models import Requirement
    from app.models.enums import RequirementLifecycle

    rows = session.execute(
        select(Requirement.code, Requirement.id).where(
            Requirement.org_id == ctx.org_id,
            Requirement.bid_id == bid_id,
            Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
        )
    ).all()
    return {code: rid for code, rid in rows}


def _extracted_requirements(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> list[dict]:
    """Code and opening words of every live requirement of this bid."""
    from app.models import Requirement, RequirementVersion
    from app.models.enums import RequirementLifecycle

    rows = session.execute(
        select(Requirement.code, RequirementVersion.requirement_text)
        .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
        .where(
            Requirement.org_id == ctx.org_id,
            Requirement.bid_id == bid_id,
            Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
        )
        .order_by(Requirement.code)
    ).all()
    return [
        {"code": code, "text": " ".join((text or "").split())[:_SUBJECT_CHARS]}
        for code, text in rows
    ]


def _requirement_rows_of(session: Session, ctx: ExecutionContext, *, bid_id, block):
    from app.services.requirements import _requirements_from_block

    return _requirements_from_block(session, ctx, bid_id=bid_id, block=block)


def _requirement_candidates(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, blocks: list
) -> list[tuple[uuid.UUID, str]]:
    """(id, code) for the live requirements of these blocks, in order, once each."""
    seen: dict[uuid.UUID, str] = {}
    for block in blocks:
        if block is None:
            continue
        for requirement, _text in _requirement_rows_of(session, ctx, bid_id=bid_id, block=block):
            seen.setdefault(requirement.id, requirement.code)
    return list(seen.items())


def _requirements_of(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    blocks: list,
) -> list[uuid.UUID]:
    """The live requirements extracted from these blocks.

    Reuses extraction's own block-to-requirement lookup rather than repeating
    the locator comparison here. A diagnostic or a link that re-derives what it
    is linking to is how two parts of this codebase come to disagree about the
    same fact -- `check_extraction_funnel.py` reported the pre-fix number after
    the fix landed for exactly that reason.

    Order is preserved and duplicates removed, so a finding citing two blocks
    that share a requirement links it once.
    """
    from app.services.requirements import _requirements_from_block

    seen: dict[uuid.UUID, None] = {}
    for block in blocks:
        if block is None:
            continue
        for requirement, _text in _requirements_from_block(
            session, ctx, bid_id=bid_id, block=block
        ):
            seen.setdefault(requirement.id, None)
    return list(seen)


def _fingerprint(*parts: str | None) -> str:
    digest = hashlib.sha256("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()
    return digest[:32]


# --- deliverables -----------------------------------------------------------

# Words that carry no identity in a deliverable's name. Dropping them is what
# makes "Signed proposal by authorized representative" and "Signed proposals by
# an authorized representative" one submission instead of two.
_DELIVERABLE_NOISE = {"a", "an", "the", "by", "of", "for", "to", "and", "with", "in", "on"}


def _deliverable_key(name: str) -> str:
    """The name reduced to what identifies the submission.

    The fingerprint used the casefolded name as typed, so one clause saying
    "signed proposal" and another "signed proposals" produced two rows for one
    signature -- and a submission checklist that asks for the same document
    twice is worse than one that asks once, because a bid manager cannot tell
    which of the two is the real obligation.

    Deliberately crude: lowercase, drop punctuation and articles, and take the
    plural off each word. It cannot tell two genuinely different deliverables
    apart if their names differ only by a plural, and that is the safer
    direction -- the fold is visible in the export, and a missed fold is not.
    """
    words = []
    for raw in re.split(r"[^\w]+", (name or "").casefold()):
        if not raw or raw in _DELIVERABLE_NOISE:
            continue
        if len(raw) > 3 and raw.endswith("s") and not raw.endswith("ss"):
            raw = raw[:-1]
        words.append(raw)
    return " ".join(words)


def _legacy_fingerprint(proposal) -> str:
    """The key this module used before `_deliverable_key` existed.

    Kept so a row written under it can be recognised and re-stamped rather
    than duplicated. It is a compatibility shim with one job and no future:
    when no deliverable in any live bid still carries an old-scheme
    fingerprint, it goes.
    """
    return _fingerprint("deliverable", proposal.name.strip().casefold(), proposal.required_filename)


def fold_duplicate_deliverables(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> int:
    """Point same-submission rows at one canonical row.

    The schema has carried `duplicate_of_deliverable_id` since a review
    objected -- correctly -- to duplicates being folded by marking them
    NOT_APPLICABLE, which said mandatory and not-applicable about one row and
    recorded nothing about what it had been folded into. Nothing ever wrote to
    the column, so the sheet's `Duplicate of` was blank however many twins it
    listed.

    The survivor is the OLDEST row, because tasks, links and anything else
    that already points at a deliverable points at that one. A folded row
    keeps its name, its type and its mandatory flag: the tender really does
    require that submission, and it is the pointer, not the status, that says
    it is the same submission as another row.
    """
    rows = list(
        session.execute(
            select(Deliverable)
            .where(
                Deliverable.org_id == ctx.org_id,
                Deliverable.bid_id == bid_id,
                Deliverable.duplicate_of_deliverable_id.is_(None),
            )
            .order_by(Deliverable.created_at, Deliverable.id)
        ).scalars()
    )
    canonical: dict[tuple, Deliverable] = {}
    folded = 0
    for row in rows:
        key = (_deliverable_key(row.name), (row.required_filename or "").strip().casefold())
        if not key[0]:
            continue
        first = canonical.get(key)
        if first is None:
            canonical[key] = row
            continue
        row.duplicate_of_deliverable_id = first.id
        row.equivalence_by = "code:deliverable_key@v1"
        row.equivalence_reason = (
            f"The same submission as '{first.name}': the two names differ only in "
            "wording, not in what must be handed over."
        )
        folded += 1
        log.info("deliverable_folded", bid_id=str(bid_id), folded=row.name, into=first.name)
    if folded:
        session.flush()
    return folded


def extract_deliverables(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> int:
    blocks = tender_blocks(session, ctx, bid_id=bid_id)
    if not blocks:
        return 0
    model_gateway = gateway or build_gateway()
    call_type = get_call_type("deliverable_extraction")
    extracted_by = f"llm:{call_type.name}@{call_type.prompt_version}"
    by_ordinal = {b.ordinal: b for b in blocks}
    created = 0
    seen: set[str] = set()

    for batch in _batches(blocks):
        result = model_gateway.generate_structured(
            "deliverable_extraction", _payload(batch), ctx=ctx, session=session
        )
        if not isinstance(result, DeliverableExtractionResult):
            raise ValidationError("deliverable_extraction returned an unexpected schema.")
        allowed = {b.ordinal for b in batch}

        for proposal in result.deliverables:
            if proposal.source_block_ordinal not in allowed:
                raise ValidationError(
                    "deliverable_extraction cited a block ordinal outside its batch: "
                    f"{proposal.source_block_ordinal}"
                )
            fingerprint = _fingerprint(
                "deliverable", _deliverable_key(proposal.name), proposal.required_filename
            )
            # Two guards, because they catch different duplicates. The query
            # catches a deliverable from an earlier analysis run; `seen`
            # catches one the model named twice in this run — usually the same
            # document referenced from two clauses. The session runs with
            # autoflush off, so rows added below are invisible to the query
            # until the flush at the end of the loop; without `seen`, both
            # copies are added and the whole batch insert dies on
            # uq_deliverable_fingerprint, losing every extracted deliverable
            # and failing the request.
            if fingerprint in seen:
                continue
            # Rows for this submission under EITHER key.
            #
            # Changing how the key is computed re-keys every future run while
            # the stored rows keep the old key, so the next run matches nothing
            # and writes a second copy of the whole sheet: 15 deliverables
            # became 24, nine of them exact-name twins. Recognising the old key
            # migrates such a row in place, with no migration to run.
            matches = list(
                session.execute(
                    select(Deliverable).where(
                        Deliverable.org_id == ctx.org_id,
                        Deliverable.bid_id == bid_id,
                        Deliverable.fingerprint.in_([fingerprint, _legacy_fingerprint(proposal)]),
                    )
                ).scalars()
            )
            existing = next((m for m in matches if m.fingerprint == fingerprint), None)
            if existing is None and matches:
                # Nothing holds the new key, so the old-key row can take it.
                existing = matches[0]
                existing.fingerprint = fingerprint
                log.info("deliverable_fingerprint_migrated", name=existing.name)
            elif existing is not None and len(matches) > 1:
                # BOTH keys are present: the twin from the run that caused this
                # in the first place. Re-stamping the old-key row here is what
                # made analysis die on uq_deliverable_fingerprint -- two rows,
                # one key. The old row is left exactly as it is, and
                # `fold_duplicate_deliverables` below points it at this one,
                # which is the fold that should have happened instead of a
                # second row.
                log.info("deliverable_twin_left_to_fold", name=existing.name)
            if existing is not None:
                seen.add(fingerprint)
                continue
            seen.add(fingerprint)

            session.add(
                Deliverable(
                    org_id=ctx.org_id,
                    bid_id=bid_id,
                    name=proposal.name.strip(),
                    deliverable_type=proposal.deliverable_type,
                    required_format=proposal.required_format,
                    required_filename=proposal.required_filename,
                    deadline=proposal.deadline,
                    is_mandatory=proposal.is_mandatory,
                    status=DeliverableStatus.IDENTIFIED,
                    source_locator=_locator(by_ordinal[proposal.source_block_ordinal]),
                    extracted_by=extracted_by,
                    fingerprint=fingerprint,
                )
            )
            created += 1
    session.flush()

    # After creating, before reporting: twins already in the table from a
    # previous scheme are folded whether or not this run added anything.
    folded = fold_duplicate_deliverables(session, ctx, bid_id=bid_id)

    if created or folded:
        audit.record(
            session,
            ctx,
            action="tender.deliverables_extracted",
            entity_type="bid",
            entity_id=bid_id,
            bid_id=bid_id,
            after={"created": created, "folded": folded},
        )
    return created


def link_deliverable(
    session: Session,
    ctx: ExecutionContext,
    *,
    deliverable_id: uuid.UUID,
    requirement_id: uuid.UUID,
) -> DeliverableRequirement:
    existing = session.execute(
        select(DeliverableRequirement).where(
            DeliverableRequirement.org_id == ctx.org_id,
            DeliverableRequirement.deliverable_id == deliverable_id,
            DeliverableRequirement.requirement_id == requirement_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    link = DeliverableRequirement(
        org_id=ctx.org_id,
        deliverable_id=deliverable_id,
        requirement_id=requirement_id,
    )
    session.add(link)
    session.flush()
    return link


# --- scope confidence -------------------------------------------------------


@dataclass
class ScopeSignals:
    """Every input is a measurement of the TENDER."""

    clause_coverage_ratio: float = 1.0
    coverage_gaps: int = 0
    unresolved_precedence: int = 0
    blocking_issues: int = 0
    major_issues: int = 0
    contradictions: int = 0
    placeholder_requirements: int = 0
    requirements_found: int = 0
    # Batches whose ambiguity reading stopped at the cap: the document was read
    # partially, so nothing here can be graded as a complete reading.
    ambiguity_batches_capped: int = 0
    has_deadline: bool = False
    has_submission_rules: bool = False
    has_evaluation_criteria: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items() if k != "reasons"}
        data["reasons"] = self.reasons
        return data


def compute_scope_confidence(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    coverage_ratio: float | None = None,
    ambiguity_batches_capped: int = 0,
) -> ScopeConfidence:
    """Deterministic. No model call, and no company-evidence input.

    Whether the organization holds matching evidence is a question about us, not
    about the tender. Feeding it in here would let a perfectly clear tender we
    happen to be unready for look ambiguous, and would block drafting for a
    reason clarification questions cannot fix.
    """
    issue_counts = clarifications.counts(session, ctx, bid_id=bid_id)
    open_issues = clarifications.list_issues(session, ctx, bid_id=bid_id)

    contradiction_types = {
        ClarificationIssueType.CONTRADICTORY_DATE,
        ClarificationIssueType.CONTRADICTORY_QUANTITY,
        ClarificationIssueType.CONFLICTING_INSTRUCTION,
    }
    signals = ScopeSignals(
        coverage_gaps=sum(
            1 for i in open_issues if i.issue_type is ClarificationIssueType.COVERAGE_GAP
        ),
        unresolved_precedence=sum(
            1 for i in open_issues if i.issue_type is ClarificationIssueType.PRECEDENCE_UNRESOLVED
        ),
        contradictions=sum(1 for i in open_issues if i.issue_type in contradiction_types),
        placeholder_requirements=sum(
            1 for i in open_issues if i.issue_type is ClarificationIssueType.PLACEHOLDER
        ),
        blocking_issues=issue_counts.get("blocking", 0),
        major_issues=issue_counts.get("open_major", 0),
    )

    signals.ambiguity_batches_capped = ambiguity_batches_capped
    all_requirements = requirements.list_requirements(session, ctx, bid_id=bid_id)
    signals.requirements_found = sum(
        1
        for r in all_requirements
        if r.applicability is not RequirementApplicability.NOT_APPLICABLE
    )

    if coverage_ratio is not None:
        signals.clause_coverage_ratio = coverage_ratio
    else:
        total_clauses = len(precedence.current_resolutions(session, ctx, bid_id=bid_id))
        signals.clause_coverage_ratio = (
            1.0
            if total_clauses == 0
            else round(max(0.0, (total_clauses - signals.coverage_gaps) / total_clauses), 4)
        )

    bid = bids.get_bid(session, ctx, bid_id=bid_id)
    facts = bid.tender_facts or {}
    opportunity = session.get(Opportunity, bid.opportunity_id)
    # A deadline the bid team recorded on the opportunity counts, as does one the tender states.
    signals.has_deadline = bool(facts.get("submission_deadline")) or bool(
        opportunity is not None and opportunity.deadline
    )
    signals.has_submission_rules = bool(facts.get("submission_rules"))
    signals.has_evaluation_criteria = bool(facts.get("evaluation_criteria"))

    level, reasons = _grade(signals)
    signals.reasons = reasons

    now = datetime.now(timezone.utc)
    existing = session.execute(
        select(ScopeConfidence).where(
            ScopeConfidence.org_id == ctx.org_id, ScopeConfidence.bid_id == bid_id
        )
    ).scalar_one_or_none()

    record = existing or ScopeConfidence(org_id=ctx.org_id, bid_id=bid_id)
    before = existing.level.value if existing else None
    record.level = level
    record.signals = signals.as_dict()
    record.blocking_reasons = reasons or None
    record.detailed_drafting_allowed = level is not ScopeConfidenceLevel.LOW
    record.computed_at = now
    record.computed_by = "scope_confidence@v1"
    if existing is None:
        session.add(record)
    session.flush()

    audit.record(
        session,
        ctx,
        action="tender.scope_confidence_computed",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        before={"level": before} if before else None,
        after={"level": level.value, "detailed_drafting_allowed": record.detailed_drafting_allowed},
    )
    return record


def _grade(signals: ScopeSignals) -> tuple[ScopeConfidenceLevel, list[str]]:
    reasons: list[str] = []

    if signals.requirements_found == 0:
        reasons.append("No requirements were extracted from the tender.")
    if signals.blocking_issues:
        reasons.append(f"{signals.blocking_issues} blocking clarification issue(s) are unresolved.")
    if signals.unresolved_precedence:
        reasons.append(f"{signals.unresolved_precedence} clause(s) have no established precedence.")
    if signals.clause_coverage_ratio < MEDIUM_COVERAGE:
        reasons.append(
            f"Only {signals.clause_coverage_ratio:.0%} of governing clauses are accounted for."
        )
    if not signals.has_deadline:
        reasons.append("No submission deadline could be identified.")

    if reasons:
        return ScopeConfidenceLevel.LOW, reasons

    downgrades: list[str] = []
    if signals.clause_coverage_ratio < HIGH_COVERAGE:
        downgrades.append(
            f"Clause coverage is {signals.clause_coverage_ratio:.0%}, below the "
            f"{HIGH_COVERAGE:.0%} required for high confidence."
        )
    if signals.major_issues > HIGH_MAX_MAJOR_ISSUES:
        downgrades.append(f"{signals.major_issues} major clarification issue(s) are open.")
    if signals.contradictions:
        downgrades.append(f"{signals.contradictions} contradiction(s) remain unresolved.")
    if signals.placeholder_requirements:
        downgrades.append(
            f"{signals.placeholder_requirements} requirement(s) rest on placeholder text."
        )
    if not signals.has_submission_rules or not signals.has_evaluation_criteria:
        downgrades.append("Submission rules or evaluation criteria are incomplete.")
    if signals.ambiguity_batches_capped:
        downgrades.append(
            f"{signals.ambiguity_batches_capped} section(s) returned the maximum number of "
            "findings allowed, so the tender has been read only partially."
        )

    if signals.major_issues > MEDIUM_MAX_MAJOR_ISSUES:
        return ScopeConfidenceLevel.LOW, downgrades
    if downgrades:
        return ScopeConfidenceLevel.MEDIUM, downgrades
    return ScopeConfidenceLevel.HIGH, []


def get_scope_confidence(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> ScopeConfidence | None:
    return session.execute(
        select(ScopeConfidence).where(
            ScopeConfidence.org_id == ctx.org_id, ScopeConfidence.bid_id == bid_id
        )
    ).scalar_one_or_none()


# --- orchestration ----------------------------------------------------------


@dataclass
class AnalysisResult:
    precedence: dict
    facts: dict
    requirements: dict
    deliverables: int
    ambiguities: int
    ambiguity_batches_capped: int
    scope_confidence: str
    detailed_drafting_allowed: bool
    obligations: dict | None = None
    quantities: dict | None = None

    def as_dict(self) -> dict:
        return {
            "precedence": self.precedence,
            "requirements": self.requirements,
            "deliverables": self.deliverables,
            "obligations": self.obligations,
            "quantities": self.quantities,
            "ambiguities": self.ambiguities,
            "ambiguity_batches_capped": self.ambiguity_batches_capped,
            "scope_confidence": self.scope_confidence,
            "detailed_drafting_allowed": self.detailed_drafting_allowed,
            "has_facts": bool(self.facts),
        }


STILL_READING = (
    DocumentVersionState.UPLOADED,
    DocumentVersionState.SCANNING,
    DocumentVersionState.PARSING,
    DocumentVersionState.PARSED,
    DocumentVersionState.CLASSIFIED,
)


def _refuse_while_documents_are_being_read(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> None:
    """Analysis started before parsing finished reads nothing and reports "0 found".

    Seen live: a 524 KB tender uploaded, "Run analysis" pressed three seconds
    later, parsing finished thirty seconds after that -- and the screen said no
    clauses were found, which was true of the moment and false of the document.
    """
    pending = (
        session.execute(
            select(Document.original_filename)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(
                Document.org_id == ctx.org_id,
                Document.bid_id == bid_id,
                Document.side == DocumentSide.TENDER,
                Document.withdrawn_at.is_(None),
                DocumentVersion.state.in_(STILL_READING),
            )
        )
        .scalars()
        .all()
    )
    if pending:
        names = ", ".join(sorted(set(pending)))
        raise StateConflict(
            f"Still reading {names}. Wait until every document shows INDEXED, "
            "then run the analysis."
        )


def analyze_bid(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> AnalysisResult:
    """The full tender-understanding pass, in dependency order.

    Precedence first, because everything downstream must read only the clause
    text that actually governs. Scope Confidence last, because it grades what
    the earlier steps found.
    """
    model_gateway = gateway or build_gateway()
    bid = bids.get_bid(session, ctx, bid_id=bid_id)
    if bid.state not in (BidState.INTAKE_IN_PROGRESS, BidState.UNDERSTANDING_COMPLETE):
        raise ValidationError(
            f"A bid in {bid.state.value} is not in a state where tender analysis applies."
        )

    _refuse_while_documents_are_being_read(session, ctx, bid_id=bid_id)
    stated_priority = extract_precedence_hints(session, ctx, bid_id=bid_id, gateway=model_gateway)
    precedence_report = precedence.resolve_bid_precedence(
        session, ctx, bid_id=bid_id, stated_priority=stated_priority
    )
    facts = extract_tender_facts(session, ctx, bid_id=bid_id, gateway=model_gateway)
    # Retire the drift notes from the run that raised them as client questions.
    # They were linked to their requirements, which made 51 of 59 rows report an
    # open question with the customer; the notes are still raised, but unlinked,
    # so the stale ones have to be closed where the credentials and the tenant
    # context already are — here, rather than in a script guessing at a password.
    clarifications.close_detector_issues(
        session,
        ctx,
        bid_id=bid_id,
        detected_by="requirement_identity_drift@v1",
        reason="Identity-drift notes are internal review items, not client questions.",
    )
    extraction = requirements.extract_requirements(
        session, ctx, bid_id=bid_id, gateway=model_gateway
    )
    # One batched call over only the clauses a deterministic rule could not
    # settle. It runs here rather than inside decomposition because it is a
    # different question asked once for the whole tender, not a field on a
    # per-batch answer — which is exactly why the per-batch answer was
    # inconsistent between sections.
    obligations = requirements.review_obligation_owners(
        session, ctx, bid_id=bid_id, gateway=model_gateway
    )
    # Same shape, same reason: one question, asked once for the whole tender,
    # over only the clauses that could possibly answer it. A clause with no
    # numeral in it cannot demand three of something, so it never reaches the
    # model at all.
    quantities = requirements.review_requirement_quantities(
        session, ctx, bid_id=bid_id, gateway=model_gateway
    )
    deliverables = extract_deliverables(session, ctx, bid_id=bid_id, gateway=model_gateway)
    ambiguities = analyze_ambiguity(session, ctx, bid_id=bid_id, gateway=model_gateway)
    confidence = compute_scope_confidence(
        session,
        ctx,
        bid_id=bid_id,
        coverage_ratio=extraction.coverage_ratio,
        ambiguity_batches_capped=getattr(ambiguities, "capped_batches", 0),
    )

    result = AnalysisResult(
        precedence=precedence_report.as_dict(),
        facts=facts,
        requirements=extraction.as_dict(),
        deliverables=deliverables,
        ambiguities=int(ambiguities),
        ambiguity_batches_capped=getattr(ambiguities, "capped_batches", 0),
        scope_confidence=confidence.level.value,
        detailed_drafting_allowed=confidence.detailed_drafting_allowed,
        obligations=obligations,
        quantities=quantities,
    )
    audit.record(
        session,
        ctx,
        action="tender.analysis_completed",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        after=result.as_dict(),
    )
    return result
