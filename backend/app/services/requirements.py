"""Atomic requirement extraction from winning tender clauses.

Missing a mandatory requirement is the worst failure this product can have, so
extraction is deliberately not a single model pass:

  1. every tender block is eligible EXCEPT one a different document version
     supersedes — a superseded clause cannot become a requirement, but a clause
     that merely shares a number with another section's must not be dropped;
  2. the model decomposes those blocks into atomic obligations, and must
     explicitly account for any block it judges to impose nothing;
  3. a deterministic cross-check then walks every eligible block and reports the
     ones that produced neither a requirement nor an explicit decision.

Step 3 is what turns "the model was quiet about that clause" from an invisible
gap into a counted, reported one.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.core.logging import get_logger
from app.gateway.gateway import ModelGateway, build_gateway
from app.gateway.registry import get as get_call_type
from app.gateway.registry import wrap_untrusted
from app.models import (
    BlockExtraction,
    ClarificationRequirement,
    ClauseResolution,
    Document,
    DocumentBlock,
    DocumentVersion,
    Requirement,
    RequirementAssessment,
    RequirementVersion,
)
from app.models.enums import (
    BlockType,
    ClarificationIssueType,
    ClarificationSeverity,
    DocumentSide,
    DocumentVersionState,
    ObligationOwner,
    RequirementApplicability,
    RequirementLifecycle,
    RiskLevel,
)
from app.schemas.source_locator import SourceLocator
from app.schemas.tender_extraction import (
    ConditionExtractionResult,
    RequirementDecompositionResult,
)
from app.services import audit, clarifications
from app.services.clause_identity import qualified_clause_ref as _qualified_clause_ref
from app.services.placeholders import PLACEHOLDER_RE as _PLACEHOLDER_RE

log = get_logger("requirements")

MAX_BATCH_BLOCKS = 15
MAX_BATCH_CHARACTERS = 18_000

# Shared with evidence approval, which has to make the same judgement about
# company text. See app/services/placeholders.py.
PLACEHOLDER_RE = _PLACEHOLDER_RE


@dataclass
class ExtractionReport:
    requirements_created: int = 0
    requirements_updated: int = 0
    blocks_considered: int = 0
    blocks_covered: int = 0
    blocks_explicitly_empty: int = 0
    coverage_gaps: list[str] = field(default_factory=list)
    model_calls: int = 0
    # Proposals the same answer made twice in different words. Reported rather
    # than silently discarded: a model repeating itself is a fact about the run,
    # and a number that quietly drops to zero is how a fix stops being noticed.
    echoes_dropped: int = 0
    # Blocks not sent to the model because this exact prompt version already
    # decomposed them. Counted, never silent: a run that does nothing must say
    # so, or "no new requirements" and "nothing was looked at" become the same
    # output.
    blocks_already_decomposed: int = 0
    # A batch the model could not answer, halved and retried; and a single
    # block that still could not be answered.
    batches_split: int = 0
    blocks_unreadable: int = 0
    # Requirements a superseding tender document reworded, as
    # (requirement_id, amendment document version id). Everything that depends
    # on them -- tasks, approved artifacts -- is flagged for revalidation at the
    # end of the run. It had to be called by hand, with the requirement ids
    # supplied by the caller, so in practice it never was: an approved answer
    # to "25 web applications" quietly survived an amendment saying 35.
    amended: list = field(default_factory=list)
    # Requirements withdrawn into a neighbour that already said everything they
    # said -- the page-break duplicate. Reported for the same reason as
    # `echoes_dropped`: a fold nobody can see is a fold nobody can check.
    page_break_folds: int = 0
    # Requirements withdrawn because the current prompt version has re-read
    # their clause. Counted for the same reason: a register that quietly
    # changes size needs to say why.
    stale_readings_retired: int = 0

    @property
    def coverage_ratio(self) -> float:
        if self.blocks_considered == 0:
            return 1.0
        # A block skipped because this prompt version already decomposed it IS
        # accounted for -- its requirements exist. Leaving it out would make a
        # correct no-op rerun report a coverage collapse and raise gaps against
        # blocks that are already covered.
        accounted = (
            self.blocks_covered + self.blocks_explicitly_empty + self.blocks_already_decomposed
        )
        return round(accounted / self.blocks_considered, 4)

    def as_dict(self) -> dict:
        return {
            "requirements_created": self.requirements_created,
            "requirements_updated": self.requirements_updated,
            "blocks_considered": self.blocks_considered,
            "blocks_covered": self.blocks_covered,
            "blocks_explicitly_empty": self.blocks_explicitly_empty,
            "coverage_gaps": len(self.coverage_gaps),
            "coverage_ratio": self.coverage_ratio,
            "model_calls": self.model_calls,
            "echoes_dropped": self.echoes_dropped,
            "blocks_already_decomposed": self.blocks_already_decomposed,
            "amended": len(self.amended),
        }


def eligible_blocks(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> list[tuple[DocumentBlock, ClauseResolution | None]]:
    """Every tender block the model should read, minus the ones truly superseded.

    This used to be defined the other way round -- "blocks that won a current
    clause resolution" -- and that inversion is what capped one tender at 14
    requirements out of roughly 39 obligations.

    Two separate losses came out of it:

      A block with no clause reference had no resolution and so was never
      eligible at all. Tenders state plenty of obligations as unnumbered prose,
      and none of them could become requirements.

      A block whose reference collided with another block's was worse. A
      resolution is created per distinct reference, and the reference used to be
      the bare `clause_ref`. This tender restarts numbering in every section, so
      Business 3, Business 3 again and Technical 3 formed one "contest",
      precedence kept one winner, and the other two were dropped as losers of a
      dispute that never existed. 25 of 39 numbered clauses disappeared here,
      before any model saw them.

    Precedence answers a narrow question: when two *different tender documents*
    both speak to the same clause, whose wording governs. Extraction's question
    is much broader -- what must the bidder do -- and borrowing precedence's
    filter silently made the second question inherit the first one's
    preconditions.

    So the rule is now: read everything on the tender side, and exclude a block
    only when a current, confident resolution says a *different document
    version* supersedes it. Two blocks inside one document can never suppress
    each other, because one document's numbering restarting is not a dispute.

    The returned resolution is the one governing that block where a real
    contest existed, and None otherwise -- `RequirementVersion.clause_resolution_id`
    is nullable precisely because an uncontested clause has nothing to record.
    """
    from app.services.precedence import MIN_CONFIDENCE

    blocks = (
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
    if not blocks:
        return []

    resolutions = (
        session.execute(
            select(ClauseResolution).where(
                ClauseResolution.org_id == ctx.org_id,
                ClauseResolution.bid_id == bid_id,
                ClauseResolution.is_current.is_(True),
                ClauseResolution.confidence >= MIN_CONFIDENCE,
                ClauseResolution.winning_block_id.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    by_ref = {r.clause_ref: r for r in resolutions}

    pairs: list[tuple[DocumentBlock, ClauseResolution | None]] = []
    for block in blocks:
        resolution = by_ref.get(qualified_clause_ref(block) or "")
        if resolution is None:
            pairs.append((block, None))
            continue
        superseded = (
            resolution.winning_block_id != block.id
            # The conjunct that makes this correct. Without it, the losing
            # blocks of a same-document collision are excluded again and the
            # bug returns wearing a new query.
            and resolution.winning_document_version_id != block.document_version_id
        )
        if superseded:
            continue
        pairs.append((block, resolution))
    return pairs


# A clause that names the bidder as the actor is the bidder's obligation, and no
# classifier gets to say otherwise. This is the backstop for the one error the
# obligation-owner distinction must never make: excusing a real obligation and
# taking a no-bid risk off the red list. It is deliberately narrow — it only
# ever moves a label back TOWARD the bidder, never away — so a false match costs
# a cluttered review list and nothing more.
_BIDDER_ACTOR = re.compile(
    # "The Firm must notify ADX" is how this tender words half its obligations,
    # and the first version of this pattern did not know the word — so the
    # clauses most plainly the bidder's were the ones it could not vouch for.
    r"\b(?:the\s+)?(?:successful\s+|selected\s+|winning\s+)?"
    r"(?:supplier|contractor|bidder|vendor|tenderer|firm|proposer|respondent|seller)"
    # Tenders soften obligations constantly — "is requested to", "is expected
    # to" — and R-021 ("the supplier is requested to maintain the System
    # Document File") is a real mandatory requirement written in exactly that
    # register. A polite verb does not make it the customer's job.
    r"(?:'s|s')?\s+(?:shall|must|will|should|agrees|undertakes|warrants|"
    r"(?:is|are)\s+(?:required|liable|requested|expected|responsible|obliged)|"
    r"(?:has|have)\s+to)\b",
    re.IGNORECASE,
)


# A prohibition with no named actor binds the person holding the document.
#
# R-001 on the live tender: "These requirements are the property of Jadwa ... It
# contains information that may not be circulated, copied or distributed in any
# form whatsoever without the express written permission of the Management."
# The model answered CUSTOMER, and the shape is why -- the sentence asserts the
# customer's property right, and names nobody as the actor. But the duty it
# creates is the READER's, and the reader is the bidder: breaching it is the
# bidder's breach. Filed as the customer's, a confidentiality undertaking
# disappears from the obligations anyone reviews.
#
# Narrow on purpose: a passive prohibition, and only the verbs that describe
# what a recipient of a document could do with it.
_BIDDER_RESTRICTION = re.compile(
    r"\b(?:may|shall|must|can|will)\s+not\s+(?:be\s+)?"
    r"(?:circulat|copi|copy|distribut|disclos|reproduc|share|shar|publish|"
    r"discuss|transmit|forward|retain|us(?:e|ed))\w*",
    re.IGNORECASE,
)


def resolved_obligation_owner(text: str, proposed: ObligationOwner) -> ObligationOwner:
    """The owner to store, after checking the model against the clause text.

    Only one transition is possible here: CUSTOMER back to BIDDER. Nothing
    promotes a clause to CUSTOMER, because that is the direction that loses
    information, and a regular expression is not a good enough reason to stop
    asking a person about an obligation.
    """
    if proposed is ObligationOwner.CUSTOMER and _BIDDER_RESTRICTION.search(text or ""):
        log.info(
            "obligation_owner_overridden", proposed=proposed.value, to="BIDDER", why="restriction"
        )
        return ObligationOwner.BIDDER
    if proposed is ObligationOwner.CUSTOMER and _BIDDER_ACTOR.search(text or ""):
        log.info("obligation_owner_overridden", proposed=proposed.value, to="BIDDER")
        return ObligationOwner.BIDDER
    return proposed


def _next_code(session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID) -> str:
    used = session.execute(
        select(func.count())
        .select_from(Requirement)
        .where(Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id)
    ).scalar_one()
    return f"R-{used + 1:03d}"


def _preceding_text(
    session: Session,
    ctx: ExecutionContext,
    *,
    pairs: list[tuple[DocumentBlock, ClauseResolution | None]],
) -> dict[int, str]:
    """For a block that cannot stand alone, the text immediately before it.

    A PDF page break cut one bulleted list in half. Block 25 of the tender reads
    "2. Tradenet Core OMS System ... Jadwa will provide access to Core OMS system
    for the following purpose  Check client already exsist  Create client account
    ...", and block 26 is the rest of the same list: " Create Beneficary accounts
    Create Electronic trading account with defined settings  Auto approval of
    compliance based on new system approval". Three bullets, no subject, no
    clause reference, starting one character after block 25 ends.

    Shown that fragment alone, the model has to supply a subject, and it supplied
    a different one every run -- "The system must integrate with the Tradenet
    Core OMS system to create beneficiary accounts", "The system must create
    beneficiary accounts via the Tradenet Core OMS system". Substantively right,
    verbally different, so nothing matched anything and the block accumulated
    eight requirements for three bullets.

    The obvious repair -- join the two blocks in the parser -- is the expensive
    one. Character spans are the coordinate requirement identity is keyed on, so
    re-splitting the document renumbers every locator in it and needs a
    migration that remaps them. This instead leaves the blocks exactly as they
    are and gives the model the preceding text as CONTEXT, which is the thing it
    was actually missing.

    Offered only where all of these hold:

      * the block has no clause reference of its own -- with one it is
        self-identifying and needs no help;
      * the previous block is immediately contiguous (`char_end + 1 ==
        char_start`), so it really is the text before this one and not a
        neighbour across a section boundary;
      * both sit under the same heading path.

    A block that satisfies all three is a continuation. Anything else gets no
    context, because context that is not actually continuous would invite
    exactly the invention this is meant to stop.
    """
    by_version: dict = defaultdict(list)
    for block, _resolution in pairs:
        by_version[block.document_version_id].append(block)

    out: dict[int, str] = {}
    for version_id, blocks in by_version.items():
        # The previous block may not be eligible itself (a heading, a superseded
        # clause), so neighbours are read from the document rather than from the
        # eligible set -- otherwise the context would depend on which blocks
        # happened to be extractable.
        ordered = (
            session.execute(
                select(DocumentBlock)
                .where(
                    DocumentBlock.org_id == ctx.org_id,
                    DocumentBlock.document_version_id == version_id,
                )
                .order_by(DocumentBlock.ordinal)
            )
            .scalars()
            .all()
        )
        before = {block.ordinal: ordered[index - 1] for index, block in enumerate(ordered) if index}

        for block in blocks:
            if block.clause_ref is not None:
                continue
            previous = before.get(block.ordinal)
            if previous is None:
                continue
            if previous.char_end + 1 != block.char_start:
                continue
            if (previous.heading_path or None) != (block.heading_path or None):
                continue
            out[block.ordinal] = previous.text or ""
    return out


def _table_headers(session: Session, ctx: ExecutionContext, *, pairs) -> dict[int, str]:
    """The header row of the table each matrix row belongs to.

    A compliance matrix arrives one row per block: "Requirement: The system
    shall support SSO | Compliance: | Remarks:". Without its header the row is
    readable, but WHICH column holds the obligation and which are the bidder's
    response columns is a guess -- and a tender written as a matrix is most of
    the Gulf market. `table_name` was already sent and is only the enclosing
    heading; the header itself sits in the TABLE block that opens the table,
    usually in an earlier batch, so the model never saw it.
    """
    rows = [b for b, _ in pairs if b.block_type is BlockType.TABLE_ROW]
    if not rows:
        return {}
    by_version: dict = defaultdict(list)
    for block in rows:
        by_version[block.document_version_id].append(block)

    out: dict[int, str] = {}
    for version_id, blocks in by_version.items():
        headers = (
            session.execute(
                select(DocumentBlock)
                .where(
                    DocumentBlock.org_id == ctx.org_id,
                    DocumentBlock.document_version_id == version_id,
                    DocumentBlock.block_type == BlockType.TABLE,
                )
                .order_by(DocumentBlock.ordinal)
            )
            .scalars()
            .all()
        )
        if not headers:
            continue
        for block in blocks:
            # The table this row belongs to is the last TABLE block before it.
            candidates = [h for h in headers if h.ordinal < block.ordinal]
            if not candidates:
                continue
            header = candidates[-1]
            if header.text:
                out[block.ordinal] = header.text
    return out


def _record_extraction(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    block: DocumentBlock,
    extracted_by: str,
    found: int,
) -> None:
    """Note that this prompt version read this block, and what it yielded.

    Upserted by hand rather than with a dialect-specific ON CONFLICT, because
    the same pass can legitimately run twice -- a retried batch, a re-analysis
    after a failure -- and the second must update the first rather than raise
    against the uniqueness constraint or silently do nothing.
    """
    existing = (
        session.execute(
            select(BlockExtraction).where(
                BlockExtraction.org_id == ctx.org_id,
                BlockExtraction.bid_id == bid_id,
                BlockExtraction.document_version_id == block.document_version_id,
                BlockExtraction.char_start == block.char_start,
                BlockExtraction.char_end == block.char_end,
                BlockExtraction.extracted_by == extracted_by,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        existing.found = found
        return
    session.add(
        BlockExtraction(
            org_id=ctx.org_id,
            bid_id=bid_id,
            document_version_id=block.document_version_id,
            char_start=block.char_start,
            char_end=block.char_end,
            extracted_by=extracted_by,
            found=found,
        )
    )


def _block_read_and_found_nothing(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    block: DocumentBlock,
) -> bool:
    """Has any prompt version read this block and recorded that it imposes none?

    The coverage check exists to catch a clause nobody accounted for. A recorded
    reading that found nothing IS an accounting -- "v6 read the executive
    summary and it is background, not a duty" -- and `BlockExtraction.found`
    stores exactly that, which is the reason the table was added in 0019.

    Any version counts, not just the current one. The question here is whether a
    human needs to look at this clause, and a considered "no requirements" from
    an earlier prompt still answers it. Re-decomposition is governed separately
    by `_blocks_already_decomposed`, which is correctly stricter.
    """
    return (
        session.execute(
            select(BlockExtraction.id).where(
                BlockExtraction.org_id == ctx.org_id,
                BlockExtraction.bid_id == bid_id,
                BlockExtraction.document_version_id == block.document_version_id,
                BlockExtraction.char_start == block.char_start,
                BlockExtraction.char_end == block.char_end,
                BlockExtraction.found == 0,
            )
        ).first()
        is not None
    )


def _blocks_already_decomposed(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    extracted_by: str,
    pairs: list[tuple[DocumentBlock, ClauseResolution | None]],
) -> set[int]:
    """Ordinals whose requirements were already produced by this exact prompt.

    Keyed on the three things that determine the answer: the document version,
    the character span, and the string in `extracted_by` -- which carries both
    the call type and its prompt version. Anything else changing (a new
    document version, a re-ingest, a prompt bump) yields a different key and the
    block is decomposed again.

    Deliberately reads the CURRENT version of each requirement only. A block
    whose requirement has since been withdrawn is not "done": the obligation is
    no longer recorded, and refusing to look at it again would make a withdrawal
    permanent by accident.
    """
    # The record of the READING. A block this prompt version read is done
    # whether or not it yielded anything -- see `BlockExtraction`. Inferring
    # this from requirements alone (as this function first did) makes a block
    # correctly judged empty look unread, so it is re-decomposed on every run
    # and its superseded rows can never be retired.
    done: set[tuple] = {
        (version_id, char_start, char_end)
        for version_id, char_start, char_end in session.execute(
            select(
                BlockExtraction.document_version_id,
                BlockExtraction.char_start,
                BlockExtraction.char_end,
            ).where(
                BlockExtraction.org_id == ctx.org_id,
                BlockExtraction.bid_id == bid_id,
                BlockExtraction.extracted_by == extracted_by,
            )
        ).all()
    }

    # The record is the ONLY source. Requirements used to be consulted too, as a
    # fallback for bids predating this table: a requirement stamped with the
    # current prompt version was taken as proof its block had been read.
    #
    # That fallback quietly outranked the record. Clearing `block_extraction` to
    # force a re-read after the matching rules changed cleared the table and
    # nothing else, so every block whose requirement already carried the current
    # stamp stayed "done" -- 29 of 47 blocks, including the whole Integration and
    # Technical sections, were never sent to the model. The analysis reported
    # success, the count moved (other blocks did get read), and the clauses
    # everyone was waiting for were silently untouched.
    #
    # The cost of removing it is that a bid extracted before this table existed
    # re-decomposes once. That is a bounded, visible cost, paid once. The cost of
    # keeping it was a control that reported doing something and did not.

    return {
        block.ordinal
        for block, _resolution in pairs
        if (block.document_version_id, block.char_start, block.char_end) in done
    }


def _batches(blocks: list[tuple[DocumentBlock, ClauseResolution | None]]):
    batch: list[tuple[DocumentBlock, ClauseResolution | None]] = []
    characters = 0
    for pair in blocks:
        text = pair[0].text
        if batch and (
            len(batch) >= MAX_BATCH_BLOCKS or characters + len(text) > MAX_BATCH_CHARACTERS
        ):
            yield batch
            batch, characters = [], 0
        batch.append(pair)
        characters += len(text)
    if batch:
        yield batch


def extract_requirements(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> ExtractionReport:
    pairs = eligible_blocks(session, ctx, bid_id=bid_id)
    report = ExtractionReport(blocks_considered=len(pairs))
    if not pairs:
        return report

    model_gateway = gateway or build_gateway()
    call_type = get_call_type("requirement_decomposition")
    extracted_by = f"llm:{call_type.name}@{call_type.prompt_version}"

    block_by_ordinal = {block.ordinal: (block, resolution) for block, resolution in pairs}

    # A block this prompt version has already decomposed is not sent again.
    #
    # Reconciliation was carrying a weight it cannot carry. It can match a
    # proposal to an existing requirement, but it cannot stop the model
    # proposing a DIFFERENT decomposition of the same text -- and on a rerun
    # with no document change and no prompt change, that is all a second answer
    # can be. Block 26 is three bullets ("Create Beneficary accounts", "Create
    # Electronic trading account with defined settings", "Auto approval of
    # compliance"); successive runs at @v4 turned them into eight requirements,
    # each a differently-worded guess at the subject the fragment omits. None
    # scored high enough to match, because none of them quote the block: they
    # are the model's inferences, and an inference is re-invented every time.
    #
    # So idempotence is established by construction rather than by similarity.
    # Same document version, same prompt version, work already done: skip.
    # Bumping the prompt version re-decomposes deliberately, and reconciliation
    # handles that case -- which is the same discipline the detectors already
    # follow, where `detected_by` carries the version and a bump retires what
    # the old version said.
    already = _blocks_already_decomposed(
        session, ctx, bid_id=bid_id, extracted_by=extracted_by, pairs=pairs
    )
    if already:
        report.blocks_already_decomposed = len(already)
        pairs = [(block, resolution) for block, resolution in pairs if block.ordinal not in already]
        if not pairs:
            # Nothing new to READ is not nothing to DO.
            #
            # This early return skipped the reconciliation passes at the end of
            # the function, and that is why R-044 survived every re-run. The
            # bump to v11 re-decomposed every block on ONE run; from the next
            # run onwards every block was already done, so the pass that would
            # have retired v10's reading of that clause was never reached. A
            # defect only the first run after a bump can fix is a defect nobody
            # can fix, because by the time anyone notices, that run is over.
            #
            # Both passes are idempotent and neither calls a model, so running
            # them on a no-op run costs two queries and settles the register.
            report.stale_readings_retired = retire_stale_clause_readings(
                session, ctx, bid_id=bid_id, extracted_by=extracted_by
            )
            report.page_break_folds = fold_page_break_duplicates(session, ctx, bid_id=bid_id)
            return report
    covered_ordinals: set[int] = set()
    empty_ordinals: set[int] = set()

    preceding = _preceding_text(session, ctx, pairs=pairs)
    table_headers = _table_headers(session, ctx, pairs=pairs)

    for batch in _batches(pairs):
        payload = {
            "clause_blocks": [
                {
                    "ordinal": block.ordinal,
                    "clause_ref": block.clause_ref,
                    "heading_path": block.heading_path or [],
                    "block_type": block.block_type.value,
                    "page_from": block.page_from,
                    # A row of a compliance matrix is unreadable without the
                    # table it belongs to: "Requirement: ... | Compliance:
                    # (blank)" says nothing about which matrix, and the header
                    # row is usually in an earlier batch.
                    **({"table_name": block.table_name} if block.table_name else {}),
                    **({"row_index": block.row_index} if block.row_index else {}),
                    # The column names this row's cells are labelled with. See
                    # `_table_headers`.
                    **(
                        {"table_header": table_headers[block.ordinal]}
                        if block.ordinal in table_headers
                        else {}
                    ),
                    "text": block.text,
                    # Context only, and only where the block cannot stand alone.
                    # See `_preceding_text`.
                    **(
                        {"continues_from": preceding[block.ordinal]}
                        if block.ordinal in preceding
                        else {}
                    ),
                }
                for block, _ in batch
            ]
        }
        result = _decompose(
            model_gateway,
            payload["clause_blocks"],
            ctx=ctx,
            session=session,
            report=report,
        )

        allowed = {block.ordinal for block, _ in batch}
        _validate_ordinals(result, allowed)

        assignments = _reconcile_against_existing(
            session,
            ctx,
            bid_id=bid_id,
            proposals=result.requirements,
            block_by_ordinal=block_by_ordinal,
        )

        for proposal in result.requirements:
            # A restatement the same answer already made. Skipped entirely: it
            # is not a second obligation, and persisting it as one is what put
            # five requirements on a paragraph that states two. The block still
            # counts as covered, because it was -- by the first statement.
            if assignments.get(id(proposal)) is _ECHO_OF:
                covered_ordinals.add(proposal.source_block_ordinal)
                report.echoes_dropped += 1
                continue
            block, resolution = block_by_ordinal[proposal.source_block_ordinal]
            created = _persist_requirement(
                session,
                ctx,
                bid_id=bid_id,
                proposal=proposal,
                block=block,
                resolution=resolution,
                extracted_by=extracted_by,
                report=report,
                assignment=assignments.get(id(proposal), _UNASSIGNED),
            )
            covered_ordinals.add(proposal.source_block_ordinal)
            if created:
                report.requirements_created += 1
            else:
                report.requirements_updated += 1

        empty_ordinals.update(o for o in result.no_requirement_block_ordinals if o in allowed)

        # Record the reading, for every block in the batch -- not only the ones
        # that produced something. A block the model read and judged empty is
        # the case this record exists for; writing only the productive ones
        # would reproduce the bug it fixes.
        yields: dict[int, int] = defaultdict(int)
        for proposal in result.requirements:
            yields[proposal.source_block_ordinal] += 1
        for block, _resolution in batch:
            _record_extraction(
                session,
                ctx,
                bid_id=bid_id,
                block=block,
                extracted_by=extracted_by,
                found=yields.get(block.ordinal, 0),
            )

    report.blocks_covered = len(covered_ordinals)
    report.blocks_explicitly_empty = len(empty_ordinals - covered_ordinals)

    # The cross-check: a block the model neither turned into a requirement nor
    # explicitly cleared is a gap, not a silence.
    #
    # `covered_ordinals` records only what THIS RUN processed, and a rerun skips
    # every block already decomposed under the current prompt. So on the second
    # run of an unchanged document almost nothing is "covered", and this raised
    # a gap for clause after clause that had produced requirements months
    # earlier. The live tender carried 47 of them, 43 linked to the very
    # requirements their text says do not exist:
    #
    #   "Clause 14 governs this tender but produced neither a requirement nor an
    #    explicit decision that it imposes none."   -> R-014, R-143, R-144
    #
    # A finding that contradicts its own link teaches a reader to skip the whole
    # column. What the check is actually for is a clause nothing has ever
    # accounted for, so it now asks the database rather than this run's tally:
    # requirements standing against the block, or a recorded reading that found
    # none, both mean accounted for.
    unaccounted = set(block_by_ordinal) - covered_ordinals - empty_ordinals
    still_gaps: set[int] = set()
    for ordinal in sorted(unaccounted):
        block, _resolution = block_by_ordinal[ordinal]
        if _requirements_from_block(session, ctx, bid_id=bid_id, block=block):
            continue
        if _block_read_and_found_nothing(session, ctx, bid_id=bid_id, block=block):
            continue
        still_gaps.add(ordinal)

    # Close the ones that are no longer true.
    #
    # Safe here in a way the ambiguity retirement was not: this check is
    # deterministic and visits every block of the document on every run, so
    # "not raised this time" really does mean "no longer a gap". There is no cap
    # and no sampling to mistake for a verdict.
    # What accounted for each clause, per finding rather than one line for all
    # of them: a reader looking at a closed gap could not tell a resolved one
    # from a dropped one.
    notes: dict[str, str] = {}
    for ordinal in sorted(set(block_by_ordinal) - still_gaps):
        block, _res = block_by_ordinal[ordinal]
        clause = block.clause_ref or f"block {ordinal}"
        found = _requirements_from_block(session, ctx, bid_id=bid_id, block=block)
        codes = [requirement.code for requirement, _how in found]
        notes[f"coverage:{clause}:{ordinal}"] = (
            f"Clause {clause} is accounted for by {', '.join(sorted(codes))}."
            if codes
            else f"Clause {clause} was read and found to impose no requirement."
        )
    resolved = clarifications.close_resolved_coverage_gaps(
        session,
        ctx,
        bid_id=bid_id,
        detected_by="requirement_coverage_check@v1",
        still_open={
            f"coverage:{block_by_ordinal[o][0].clause_ref or f'block {o}'}:{o}" for o in still_gaps
        },
        reason="The clause now has requirements, or a recorded reading that found none.",
        notes=notes,
    )
    if resolved:
        log.info("coverage_gaps_resolved", bid_id=str(bid_id), closed=resolved)

    for ordinal in sorted(still_gaps):
        block, _resolution = block_by_ordinal[ordinal]
        clause = block.clause_ref or f"block {ordinal}"
        report.coverage_gaps.append(clause)
        clarifications.raise_issue(
            session,
            ctx,
            bid_id=bid_id,
            issue_type=ClarificationIssueType.COVERAGE_GAP,
            description=(
                f"Clause {clause} governs this tender but produced neither a requirement "
                "nor an explicit decision that it imposes none. It must be reviewed "
                "manually before drafting."
            ),
            severity=ClarificationSeverity.MAJOR,
            blocks_generation=False,
            recommended_question=None,
            source_a=_locator(block),
            detected_by="requirement_coverage_check@v1",
            fingerprint=f"coverage:{clause}:{ordinal}",
        )

    report.stale_readings_retired = retire_stale_clause_readings(
        session, ctx, bid_id=bid_id, extracted_by=extracted_by
    )
    report.page_break_folds = fold_page_break_duplicates(session, ctx, bid_id=bid_id)
    _flag_work_that_depends_on(session, ctx, bid_id=bid_id, amended=report.amended)
    audit.record(
        session,
        ctx,
        action="requirements.extracted",
        entity_type="bid",
        entity_id=bid_id,
        bid_id=bid_id,
        after=report.as_dict(),
    )
    return report


# How near two requirements must sit to be the same clause read twice, in
# characters of the source document. A page break splits one clause across two
# blocks that are adjacent in the text, so the span is small; two genuinely
# similar obligations from different sections are not.
PAGE_BREAK_SPAN = 4_000
# Every word of the shorter requirement appearing in the longer. Not a
# similarity score: containment. "Jadwa will provide access to Core OMS for the
# following purpose: create beneficiary accounts; create electronic trading
# account" adds nothing to a requirement that already lists those purposes and
# two more.
CONTAINMENT = 0.98


def retire_stale_clause_readings(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, extracted_by: str
) -> int:
    """Withdraw an older prompt's reading of a clause the current one has re-read.

    A prompt bump re-decomposes every block, and the new reading is the
    authoritative one -- that is the whole point of bumping. Matching usually
    pairs each new proposal with the row it replaces, but not always: v11 read
    "Integration Requirements / 7" and produced R-081, naming the product and
    the SDK URL that v10's R-044 had dropped. The two were too differently
    worded to pair, so both stayed live, and the register mixed one stale v10
    row in with 74 v11 rows -- the same obligation twice, evidenced twice, and
    no record saying which was current.

    Narrow by design. Only a requirement on the SAME qualified clause of the
    same document, only when that clause also holds a reading by the current
    prompt version, and only when the stale one came from an earlier version of
    THIS call type -- never from a person, and never from a clause the current
    run did not reach. Anything broader would let a prompt that got worse
    silently delete what a better one had found.
    """
    call_type = extracted_by.rsplit("@", 1)[0]
    # Grouped by DOCUMENT, not document version.
    #
    # The first version of this keyed on source_document_version_id and
    # retired nothing: R-044 (v10) and R-081 (v11) are both "Integration
    # Requirements / 7", but they hang off different VERSIONS of the same
    # uploaded file, because the document was parsed again between the two
    # runs. A re-parse does not make it a different clause -- "clause 7 of the
    # integration requirements in this file" is the same obligation whichever
    # parse found it, which is the whole idea of a qualified clause ref.
    rows = list(
        session.execute(
            select(Requirement, RequirementVersion, DocumentVersion.document_id)
            .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
            .join(
                DocumentVersion,
                DocumentVersion.id == RequirementVersion.source_document_version_id,
            )
            .where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
                RequirementVersion.clause_ref.is_not(None),
            )
        )
    )
    by_clause: dict[tuple, list] = defaultdict(list)
    for requirement, version, document_id in rows:
        by_clause[(document_id, version.clause_ref)].append((requirement, version))

    retired = 0
    for (_document, clause), group in by_clause.items():
        current = [r for r, v in group if v.extracted_by == extracted_by]
        if not current:
            continue
        stale = [
            r
            for r, v in group
            if (v.extracted_by or "").startswith(f"{call_type}@") and v.extracted_by != extracted_by
        ]
        for requirement in stale:
            withdraw(
                session,
                ctx,
                requirement=requirement,
                successors=[r.id for r in current],
                reason=(
                    f"Clause {clause} has been read again by {extracted_by}. "
                    f"{requirement.code} is that clause as an earlier prompt version read it, "
                    "and the current reading of the same clause replaces it."
                ),
            )
            retired += 1
            log.info(
                "requirement_stale_reading_retired",
                bid_id=str(bid_id),
                code=requirement.code,
                clause=clause,
                superseded_by=[r.code for r in current],
            )
    return retired


def fold_page_break_duplicates(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID
) -> int:
    """Withdraw a requirement whose whole content is already in a neighbour.

    The page-break duplicate. A clause runs over the foot of a page, the parser
    gives its two halves separate blocks, and each is decomposed on its own --
    so the tail of the list becomes a second requirement saying a subset of
    what the first says. On the live tender R-033 listed five purposes for the
    Core OMS integration and R-034 listed three of the same five, and both went
    to the matrix, to be evidenced twice and answered twice.

    Deliberately not `_similarity`: two obligations can be 0.9 alike and be two
    obligations. What makes this one safe is CONTAINMENT plus NEARNESS -- every
    word of the shorter is in the longer, and they come from the same document
    a few thousand characters apart. A clause that really does restate its
    neighbour in full is a clause said once.

    The survivor is the longer text, because the shorter is missing part of the
    list. The loser is withdrawn through `withdraw`, so its clarification links
    move to the survivor and its assessment stops claiming to be current.
    """
    rows = list(
        session.execute(
            select(Requirement, RequirementVersion)
            .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
            .where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
            )
        )
    )

    def start_of(version: RequirementVersion) -> int | None:
        locator = version.source_locator
        return locator.get("char_start") if isinstance(locator, dict) else None

    folded = 0
    withdrawn: set[uuid.UUID] = set()
    for index, (requirement, version) in enumerate(rows):
        if requirement.id in withdrawn:
            continue
        for other, other_version in rows[index + 1 :]:
            if other.id in withdrawn:
                continue
            if version.source_document_version_id != other_version.source_document_version_id:
                continue
            here, there = start_of(version), start_of(other_version)
            if here is None or there is None or abs(here - there) > PAGE_BREAK_SPAN:
                continue
            mine = set(_comparable(version.requirement_text).split())
            theirs = set(_comparable(other_version.requirement_text).split())
            if not mine or not theirs:
                continue
            # The shorter one is the candidate for folding; if they are the
            # same length nothing is contained in anything.
            shorter, longer = (
                (other, requirement) if len(theirs) < len(mine) else (requirement, other)
            )
            small, large = (theirs, mine) if len(theirs) < len(mine) else (mine, theirs)
            if len(small) == len(large):
                continue
            if len(small & large) / len(small) < CONTAINMENT:
                continue
            withdraw(
                session,
                ctx,
                requirement=shorter,
                successors=[longer.id],
                reason=(
                    f"Every obligation in {shorter.code} is already stated in {longer.code}, "
                    "which covers the same clause. The two were produced from one clause "
                    "split across a page break."
                ),
            )
            withdrawn.add(shorter.id)
            folded += 1
            log.info(
                "requirement_page_break_folded",
                bid_id=str(bid_id),
                folded=shorter.code,
                into=longer.code,
            )
            if shorter.id == requirement.id:
                break
    return folded


def _flag_work_that_depends_on(
    session: Session, ctx: ExecutionContext, *, bid_id: uuid.UUID, amended: list
) -> int:
    """Mark tasks and approved answers built on reworded requirements for revalidation.

    One impact per amending document, naming every requirement it changed.
    `amendments.propagate` does the marking -- stale assessments, tasks and
    artifacts to REVALIDATION_REQUIRED -- and is idempotent per document, so a
    rerun changes nothing further.
    """
    if not amended:
        return 0
    from app.models import Document, DocumentVersion
    from app.services import amendments

    by_document: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for requirement_id, document_version_id in amended:
        if requirement_id not in by_document[document_version_id]:
            by_document[document_version_id].append(requirement_id)
    flagged = 0
    for document_version_id, requirement_ids in by_document.items():
        version = session.get(DocumentVersion, document_version_id)
        document = session.get(Document, version.document_id) if version else None
        name = document.original_filename if document else "a superseding document"
        amendments.propagate(
            session,
            ctx,
            bid_id=bid_id,
            amendment_document_version_id=document_version_id,
            affected_requirement_ids=requirement_ids,
            reason=(
                f"{name} changed the wording of {len(requirement_ids)} requirement(s). "
                "Work approved against the earlier wording must be checked again."
            ),
        )
        flagged += 1
    return flagged


def _validate_ordinals(result: RequirementDecompositionResult, allowed: set[int]) -> None:
    used = {r.source_block_ordinal for r in result.requirements}
    used.update(result.no_requirement_block_ordinals)
    invalid = used - allowed
    if invalid:
        values = ", ".join(str(v) for v in sorted(invalid))
        raise ValidationError(
            f"requirement_decomposition cited block ordinals outside its input batch: {values}"
        )


def _locator(block: DocumentBlock) -> dict:
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


# The bar for a match made INSIDE an assignment, which is lower than the bar for
# a standalone pairwise decision because the assignment knows more: each existing
# requirement can be claimed once, and the best-scoring pairs are taken first, so
# a proposal is not compared against one candidate in isolation but placed among
# all of them.
#
# 0.75 admits the re-quotings the live bid produced while excluding the pair
# that must never merge on its own -- the two Tradenet obligations, which score
# 0.804 as clauses and are held apart by the higher `_SAME_OBLIGATION` bar. An
# unmatched candidate is left untouched rather than deleted, so the cost of a
# wrong pairing here is a wording change recorded as drift, not a lost
# obligation.
#
# The figures that used to be quoted here (0.751, 0.703) were measured while the
# matcher was comparing a stored CLAUSE against a proposal's RESTATEMENT -- see
# `_match_text`. They described the skew, not the texts, and are gone rather
# than restated, because a threshold justified by numbers from a broken
# measurement is a threshold nobody can check.
_POSITIONAL_FLOOR = 0.75


SEPARATE_PRICE = re.compile(
    # "Qoute Separate" is how the live tender spells it. A marker this cheap to
    # miss is read from the clause and its heading rather than asked of the
    # model: the cost of missing it is a separately-quoted system folded into
    # the fixed price.
    r"\b(?:q[uo]{2}te[ds]?\s+separate(?:ly)?|separate\s+q[uo]{2}te|priced?\s+separately"
    r"|separate\s+(?:price|pricing|line\s+item))\b",
    re.IGNORECASE,
)


def priced_separately(block) -> bool:
    """Whether the clause says this item is quoted on its own."""
    headings = [h or "" for h in (getattr(block, "heading_path", None) or [])]
    text = " ".join([getattr(block, "text", "") or ""] + headings)
    return bool(SEPARATE_PRICE.search(text))


def _decompose(
    gateway,
    entries: list[dict],
    *,
    ctx,
    session,
    report,
    depth: int = 0,
):
    """One decomposition call, split in half if the model cannot answer it.

    The reply is capped at 8,192 tokens and the repair loop re-sends the SAME
    batch, so a batch whose answer does not fit can only fail -- three paid
    calls and then a ProviderError that aborted the entire analysis. A prose
    tender of ten pages never reached it; a dense or table-heavy one would.
    Half the blocks produce half the answer, so splitting is the one repair
    that can work.
    """
    import json

    from app.core.errors import ProviderError
    from app.gateway.registry import wrap_untrusted
    from app.schemas.tender_extraction import RequirementDecompositionResult

    try:
        result = gateway.generate_structured(
            "requirement_decomposition",
            wrap_untrusted(json.dumps({"clause_blocks": entries}, ensure_ascii=False, default=str)),
            ctx=ctx,
            session=session,
        )
        if not isinstance(result, RequirementDecompositionResult):
            raise ValidationError("requirement_decomposition returned an unexpected schema.")
        report.model_calls += 1
        return result
    except (ProviderError, ValidationError):
        if len(entries) == 1 or depth >= 3:
            # One block that cannot be answered is one block, recorded and
            # stepped over. Losing the rest of the tender to it is the worse
            # outcome, and the coverage check will report it as unaccounted.
            report.blocks_unreadable += 1
            log.warning(
                "decomposition_block_unreadable",
                ordinal=entries[0].get("ordinal") if entries else None,
            )
            return RequirementDecompositionResult()
        middle = len(entries) // 2
        report.batches_split += 1
        log.info("decomposition_batch_split", blocks=len(entries), depth=depth)
        first = _decompose(
            gateway, entries[:middle], ctx=ctx, session=session, report=report, depth=depth + 1
        )
        second = _decompose(
            gateway, entries[middle:], ctx=ctx, session=session, report=report, depth=depth + 1
        )
        return RequirementDecompositionResult(
            requirements=list(first.requirements) + list(second.requirements),
            no_requirement_block_ordinals=(
                list(first.no_requirement_block_ordinals)
                + list(second.no_requirement_block_ordinals)
            ),
        )


def _match_text(proposal) -> str:
    """The words a proposal is judged by. The clause, never the restatement.

    A stored requirement carries two texts: `requirement_text`, the clause as
    the document states it, and `normalized_requirement`, the model's own
    restatement of it. Both read like "the requirement text", and the matcher
    used one on each side -- `_requirements_from_block` returns the stored
    CLAUSE, while every proposal was reduced to its RESTATEMENT before scoring.
    So every comparison was a clause against a paraphrase.

    That is not a small skew. R-055 is a byte-for-byte copy of R-050 on both
    fields and scored 0.748 against it -- two thousandths under the assignment
    floor -- because "for the following purpose: Create Beneficiary accounts"
    was being matched against "for creating beneficiary accounts". A row could
    not match a perfect copy of itself, so a rerun HAD to mint a new one, and
    which duplicates appeared was decided by where each pair happened to fall
    either side of 0.75.

    Comparing restatement to restatement would be consistent and still wrong.
    R-024 and R-025 -- "create beneficiary accounts" and "create electronic
    trading accounts", two distinct Tradenet obligations -- score 0.879 as
    restatements, above the merge bar, because the restatement compresses away
    the words that tell them apart. As clauses they score 0.804. Taking the
    better of the two pairings has the same effect, so it is not done either.

    Identity is judged on the clause. It is the document's own wording, it
    cannot drift between runs the way a regenerated restatement does, it is
    never NULL, and it is the text a person reads in the matrix -- so a
    similarity score can now be checked by reading the two rows.

    The restatement keeps its own jobs: it is stored, and the qualified-clause
    branch of `_find_existing` still matches it exactly against a stored
    restatement. That comparison is like-for-like and stays.
    """
    return (proposal.requirement_text or "").strip()


# Distinguishes "no match was found" from "reconciliation was not attempted",
# which the caller must tell apart: the first means create, the second means
# fall back to `_find_existing`.
_UNASSIGNED = object()

# A proposal the same model answer already made in different words. Not
# persisted at all: it is not a second obligation, and recording it as one is
# how a ten-page tender grew two extra requirements per run.
_ECHO_OF = object()


def _reconcile_against_existing(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    proposals,
    block_by_ordinal: dict,
) -> dict[int, object]:
    """Match a rerun's proposals to what each block already produced.

    Thresholded matching alone is not enough to make a rerun idempotent, and
    the live bid showed why. `_SAME_OBLIGATION` is set high on purpose -- an
    obligation merged away is unrecoverable -- so a restatement that scores 0.81
    is deliberately NOT matched, and becomes a second row. Do that on every
    analysis and the bid accumulates: 38 rows became 47, then 50, and two more
    (R-051, R-052) appeared after a cleanup had just removed ten.

    What the assignment knows that a pairwise score does not is that each
    existing requirement can be claimed once. A block holding two requirements
    can absorb at most two proposals however they are worded, so the remainder
    -- and only the remainder -- is genuinely new. That cap does the work a
    threshold was doing badly, which is why the bar here (`_POSITIONAL_FLOOR`)
    can sit below `_SAME_OBLIGATION` without risking a merge: the alternative to
    matching is not deletion but a second row, and an unmatched candidate is
    left exactly as it was.

    Assignment is greedy best-first, which is not optimal in general but is
    exact for the case that matters (one or two obligations per block) and is
    trivially explicable, which a Hungarian solver here would not be.

    Numbered clauses are left alone: they have a stable reference of their own
    and `_find_existing` handles them.
    """
    by_block: dict[int, list] = defaultdict(list)
    for proposal in proposals:
        by_block[proposal.source_block_ordinal].append(proposal)

    assignments: dict[int, object] = {}
    for ordinal, group in by_block.items():
        entry = block_by_ordinal.get(ordinal)
        if entry is None:
            continue
        block, _resolution = entry
        # Numbered blocks used to be exempt here, on the reasoning that a clause
        # reference is a stable identity of its own. It is a stable identity for
        # a CLAUSE. It is not one for an obligation, and a numbered clause in
        # this tender routinely states several.
        #
        # What the exemption did: every proposal from clause "Business
        # Requirements / 4" fell through to `_find_existing`, matched the single
        # requirement carrying that reference, and wrote a new version of it. A
        # clause with five bullets therefore produced one requirement holding
        # whichever bullet was processed last -- and a different one next run.
        # R-020 reached version 95 that way, rotating between the SMS bullets;
        # R-018 alternated between "Verify Information" and "Get applicant
        # related more information". Any task or evidence attached to those
        # codes silently came to mean something else.
        #
        # The other four bullets never became requirements at all. That is why
        # the SMS functions, the SQL Server database, the IIS configuration, the
        # cross-site-scripting controls and the load test are absent from the
        # matrix: they collided with a sibling bullet on the same clause number
        # and lost.
        #
        # So the reference now SCOPES the candidates rather than being the
        # identity, and assignment runs for numbered and unnumbered blocks
        # alike: each existing requirement can be claimed once, and the surplus
        # is genuinely new. Several requirements may share a clause reference,
        # which is what the document says.

        # First: is the model repeating ITSELF, in this one answer?
        #
        # It does. One paragraph came back as five requirements -- the design
        # obligation stated twice and the screen list stated twice -- and
        # nothing noticed, because matching only ever compared a proposal
        # against requirements already stored. Two proposals in the same reply
        # were never compared to each other, so both were "new", and the ones
        # that could not claim an existing row became R-053 and R-054.
        #
        # Judged at the high standalone bar rather than the assignment floor.
        # An assignment is safe at 0.75 because each existing requirement can be
        # claimed once and the alternative to matching is a visible extra row;
        # here the alternative is DROPPING a proposal outright, so the two
        # statements have to be near-identical before one is discarded. On this
        # bid that admits 0.884 and 0.958 while leaving the Tradenet obligations
        # at 0.804 and 0.745 alone.
        kept: list = []
        for proposal in group:
            text = _match_text(proposal)
            twin = next(
                (
                    other
                    for other, other_text in kept
                    if _similarity(other_text, text) >= _SAME_OBLIGATION
                ),
                None,
            )
            if twin is not None:
                # Follows whatever the first statement is assigned to, so a
                # repeat updates the same requirement instead of creating one.
                assignments[id(proposal)] = _ECHO_OF
                continue
            kept.append((proposal, text))
        group = [proposal for proposal, _ in kept]

        candidates = _requirements_from_block(session, ctx, bid_id=bid_id, block=block)
        if not candidates:
            for proposal in group:
                assignments[id(proposal)] = None
            continue

        scored = []
        for proposal in group:
            # `existing` is the stored requirement_text, so this must be the
            # proposal's requirement_text too. The two sides of every
            # similarity call draw the same field; that is the whole fix.
            text = _match_text(proposal)
            for requirement, existing in candidates:
                scored.append((_similarity(existing, text), id(proposal), requirement))
        scored.sort(key=lambda row: -row[0])

        used_proposals: set[int] = set()
        used_requirements: set = set()
        for score, proposal_id, requirement in scored:
            if proposal_id in used_proposals or requirement.id in used_requirements:
                continue
            if score < _POSITIONAL_FLOOR:
                continue
            assignments[proposal_id] = requirement
            used_proposals.add(proposal_id)
            used_requirements.add(requirement.id)
        for proposal in group:
            assignments.setdefault(id(proposal), None)
    return assignments


def _persist_requirement(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    proposal,
    block: DocumentBlock,
    resolution: ClauseResolution | None,
    extracted_by: str,
    report: ExtractionReport,
    assignment: object = _UNASSIGNED,
) -> bool:
    """Create a requirement, or add a version when the wording has changed.

    A wording change creates version N+1 and marks N superseded. The requirement
    itself stays CONFIRMED with the same identity and code, so every task,
    artifact and assessment that already references it keeps pointing at the
    right thing.
    """
    normalized = (proposal.normalized_requirement or proposal.requirement_text).strip()
    # `_reconcile_against_existing` has already decided for unnumbered blocks,
    # where it can see the whole batch at once. `_UNASSIGNED` means it declined
    # (a numbered clause), so the per-proposal matcher answers instead.
    if assignment is _UNASSIGNED:
        # Two texts, two different jobs, and they are passed separately so the
        # call site says which is which: `normalized` is matched for EQUALITY
        # against a stored restatement (like against like), `match_text` is
        # SCORED against a stored clause. Collapsing them into one argument is
        # how a clause came to be compared with a paraphrase.
        existing = _find_existing(
            session,
            ctx,
            bid_id=bid_id,
            block=block,
            normalized=normalized,
            match_text=_match_text(proposal),
        )
    else:
        existing = cast("Requirement | None", assignment)

    if existing is None:
        requirement = Requirement(
            org_id=ctx.org_id,
            bid_id=bid_id,
            code=_next_code(session, ctx, bid_id=bid_id),
            category=proposal.category,
            subcategory=proposal.subcategory,
            obligation_owner=resolved_obligation_owner(
                proposal.requirement_text, proposal.obligation_owner
            ),
            is_mandatory=proposal.is_mandatory,
            is_scored=proposal.is_scored,
            evaluation_weight=proposal.evaluation_weight,
            is_conditional=proposal.is_conditional,
            priced_separately=priced_separately(block),
            condition_expression={"raw": proposal.condition_text}
            if proposal.is_conditional
            else None,
            applicability=RequirementApplicability.CONDITIONAL_PENDING
            if proposal.is_conditional
            else RequirementApplicability.ACTIVE,
            lifecycle_state=RequirementLifecycle.DRAFT_EXTRACTED,
            risk=proposal.risk or RiskLevel.MEDIUM,
        )
        session.add(requirement)
        session.flush()
        _add_version(
            session,
            ctx,
            requirement=requirement,
            proposal=proposal,
            block=block,
            resolution=resolution,
            extracted_by=extracted_by,
            change_reason=None,
        )
        _flag_placeholder(session, ctx, bid_id=bid_id, requirement=requirement, block=block)
        return True

    current = session.get(RequirementVersion, existing.current_version_id)
    if current is not None and same_wording(current.requirement_text, proposal.requirement_text):
        # Same wording, so no new version — but the CLASSIFICATION may still have
        # moved, and re-running the analysis is exactly how a reviewer asks for
        # it to be reconsidered. Returning here without refreshing made adding
        # `obligation_owner` a silent no-op on every existing requirement: the
        # column shipped, the prompt shipped, the re-run cost a full batch of
        # model calls, and all 58 rows kept the default. Wording and
        # classification are different questions and only the first needs a
        # version.
        _refresh_classification(session, existing, proposal)
        # And record WHO confirmed it. Identical wording means the newer prompt
        # read this block and produced the same requirement -- agreement, which
        # is the best outcome a re-extraction can have. Leaving the stamp at the
        # older version made that outcome indistinguishable from "the newer
        # prompt found nothing here", and the sweep that retires superseded
        # extractions proposed withdrawing exactly the rows v6 had just
        # confirmed: the B2B reconciliation obligation, the Windows 2012
        # deployment requirement, the NDA notification. Every one at 100%
        # coverage, every one real.
        #
        # The wording's history is not lost -- superseded RequirementVersion
        # rows keep their own stamps. This records the latest reader to vouch
        # for the current text.
        if current.extracted_by != extracted_by:
            current.extracted_by = extracted_by
        return False

    # Why the wording changed decides what this IS, and the two cases are not
    # the same event. A superseding document restating clause 6.1 is an
    # amendment: same obligation, new words, and the requirement's identity
    # rightly carries over. The same document analysed a second time is not —
    # nothing superseded anything, the model simply decomposed the block
    # differently, and the code that tasks and artifacts already cite may now
    # denote a different obligation.
    #
    # Observed live. Re-running the analysis on unchanged documents rewrote
    # R-003 from "responses to clarifications would be annexed to the contract"
    # to "the Firm must prepare the number of copies as indicated" — a different
    # obligation under the same code — and recorded it as an amendment by a
    # superseding document that does not exist.
    re_extracted = (
        current is not None and current.source_document_version_id == block.document_version_id
    )
    _add_version(
        session,
        ctx,
        requirement=existing,
        proposal=proposal,
        block=block,
        resolution=resolution,
        extracted_by=extracted_by,
        change_reason=(
            "Re-extracted from the same source document version: the wording of this "
            "requirement changed without the tender changing."
            if re_extracted
            else "Wording changed by a superseding tender document."
        ),
        previous=current,
    )
    if not re_extracted:
        report.amended.append((existing.id, block.document_version_id))
    if (
        re_extracted
        and current is not None
        and not is_benign_rewording(current.requirement_text, proposal.requirement_text)
    ):
        _flag_identity_drift(
            session,
            ctx,
            bid_id=bid_id,
            requirement=existing,
            block=block,
            before=current.requirement_text,
            after=proposal.requirement_text,
        )
    # Identity is preserved: only the version is superseded.
    _refresh_classification(session, existing, proposal)
    return False


def _canonical(text: str | None) -> str:
    """Words only, case-folded: what is left when spacing, case and punctuation go."""
    return " ".join(re.findall(r"\w+", (text or "").casefold()))


def same_wording(a: str | None, b: str | None) -> bool:
    """The same requirement, said again.

    A trailing full stop is not a new version. It was one: nine drift items were
    opened on a re-run, several of them a final period, a stray space or a capital
    letter, each a MAJOR "this code may now mean something else" for a code that
    means exactly what it did.
    """
    return _canonical(a) == _canonical(b)


def is_benign_rewording(before: str | None, after: str | None) -> bool:
    """A new version whose meaning did not move, so nobody needs to look.

    Two shapes. The same words (see `same_wording`), and one text sitting inside
    the other at nearly its full length -- a section label such as "SSL Support:"
    dropped from the front. Nearly, because containment alone would call a
    narrowing benign: a row that used to carry "check the client exists AND
    create the account" and now says only "the customer will provide access" has
    lost obligations, and that is exactly what the drift item is for.
    """
    x, y = _canonical(before), _canonical(after)
    if not x or not y:
        return False
    if x == y:
        return True
    shorter, longer = (x, y) if len(x) <= len(y) else (y, x)
    return shorter in longer and len(shorter) / len(longer) >= 0.6


def _refresh_classification(session: Session, requirement: Requirement, proposal) -> None:
    """Bring a requirement's classification up to date with a fresh extraction.

    Deliberately not the wording, the code, the lifecycle state or anything a
    person has decided about the requirement — only the labels the extractor
    owns. A later human override of `obligation_owner` will need a guard here;
    there is no such override today, and re-extraction is currently the only
    thing that sets it.
    """
    requirement.category = proposal.category
    requirement.is_mandatory = proposal.is_mandatory
    requirement.is_scored = proposal.is_scored
    requirement.obligation_owner = resolved_obligation_owner(
        proposal.requirement_text, proposal.obligation_owner
    )
    # This session runs with autoflush off, so an unflushed update is invisible
    # to the next SELECT in the same transaction.
    session.flush()


# Re-exported so this module's existing callers and tests keep working, but the
# definition now lives in one place shared with `precedence`. Having a private
# copy here is exactly how the two drifted: this module was fixed and precedence
# kept keying on the bare reference, which is where the clauses were being lost.
qualified_clause_ref = _qualified_clause_ref


# Above this, two statements from one block are the same obligation re-phrased,
# and the later one updates the earlier rather than creating a row beside it.
#
# Set deliberately HIGH, and the asymmetry is the reason. A duplicate
# requirement is visible: it sits in the matrix, a person can see two rows
# saying the same thing, and a cleanup can merge them. An obligation wrongly
# folded into another is gone -- nothing downstream can recover a requirement
# that was never recorded, and the bid silently stops covering it. So this
# errs toward keeping both, and anything between "clearly the same" and
# "clearly different" is left for a human rather than decided here.
#
# 0.90, raised from 0.85, and the margin is why.
#
# 0.85 was set when the highest score between two genuinely different
# obligations was 0.796 -- three Tradenet clauses sharing a long stem. That
# margin has since been eaten twice over, by fixes that were each correct on
# their own terms:
#
#   * carrying a list's introducing phrase into every one of its items (so a
#     bullet reads on its own) makes siblings share a long identical prefix.
#     R-072 and R-073 -- beneficiary accounts, electronic trading accounts --
#     now score 0.825;
#   * decomposing numbered clauses fully surfaced the SMS clause as four
#     obligations, and R-115 vs R-117 -- "send validation SMS" against "send
#     announcement SMS" -- scores 0.848.
#
# Two thousandths from a bar whose effect is to DISCARD a proposal outright.
# Not to merge it, not to flag it: echo collapse drops the later statement, so
# crossing this line loses an obligation with nothing left to show it existed.
#
# The asymmetry decides the direction. A real echo that no longer collapses
# becomes one visible duplicate row that a person can merge. A distinct
# obligation that wrongly collapses is gone from the bid.
#
# The window is narrow, and 0.95 was tried first and was wrong: a real
# restatement of the design clause measures 0.938, so that bar would have
# stopped collapsing genuine echoes. Every labelled pair on this tender:
#
#     distinct    0.848 (SMS validation vs announcement)   <- must NOT collapse
#     distinct    0.825 (beneficiary vs trading accounts)
#     echo        0.884 (screen list, re-worded)
#     echo        0.938 (design approach, re-worded)        <- must collapse
#     echo        0.958 (screen list again)
#
# 0.90 sits in the gap, five points above the worst distinct pair and thirty-
# eight above the two thousandths that 0.85 left. The 0.884 echo now survives as
# a visible duplicate, which is the cost being accepted on purpose.
#
# This threshold is a stopgap for a judgement that does not belong to character
# arithmetic. "Are these the same obligation" is a question about meaning, and
# the numbers above are what it looks like when a similarity ratio is asked to
# answer it: correct fixes elsewhere keep pushing distinct clauses toward the
# bar, and each time the response is to move the bar.
_SAME_OBLIGATION = 0.90


def _comparable(text: str) -> str:
    """Wording reduced to what a rerun cannot change: letters, digits, spaces.

    Punctuation is replaced BEFORE whitespace is collapsed. The other order
    turns "requirements -- confidential" into a run of spaces that survives into
    the comparable form, so two texts differing only in punctuation compare
    unequal -- which is the whole thing this function exists to prevent. Caught
    by its own test, which is the only reason the order is written down here.
    """
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _similarity(a: str, b: str) -> float:
    """How alike two statements of an obligation are, 0 to 1.

    `autojunk=False` is not a detail. `difflib` discards any element appearing
    in more than 1% of a sequence longer than 200 characters -- for characters
    that means spaces and common letters, and every requirement text is longer
    than 200 characters. With it left on, two plainly identical obligations
    scored 0.089:

        "The vendor must follow a design approach similar to the current design
         of www.jadwa.com and create a new design for this system..."
        "The bidder must follow the design approach of the current
         www.jadwa.com website and create a new design for the portal..."

    The same pair scores 0.848 with it off. The heuristic exists to make source
    diffs fast, is wrong for prose, and fails silently -- a low score looks
    exactly like a genuine mismatch.

    Token overlap is taken alongside and the larger wins: the sequence ratio is
    sensitive to reordering, which a restatement does constantly, and the token
    set is not.

    An earlier version weighted words by how well they distinguished the
    candidates from one block, which separated the hard cases beautifully on
    the full corpus and then moved as the number of candidates changed -- so a
    block's second run scored differently from its fifth, which is unusable for
    a decision that must be stable. Plain scoring with a high threshold is worse
    at merging and never wrong about it.
    """
    left, right = _comparable(a), _comparable(b)
    if not left or not right:
        return 0.0
    ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
    lt, rt = set(left.split()), set(right.split())
    jaccard = len(lt & rt) / len(lt | rt) if (lt | rt) else 0.0
    return max(ratio, jaccard)


def _requirements_from_block(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    block: DocumentBlock,
) -> list[tuple[Requirement, str]]:
    """Live requirements this block could already have produced.

    Two ways in, and the second one is why an amendment does not fork the bid.

    **Same block.** The exact character span inside the same document version.
    The locator is JSONB and the candidate set for one version is tens of rows,
    so the span is compared in Python rather than in SQL; heading_path and
    clause_ref inside the same locator are derived and can move, the span
    cannot.

    **Same clause, different document version.** When an amendment supersedes a
    clause, the winning block moves into the amendment's version while the
    requirement's current version still points at the RFP's. Scoping candidates
    to one `document_version_id` therefore found nothing, every proposal looked
    new, and the amendment created a SECOND live requirement on the same clause
    instead of a second version of the first: "Test 25 applications" and "Test
    35 applications", both current, both answerable, one of them retracted by
    the customer. A response would have answered the clause twice with
    contradictory numbers.

    That is precisely what `Requirement.code`, `RequirementVersion.version_no`
    and `superseded_at` exist to prevent, and it made the whole amendment
    lifecycle a duplicate factory -- the one input the versioning design was
    built for.

    The qualified clause reference is the bridge, because it survives the move:
    "4.2 Testing scope / 4.2" in the RFP and in the amendment are the same
    clause. Note it SCOPES candidates and is not an identity -- one clause
    routinely states several obligations, and R-020 reached version 95 by
    treating a clause number as an obligation's name. The caller's greedy
    assignment still lets each existing requirement be claimed once, so a
    surplus proposal is new and a matched one is a new version.
    """
    from app.services.clause_identity import qualified_clause_ref

    rows = session.execute(
        select(
            Requirement,
            RequirementVersion.requirement_text,
            RequirementVersion.source_locator,
            RequirementVersion.source_document_version_id,
            RequirementVersion.clause_ref,
        )
        .join(RequirementVersion, RequirementVersion.requirement_id == Requirement.id)
        .where(
            Requirement.org_id == ctx.org_id,
            Requirement.bid_id == bid_id,
            Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
            RequirementVersion.superseded_at.is_(None),
        )
    ).all()

    clause = qualified_clause_ref(block)
    out: list[tuple[Requirement, str]] = []
    seen: set = set()
    for requirement, text, locator, version_id, version_clause in rows:
        if requirement.id in seen:
            continue
        same_block = (
            version_id == block.document_version_id
            and isinstance(locator, dict)
            and locator.get("char_start") == block.char_start
            and locator.get("char_end") == block.char_end
        )
        # `clause` may be None for an unnumbered block, and None == None would
        # then gather every other unnumbered requirement in the bid into one
        # contest. Only a real reference bridges versions.
        same_clause = clause is not None and version_clause == clause
        if same_block or same_clause:
            out.append((requirement, text))
            seen.add(requirement.id)
    return out


def _find_existing(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    block: DocumentBlock,
    normalized: str,
    match_text: str,
) -> Requirement | None:
    """Is this block an obligation we already have, or a new one?

    `normalized` is the proposal's restatement, used for the exact-equality
    check against a stored restatement in the qualified-reference branch.
    `match_text` is the proposal's clause, used for the similarity score
    against a stored clause. They are different texts for different questions;
    passing one where the other belongs is the defect `_match_text` documents.

    `match_text` is REQUIRED, and deliberately has no default. It was briefly
    written as `match_text: str | None = None`, falling back to `normalized` --
    which meant any caller that forgot the argument silently got the broken
    cross-field comparison back, with nothing to show for it but a slightly
    wrong number. The replay script forgot it within the hour and reported
    "would create a new row" against a fixed pipeline. A missing argument must
    be a TypeError, not a quiet 0.748.

    Three failures lived here, and all of them were silent -- the extraction
    reported success, the count was simply wrong.

    The first was matching on a BARE clause reference. Numbering restarts in
    every section of this tender, so Business 3 matched Technical 3 and one
    overwrote the other; R-006 passed through five unrelated obligations that
    way, and tasks and drafts kept pointing at whichever meaning arrived last.
    References are now qualified by their section.

    The second was worse and had no visible symptom at all. The fallback ran
    `clause_ref == block.clause_ref` unconditionally, and for a block with no
    reference SQLAlchemy renders that as `clause_ref IS NULL` -- which matches
    ANY requirement that also lacks one. Most obligations in a tender are prose
    and carry no number (`_CLAUSE_RE` only fires when the text opens with one),
    so every unnumbered obligation after the first collapsed onto it. A ten-page
    document with thirty-odd obligations yielded fourteen requirements, and the
    twenty that vanished left no trace: each was recorded as a new VERSION of an
    unrelated requirement.

    The absence of a reference is not an identity. When a block has no clause
    reference the wording is all there is to go on, so that is what is matched.
    """
    reference = qualified_clause_ref(block)

    if reference is None:
        # No reference: identity is WHERE THE TEXT CAME FROM, not how the model
        # phrased it this time.
        #
        # The previous rule matched on `normalized_requirement`, a field the
        # model writes. Re-analysing an unchanged document produced the same
        # obligation in slightly different words -- "using existing components
        # such as" one run, "reusing existing components including" the next;
        # "the RFP requirements" then "the RFP content" -- so nothing matched
        # and every rerun minted a fresh requirement beside the old one. A bid
        # went 38 -> 47 rows with no new obligations in the document, and the
        # duplicates then drew their own evidence and their own assessments,
        # inflating every count downstream.
        #
        # A block is a stable coordinate: same document version, same character
        # span, same obligation. The model cannot perturb it. So candidates are
        # drawn by source first, and the wording is used only to tell apart the
        # several obligations one block can decompose into.
        candidates = _requirements_from_block(session, ctx, bid_id=bid_id, block=block)
        if not candidates:
            return None
        # `text` is the stored CLAUSE. Scoring it against the restatement made
        # this branch unable to recognise an exact copy: R-055 scored 0.748
        # against a row identical to it in every field.
        against = match_text.strip()
        best, score = None, 0.0
        for requirement, text in candidates:
            ratio = _similarity(text, against)
            if ratio > score:
                best, score = requirement, ratio
        # Above the threshold it is the same obligation re-worded; below it, the
        # block decomposed into something genuinely different and a new
        # requirement is right. 0.75 accepts the paraphrases seen live and still
        # separates two distinct obligations lifted from one paragraph.
        return best if score >= _SAME_OBLIGATION else None

    by_clause = session.execute(
        select(Requirement)
        .join(RequirementVersion, RequirementVersion.requirement_id == Requirement.id)
        .where(
            Requirement.org_id == ctx.org_id,
            Requirement.bid_id == bid_id,
            Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
            RequirementVersion.clause_ref == reference,
            RequirementVersion.normalized_requirement == normalized,
        )
        .limit(1)
    ).scalar_one_or_none()
    if by_clause is not None:
        return by_clause

    # Same qualified reference, different wording: the customer amended it.
    return session.execute(
        select(Requirement)
        .join(RequirementVersion, RequirementVersion.requirement_id == Requirement.id)
        .where(
            Requirement.org_id == ctx.org_id,
            Requirement.bid_id == bid_id,
            Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
            RequirementVersion.clause_ref == reference,
            RequirementVersion.superseded_at.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()


def _add_version(
    session: Session,
    ctx: ExecutionContext,
    *,
    requirement: Requirement,
    proposal,
    block: DocumentBlock,
    resolution: ClauseResolution | None,
    extracted_by: str,
    change_reason: str | None,
    previous: RequirementVersion | None = None,
) -> RequirementVersion:
    next_no = (
        session.execute(
            select(func.coalesce(func.max(RequirementVersion.version_no), 0)).where(
                RequirementVersion.org_id == ctx.org_id,
                RequirementVersion.requirement_id == requirement.id,
            )
        ).scalar_one()
        + 1
    )
    version = RequirementVersion(
        org_id=ctx.org_id,
        requirement_id=requirement.id,
        version_no=next_no,
        requirement_text=proposal.requirement_text.strip(),
        normalized_requirement=(
            proposal.normalized_requirement or proposal.requirement_text
        ).strip(),
        source_document_version_id=block.document_version_id,
        source_locator=_locator(block),
        # The qualified reference, so what is matched on and what is displayed
        # are the same string. A bare number in this column was also unusable to
        # a reader: "3" does not say which section's 3.
        clause_ref=qualified_clause_ref(block),
        # None where the clause was never contested. The column is nullable
        # for exactly this case, and demanding a resolution here is what tied
        # extraction to precedence in the first place.
        clause_resolution_id=resolution.id if resolution is not None else None,
        change_reason=change_reason,
        extracted_by=extracted_by,
    )
    session.add(version)
    session.flush()

    if previous is not None:
        previous.superseded_at = datetime.now(timezone.utc)
    requirement.current_version_id = version.id
    session.flush()

    if change_reason:
        audit.record(
            session,
            ctx,
            action="requirement.version_added",
            entity_type="requirement",
            entity_id=requirement.id,
            bid_id=requirement.bid_id,
            before={"version_no": previous.version_no if previous else None},
            after={
                "version_no": next_no,
                "lifecycle_state": requirement.lifecycle_state.value,
            },
            reason=change_reason,
        )
    return version


def _flag_placeholder(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    requirement: Requirement,
    block: DocumentBlock,
) -> None:
    match = PLACEHOLDER_RE.search(block.text)
    if not match:
        return
    clarifications.raise_issue(
        session,
        ctx,
        bid_id=bid_id,
        issue_type=ClarificationIssueType.PLACEHOLDER,
        description=(
            f"Requirement {requirement.code} derives from clause text containing an "
            f"unresolved placeholder ({match.group(0)!r})."
        ),
        severity=ClarificationSeverity.MAJOR,
        blocks_generation=False,
        recommended_question=(
            f"Please confirm the intended value in clause {block.clause_ref or ''}".strip() + "."
        ),
        source_a=_locator(block),
        detected_by="requirement_placeholder_check@v1",
        fingerprint=f"placeholder:{requirement.code}",
        requirement_ids=[requirement.id],
    )


def _flag_identity_drift(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    requirement: Requirement,
    block: DocumentBlock,
    before: str,
    after: str,
) -> None:
    """Raise a review item when a re-extraction changed what a code means.

    Silence is the dangerous option here. A requirement code is cited by
    assessments, department tasks and artifact statements, so a code that
    quietly comes to mean something else breaks every one of those references
    without breaking anything visibly. This does not block the re-extraction —
    a second pass is often better than the first — it makes the drift a thing
    somebody has to look at.
    """
    clarifications.raise_issue(
        session,
        ctx,
        bid_id=bid_id,
        issue_type=ClarificationIssueType.INCOMPLETE_SCOPE,
        description=(
            f"Requirement {requirement.code} was re-extracted from an unchanged source "
            f"document and now reads differently. Before: {before[:200]!r}. "
            f"After: {after[:200]!r}. Anything already citing {requirement.code} may now "
            "point at a different obligation."
        ),
        severity=ClarificationSeverity.MAJOR,
        blocks_generation=False,
        recommended_question=None,
        source_a=_locator(block),
        detected_by="requirement_identity_drift@v1",
        # One item per requirement per wording, so re-running the analysis again
        # does not pile up duplicates of the same drift.
        fingerprint=f"drift:{requirement.code}:{hashlib.sha256(after.encode()).hexdigest()[:16]}",
        # Linked, now that linking and gating are separate questions.
        #
        # This was deliberately UNLINKED, and the reason was sound: a linked
        # clarification used to make a requirement read "needs client
        # clarification", and a note about our own re-extraction is nothing
        # anyone can ask the customer. Linking it once put 51 of 59 rows into
        # that state and the matrix said nothing else.
        #
        # The gate now turns on `recommended_question` -- whether there is
        # something a customer could actually answer -- and this detector raises
        # none, by design. So the drift note reaches the row it concerns,
        # appears in its open-clarification count, and cannot claim the bid is
        # waiting on anybody.
        requirement_ids=[requirement.id],
    )


# How many clauses may go into one review request. The whole point is that this
# is ONE call, so the cap exists only to stop a 400-requirement tender building
# a prompt too long to answer well; past it, the review runs in batches.
OBLIGATION_REVIEW_BATCH = 40
# Clause text is truncated in the prompt. Who acts is settled by the opening
# words of a clause in every case this has been tested against.
OBLIGATION_REVIEW_CHARS = 1200


def review_obligation_owners(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> dict[str, int]:
    """Give the ambiguous clauses a call of their own.

    Decomposition answers a dozen questions about a clause at once and the owner
    is one field among them. On the live bid that produced a split result: the
    customer's rights in the contract section were labelled CUSTOMER, and the
    same class of clause among the submission instructions was labelled BIDDER.
    Not a hard judgement — a crowded one.

    So the clauses a deterministic rule can settle are settled without a model
    at all, and the rest go to a call that asks one question. Cheap because it is
    batched: one request for a whole tender, against roughly sixty for the
    evidence check that follows it.

    Nothing here can excuse an obligation the text plainly assigns: the same
    backstop runs on the answer, and a clause the review does not mention keeps
    the label it already had.
    """
    model_gateway = gateway or build_gateway()
    rows = list(
        session.execute(
            select(Requirement)
            .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
            .where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
            )
            .order_by(Requirement.code)
        ).scalars()
    )

    pending: list[tuple[Requirement, str]] = []
    settled = 0
    for requirement in rows:
        version = session.get(RequirementVersion, requirement.current_version_id)
        text = (version.requirement_text if version else "") or ""
        if _BIDDER_ACTOR.search(text):
            # The text names the bidder as the actor. There is nothing to weigh
            # up, and asking anyway would spend a model on a decided question.
            if requirement.obligation_owner is not ObligationOwner.BIDDER:
                requirement.obligation_owner = ObligationOwner.BIDDER
            settled += 1
            continue
        pending.append((requirement, text))

    changed = 0
    calls = 0
    failed_batches = 0
    failed_clauses = 0
    for start in range(0, len(pending), OBLIGATION_REVIEW_BATCH):
        batch = pending[start : start + OBLIGATION_REVIEW_BATCH]
        lines = [f"{len(batch)} clauses. Answer for every number."]
        for index, (_, text) in enumerate(batch, start=1):
            lines.append(f"\n[{index}]")
            lines.append(wrap_untrusted(text.strip()[:OBLIGATION_REVIEW_CHARS]))
        try:
            result = model_gateway.generate_structured(
                "obligation_owner_review",
                "\n".join(lines),
                ctx=ctx,
                session=session,
                # Deterministic read at temperature zero over fixed clause text:
                # re-analysing an unchanged tender should not buy this twice.
                cache=True,
            )
        except Exception as exc:  # noqa: BLE001 - a review failure keeps the labels
            log.warning("obligation_review_unavailable", reason=type(exc).__name__)
            # `break` abandoned every remaining batch while the summary still
            # reported all of them reviewed: on a long tender a failure in
            # batch 2 of 13 read as a complete pass.
            failed_batches += 1
            failed_clauses += len(batch)
            continue
        calls += 1
        for verdict in result.verdicts:
            if not 1 <= verdict.clause <= len(batch):
                continue
            requirement, text = batch[verdict.clause - 1]
            owner = resolved_obligation_owner(text, verdict.owner)
            if owner is not requirement.obligation_owner:
                requirement.obligation_owner = owner
                changed += 1
    session.flush()

    summary = {
        "reviewed": len(pending) - failed_clauses,
        "not_reviewed": failed_clauses,
        "failed_batches": failed_batches,
        "settled_without_a_call": settled,
        "relabelled": changed,
        "calls": calls,
    }
    log.info("obligation_review_completed", **summary)
    return summary


def recheck_customer_owned(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> dict[str, int]:
    """Second look at the clauses labelled as the customer's.

    Labelling a bidder obligation as the customer's takes a real obligation out of the
    compliance matrix, so that is the one direction worth re-asking. Only CUSTOMER -> BIDDER can
    happen here, and the stored label changes only when the model, under the current prompt,
    says BIDDER. Cheap: one call over the customer-owned clauses, cached by prompt version.
    """
    model_gateway = gateway or build_gateway()
    rows = list(
        session.execute(
            select(Requirement)
            .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
            .where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
                Requirement.obligation_owner == ObligationOwner.CUSTOMER,
            )
            .order_by(Requirement.code)
        ).scalars()
    )
    pending = []
    for requirement in rows:
        version = session.get(RequirementVersion, requirement.current_version_id)
        pending.append((requirement, (version.requirement_text if version else "") or ""))
    flipped = 0
    for start in range(0, len(pending), OBLIGATION_REVIEW_BATCH):
        batch = pending[start : start + OBLIGATION_REVIEW_BATCH]
        lines = [f"{len(batch)} clauses. Answer for every number."]
        for index, (_, text) in enumerate(batch, start=1):
            lines.append(f"\n[{index}]")
            lines.append(wrap_untrusted(text.strip()[:OBLIGATION_REVIEW_CHARS]))
        try:
            result = model_gateway.generate_structured(
                "obligation_owner_review", "\n".join(lines), ctx=ctx, session=session, cache=True
            )
        except Exception as exc:  # noqa: BLE001 - the labels stay as they were
            log.warning("owner_recheck_unavailable", reason=type(exc).__name__)
            continue
        for verdict in result.verdicts:
            if not 1 <= verdict.clause <= len(batch):
                continue
            requirement, text = batch[verdict.clause - 1]
            if verdict.owner is ObligationOwner.BIDDER:
                requirement.obligation_owner = ObligationOwner.BIDDER
                flipped += 1
    session.flush()
    return {"checked": len(pending), "now_bidder": flipped}


def confirm_requirements(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    requirement_ids: list[uuid.UUID] | None = None,
) -> int:
    """A person accepts that these are what the customer asked for.

    This is the gate that was missing. Evidence needs a knowledge administrator,
    an artifact version needs a reviewer, a proposed commitment needs a named
    role -- and the model's reading of the CLAUSE ITSELF needed nobody. Every
    one of those other approvals approves an answer to a question that was never
    checked, so a misread clause carries the whole governed chain along with it.

    `CONFIRMED` existed for this and was read by nothing: every query in the
    codebase filters on `!= WITHDRAWN`, so a requirement stayed DRAFT_EXTRACTED
    forever and it changed nothing. `requirement_scope.py` opens by noting that
    `lifecycle_state` once had a WITHDRAWN value nothing read; this was the
    leftover of the same problem.

    Two changes make it an approval rather than a formality.

    **It records WHO.** The old version promoted every row on the bid and wrote
    an audit entry saying how many, so "who accepted R-042, and when" had no
    answer -- which is the only question an approval exists to settle.

    **It takes a subset.** Confirming 89 requirements in one call is a rubber
    stamp whatever it records. `requirement_ids` lets a reviewer accept the rows
    they have actually read; omitting it keeps the bid-wide behaviour for the
    case where somebody genuinely means all of them.
    """
    stmt = select(Requirement).where(
        Requirement.org_id == ctx.org_id,
        Requirement.bid_id == bid_id,
        Requirement.lifecycle_state == RequirementLifecycle.DRAFT_EXTRACTED,
    )
    if requirement_ids is not None:
        if not requirement_ids:
            return 0
        stmt = stmt.where(Requirement.id.in_(requirement_ids))
    rows = session.execute(stmt).scalars().all()
    now = datetime.now(timezone.utc)
    for requirement in rows:
        requirement.lifecycle_state = RequirementLifecycle.CONFIRMED
        requirement.confirmed_by = ctx.actor_user_id
        requirement.confirmed_at = now
    session.flush()
    if rows:
        audit.record(
            session,
            ctx,
            action="requirements.confirmed",
            entity_type="bid",
            entity_id=bid_id,
            bid_id=bid_id,
            after={
                "confirmed": len(rows),
                "codes": sorted(r.code for r in rows)[:50],
                "scope": "listed" if requirement_ids is not None else "whole bid",
            },
        )
    return len(rows)


def set_applicability(
    session: Session,
    ctx: ExecutionContext,
    *,
    requirement_id: uuid.UUID,
    applicability: RequirementApplicability,
    reason: str,
) -> Requirement:
    if not (reason or "").strip():
        raise ValidationError("Changing applicability requires a reason.")
    requirement = session.get(Requirement, requirement_id)
    if requirement is None or requirement.org_id != ctx.org_id:
        raise NotFound("Requirement not found.")

    before = requirement.applicability.value
    requirement.applicability = applicability
    requirement.applicability_reason = reason
    session.flush()
    audit.record(
        session,
        ctx,
        action="requirement.applicability_changed",
        entity_type="requirement",
        entity_id=requirement.id,
        bid_id=requirement.bid_id,
        before={"applicability": before},
        after={"applicability": applicability.value},
        reason=reason,
    )
    return requirement


def evaluate_conditions(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    variables: dict[str, object],
    gateway: ModelGateway | None = None,
) -> dict[str, int]:
    """Resolve CONDITIONAL_PENDING requirements against known bid variables.

    A condition that cannot be evaluated stays pending. It is never guessed into
    ACTIVE (which would invent an obligation) or NOT_APPLICABLE (which would
    hide one).
    """
    pending = (
        session.execute(
            select(Requirement).where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                Requirement.is_conditional.is_(True),
                Requirement.applicability == RequirementApplicability.CONDITIONAL_PENDING,
            )
        )
        .scalars()
        .all()
    )
    counts = {"activated": 0, "not_applicable": 0, "still_pending": 0, "parsed": 0}
    model_gateway = gateway or build_gateway()

    for requirement in pending:
        expression = (requirement.condition_expression or {}).get("expression")
        if expression is None:
            raw = (requirement.condition_expression or {}).get("raw")
            if not raw:
                counts["still_pending"] += 1
                continue
            parsed = model_gateway.generate_structured(
                "condition_extraction",
                wrap_untrusted(raw),
                ctx=ctx,
                session=session,
            )
            if not isinstance(parsed, ConditionExtractionResult) or parsed.expression is None:
                counts["still_pending"] += 1
                continue
            expression = parsed.expression.as_json()
            requirement.condition_expression = {
                **(requirement.condition_expression or {}),
                "expression": expression,
            }
            counts["parsed"] += 1
            session.flush()

        verdict = _evaluate(expression, variables)
        if verdict is None:
            counts["still_pending"] += 1
            continue
        if verdict:
            set_applicability(
                session,
                ctx,
                requirement_id=requirement.id,
                applicability=RequirementApplicability.ACTIVE,
                reason=f"Condition satisfied: {expression.get('description', '')}".strip(),
            )
            counts["activated"] += 1
        else:
            set_applicability(
                session,
                ctx,
                requirement_id=requirement.id,
                applicability=RequirementApplicability.NOT_APPLICABLE,
                reason=f"Condition not met: {expression.get('description', '')}".strip(),
            )
            counts["not_applicable"] += 1
    return counts


def _evaluate(expression: dict, variables: dict[str, object]) -> bool | None:
    """Deterministic evaluation. Returns None when the variable is unknown."""
    variable = expression.get("variable")
    operator = expression.get("operator")
    expected = expression.get("value")
    if variable not in variables:
        return None
    actual = variables[variable]

    if operator == "EXISTS":
        return actual is not None
    if operator == "EQUALS":
        return actual == expected
    if operator == "NOT_EQUALS":
        return actual != expected
    if operator in ("GREATER_THAN", "LESS_THAN"):
        left, right = _as_number(actual), _as_number(expected)
        if left is None or right is None:
            # A comparison against something non-numeric is undecidable, not
            # false: returning False here would silently mark a requirement
            # inapplicable.
            return None
        return left > right if operator == "GREATER_THAN" else left < right
    return None


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def list_requirements(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    category=None,
    mandatory: bool | None = None,
    applicability: RequirementApplicability | None = None,
) -> list[Requirement]:
    stmt = select(Requirement).where(Requirement.org_id == ctx.org_id, Requirement.bid_id == bid_id)
    if category is not None:
        stmt = stmt.where(Requirement.category == category)
    if mandatory is not None:
        stmt = stmt.where(Requirement.is_mandatory.is_(mandatory))
    if applicability is not None:
        stmt = stmt.where(Requirement.applicability == applicability)
    return list(session.execute(stmt.order_by(Requirement.code)).scalars().all())


# --- how many the tender asks for -------------------------------------------

# Only clauses containing a number reach the model, which on an ordinary tender
# is a small minority and on some is none at all.
#
# Written out rather than `\d` alone because tenders say "three comparable
# projects" at least as often as "3". The word list stops at twelve on purpose:
# past that, tenders use figures, and a longer list buys nothing but false
# positives from "twenty-four hours".
_NUMERAL = re.compile(
    r"\d|\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
    re.IGNORECASE,
)

QUANTITY_REVIEW_BATCH = 40
# The count and its noun are stated together, early, in every clause tested
# against. The rest of a long clause is conditions on the thing being counted.
QUANTITY_REVIEW_CHARS = 1200


def review_requirement_quantities(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    gateway: ModelGateway | None = None,
) -> dict[str, int]:
    """Read the count each clause demands, and store it beside the wording.

    This is the reading half of the quantity rule. The checking half is
    arithmetic in `assessment`, and the split is the point: a model that is
    asked to both read a number and decide whether we have enough of it will
    answer the second question badly and invisibly. R-006 is the case -- one
    reference project against a demand for three, and the row read DRAFTABLE
    with "the gaps must be filled during drafting", which is true of a wording
    gap and false of two projects that do not exist.

    Cheap by construction. A deterministic filter drops every clause with no
    number in it, the rest go in one batched request, and the call is cached at
    temperature zero, so re-analysing an unchanged tender does not buy it twice.

    A failure here leaves every count exactly as it was. That is deliberate: no
    count means the shortfall rule does not fire, and not firing is the same
    behaviour the product had before this existed. The rule must never block a
    bid because a provider was down.
    """
    model_gateway = gateway or build_gateway()
    rows = list(
        session.execute(
            select(Requirement)
            .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
            .where(
                Requirement.org_id == ctx.org_id,
                Requirement.bid_id == bid_id,
                Requirement.lifecycle_state != RequirementLifecycle.WITHDRAWN,
            )
            .order_by(Requirement.code)
        ).scalars()
    )

    pending: list[tuple[RequirementVersion, str]] = []
    for requirement in rows:
        version = session.get(RequirementVersion, requirement.current_version_id)
        if version is None:
            continue
        text = version.requirement_text or ""
        if not _NUMERAL.search(text):
            # No number, no count. Settled without spending anything, and
            # settled correctly: a clause with no numeral cannot demand three
            # of something.
            continue
        pending.append((version, text))

    call = get_call_type("requirement_quantity")
    stamp = f"llm:{call.name}@{call.prompt_version}"
    counted = 0
    calls = 0
    failed_batches = 0
    failed_clauses = 0
    for start in range(0, len(pending), QUANTITY_REVIEW_BATCH):
        batch = pending[start : start + QUANTITY_REVIEW_BATCH]
        lines = [f"{len(batch)} clauses. Answer for every number."]
        for index, (_, text) in enumerate(batch, start=1):
            lines.append(f"\n[{index}]")
            lines.append(wrap_untrusted(text.strip()[:QUANTITY_REVIEW_CHARS]))
        try:
            result = model_gateway.generate_structured(
                "requirement_quantity",
                "\n".join(lines),
                ctx=ctx,
                session=session,
                cache=True,
            )
        except Exception as exc:  # noqa: BLE001 - a failure leaves the counts alone
            log.warning("quantity_review_unavailable", reason=type(exc).__name__)
            # One failed batch is not a reason to leave the rest uncounted, and
            # the summary must not report them as read.
            failed_batches += 1
            failed_clauses += len(batch)
            continue
        calls += 1
        for verdict in result.verdicts:
            if not 1 <= verdict.clause <= len(batch):
                continue
            version, _text = batch[verdict.clause - 1]
            # A count of one is the ordinary case, not a counting obligation,
            # and the CHECK constraint refuses it. Anything below two is stored
            # as "nothing to count", which is also what a NO verdict means.
            if verdict.is_evidence_count and verdict.required_count >= 2:
                version.required_count = verdict.required_count
                version.counted_noun = (verdict.counted_noun or "").strip()[:200] or None
                counted += 1
            else:
                version.required_count = None
                version.counted_noun = None
            version.quantity_by = stamp
    session.flush()

    summary = {
        "carried_a_number": len(pending) - failed_clauses,
        "not_reviewed": failed_clauses,
        "failed_batches": failed_batches,
        "settled_without_a_call": len(rows) - len(pending),
        "counting_obligations": counted,
        "calls": calls,
    }
    log.info("quantity_review_completed", **summary)
    return summary


# --- withdrawing a requirement, in one place --------------------------------


def move_clarification_links(
    session: Session,
    ctx: ExecutionContext,
    *,
    source: uuid.UUID,
    targets: list[uuid.UUID],
) -> int:
    """Re-point a withdrawn requirement's clarifications at what replaced it.

    Withdrawing without this leaves the join table addressing rows that appear
    nowhere: an export of 89 requirements cited 25 codes that no longer existed,
    across 64 open clarifications. The matrix counted only the resolvable links
    and looked consistent, which is the worst version of the failure -- the
    sheet a reviewer trusts silently omitted 130 associations.

    A question was raised about a BLOCK, and the block still has requirements:
    the ones being kept. So the link follows the clause onto every survivor,
    rather than being deleted (which loses a customer question over a
    bookkeeping change) or pinned to one arbitrary row.

    This lived in `scripts/retire_superseded_extractions.py` and was imported
    from there by `scripts/repair_withdrawn_links.py`, which is how a rule that
    belongs to the domain came to be owned by a maintenance script. Moved here
    because `withdraw` needs it and because a third caller would otherwise
    import from a script for a third time.
    """
    if not targets:
        return 0
    links = list(
        session.execute(
            select(ClarificationRequirement).where(
                ClarificationRequirement.org_id == ctx.org_id,
                ClarificationRequirement.requirement_id == source,
            )
        ).scalars()
    )
    if not links:
        return 0
    held: dict[uuid.UUID, set] = {}
    for target in targets:
        held[target] = {
            row.clarification_id
            for row in session.execute(
                select(ClarificationRequirement).where(
                    ClarificationRequirement.org_id == ctx.org_id,
                    ClarificationRequirement.requirement_id == target,
                )
            ).scalars()
        }

    moved = 0
    for link in links:
        placed = False
        for target in targets:
            if link.clarification_id in held[target]:
                continue
            # The join is unique on (clarification, requirement), so the first
            # survivor that does not already hold this question takes the
            # existing row and the rest get copies.
            if not placed:
                link.requirement_id = target
                placed = True
            else:
                session.add(
                    ClarificationRequirement(
                        org_id=ctx.org_id,
                        clarification_id=link.clarification_id,
                        requirement_id=target,
                    )
                )
            held[target].add(link.clarification_id)
            moved += 1
        if not placed:
            # Every survivor already carries this question. The link is then a
            # duplicate, not information.
            session.delete(link)
    session.flush()
    return moved


def retire_assessment(session: Session, ctx: ExecutionContext, *, requirement_id: uuid.UUID) -> int:
    """Stop a withdrawn requirement's verdict claiming to be current.

    `is_current` means "this is THE assessment of this requirement right now".
    Withdrawing the requirement left the assessment saying exactly that about a
    row nobody should be reading, and the live bid carried 69 of them against 89
    live requirements: BLOCKED read 81 unfiltered and 59 filtered, a 37%
    overstatement of the work outstanding.

    Nothing was corrupt -- one current row per requirement throughout -- and
    that is what made it dangerous. The export joins to the requirement and
    filters, so every number a person had seen was right, and the trap was set
    for whatever queried `is_current` next without knowing it had to.

    The row is NOT deleted. A withdrawn requirement's last verdict is part of
    how the bid got to where it is, and `version` plus `is_current = False` is
    exactly how every superseded assessment is already kept.
    """
    rows = list(
        session.execute(
            select(RequirementAssessment).where(
                RequirementAssessment.org_id == ctx.org_id,
                RequirementAssessment.requirement_id == requirement_id,
                RequirementAssessment.is_current.is_(True),
            )
        ).scalars()
    )
    for row in rows:
        row.is_current = False
    if rows:
        session.flush()
    return len(rows)


def move_task_links(
    session: Session,
    ctx: ExecutionContext,
    *,
    source: uuid.UUID,
    targets: list[uuid.UUID],
) -> int:
    """Point the tasks that covered `source` at its survivors, then drop the old link."""
    from app.models import TaskRequirement

    links = list(
        session.execute(
            select(TaskRequirement).where(
                TaskRequirement.org_id == ctx.org_id, TaskRequirement.requirement_id == source
            )
        ).scalars()
    )
    for link in links:
        for target in targets:
            exists = session.execute(
                select(TaskRequirement.id).where(
                    TaskRequirement.task_id == link.task_id,
                    TaskRequirement.requirement_id == target,
                )
            ).first()
            if exists is None:
                session.add(
                    TaskRequirement(org_id=ctx.org_id, task_id=link.task_id, requirement_id=target)
                )
        session.delete(link)
    if links:
        session.flush()
    return len(links)


def withdraw(
    session: Session,
    ctx: ExecutionContext,
    *,
    requirement: Requirement,
    successors: list[uuid.UUID] | None = None,
    reason: str | None = None,
) -> dict[str, int]:
    """Retire a requirement, and everything that pointed at it.

    Withdrawing is three facts, not one, and this codebase has now paid for
    getting that wrong twice:

      1. the requirement's own lifecycle and, where there is one, the row that
         replaced it;
      2. its clarification links, which must follow the clause onto the
         survivors or else address codes that no longer exist;
      3. its assessment, which must stop claiming to be current.

    Fact 2 was fixed in `coalesce_enumerated_fragments.py`, not in
    `retire_superseded_extractions.py` which does the same thing, and the
    identical failure reappeared a day later with 130 occurrences. Fact 3 was
    never fixed anywhere, and five separate call sites set
    `lifecycle_state = WITHDRAWN` by hand without it.

    So there is one function. A caller that withdraws a requirement without
    coming through here is the defect, not the exception, and the next reviewer
    can find every withdrawal by finding every caller of this.
    """
    successors = list(successors or [])
    before = str(getattr(requirement.lifecycle_state, "value", requirement.lifecycle_state))
    requirement.lifecycle_state = RequirementLifecycle.WITHDRAWN
    if successors and requirement.superseded_by_requirement_id is None:
        # The FIRST survivor is recorded as the successor, which is a
        # simplification the schema forces: one column, possibly several
        # survivors. The clarification links go to all of them, so nothing is
        # lost -- this column answers "what should I read instead", not "what
        # absorbed every part of this".
        requirement.superseded_by_requirement_id = successors[0]
    session.flush()

    moved = move_clarification_links(session, ctx, source=requirement.id, targets=successors)
    retired = retire_assessment(session, ctx, requirement_id=requirement.id)
    # Fact 4: the tasks that covered it. A task keeps answering a clause that no
    # longer exists, while the diagram and gap checks (which ignore withdrawn
    # clauses) see nothing -- the two halves of one screen then disagree. The
    # link follows the clause to its survivors, and is dropped where there are none.
    task_links_moved = move_task_links(session, ctx, source=requirement.id, targets=successors)

    audit.record(
        session,
        ctx,
        action="requirement.withdrawn",
        entity_type="requirement",
        entity_id=requirement.id,
        bid_id=requirement.bid_id,
        before={"lifecycle_state": before},
        after={
            "lifecycle_state": RequirementLifecycle.WITHDRAWN.value,
            "successors": [str(s) for s in successors],
            "clarification_links_moved": moved,
            "assessments_retired": retired,
            "task_links_moved": task_links_moved,
        },
        reason=reason,
    )
    return {
        "clarification_links_moved": moved,
        "assessments_retired": retired,
        "task_links_moved": task_links_moved,
    }
