"""The same prompt must not decompose the same text twice.

Reconciliation was being asked to do something it cannot do. It can match a
proposal to an existing requirement; it cannot stop the model from splitting the
same paragraph a different way on a second reading. On a rerun with no document
change and no prompt change, a different answer is the only thing a second call
can contribute -- and every such answer that words an obligation differently
lands as a new row, because it no longer resembles the first one.

The live bid measured it. Block ordinal 26 holds three bullets and 145
characters:

    Create Beneficary accounts
    Create Electronic trading account with defined settings
    Auto approval of compliance based on new system approval

The bullets have no subject; the sentence that supplies it sits in another
block. Successive runs, all at `requirement_decomposition@v4`, turned those
three bullets into EIGHT requirements -- "The system must integrate with the
Tradenet Core OMS system to create beneficiary accounts", "The system must
create beneficiary accounts via the Tradenet Core OMS system", and so on. They
score 0.50 against each other, far below any matching threshold, because none of
them quotes the block: each is a fresh guess at the missing subject. No
similarity rule can collapse those, and no similarity rule should -- at 0.50 the
same rule would merge genuinely different obligations.

So idempotence is established by construction. Same document version, same
character span, same prompt version: the work is done, and the block is not sent
again. A prompt bump changes `extracted_by` and re-decomposes deliberately,
which is the discipline the detectors already follow.
"""

from __future__ import annotations

import uuid

from app.services.requirements import ExtractionReport, _blocks_already_decomposed

DOC = uuid.uuid4()
OTHER_DOC = uuid.uuid4()
V4 = "llm:requirement_decomposition@v4"
V5 = "llm:requirement_decomposition@v5"


class _Block:
    def __init__(self, ordinal, char_start, char_end, document_version_id=DOC):
        self.ordinal = ordinal
        self.char_start = char_start
        self.char_end = char_end
        self.document_version_id = document_version_id


class _Session:
    """Answers the two queries `_blocks_already_decomposed` makes.

    Requirement rows are (document_version_id, locator); extraction rows are
    (document_version_id, char_start, char_end). Each query filters
    `extracted_by` itself, so the stamp is not carried in the fixtures -- rows
    given here are ones that already matched.

    It asks `block_extraction` first -- the record that a prompt version READ a
    block, whatever it found -- and then falls back to requirements, for bids
    that predate that table. The two return different shapes, so the fake
    routes on the table named in the statement rather than on call order, which
    would silently mis-answer if the queries were ever reordered.

    Rows are supplied already filtered, as each query's WHERE clause leaves
    them: entries matching this bid and this `extracted_by`.
    """

    def __init__(self, requirement_rows, extraction_rows=()):
        self.requirement_rows = list(requirement_rows)
        self.extraction_rows = list(extraction_rows)

    def execute(self, statement):
        text = str(statement)
        rows = self.extraction_rows if "block_extraction" in text else self.requirement_rows

        class _Result:
            def all(self_inner):
                return rows

        return _Result()


class _Ctx:
    org_id = uuid.uuid4()


BID = uuid.uuid4()


def _call(rows, pairs, extraction_rows=()):
    return _blocks_already_decomposed(
        _Session(rows, extraction_rows), _Ctx(), bid_id=BID, extracted_by=V4, pairs=pairs
    )


def test_a_block_this_prompt_already_decomposed_is_skipped():
    """The whole point: block 26 is not sent to the model a second time."""
    block = _Block(26, 13144, 13289)
    assert _call([], [(block, None)], extraction_rows=[(DOC, 13144, 13289)]) == {26}


def test_an_untouched_block_is_still_decomposed():
    """Skipping must be per block, not per document. A run that added a block --
    or an earlier run that missed one -- must still reach it."""
    done = _Block(26, 13144, 13289)
    fresh = _Block(27, 13290, 13500)
    out = _call([], [(done, None), (fresh, None)], extraction_rows=[(DOC, 13144, 13289)])
    assert out == {26}


def test_a_prompt_bump_re_decomposes():
    """The escape hatch, and the reason this is safe.

    Improving the prompt is how extraction gets better. If a bump did not
    re-decompose, this guard would freeze every bad extraction in place for ever
    -- and the eight bullets on block 26 would be permanent.
    """
    block = _Block(26, 13144, 13289)
    # The stored work came from a version that is no longer current, so both
    # queries' `extracted_by == V5` filters return nothing.
    assert (
        _blocks_already_decomposed(
            _Session([], []), _Ctx(), bid_id=BID, extracted_by=V5, pairs=[(block, None)]
        )
        == set()
    )


def test_a_new_document_version_re_decomposes():
    """An amended document is new text at the same coordinates. Matching the
    span alone would skip it and the amendment would never be read."""
    block = _Block(26, 13144, 13289, document_version_id=OTHER_DOC)
    rows = [(DOC, {"char_start": 13144, "char_end": 13289})]
    assert _call(rows, [(block, None)]) == set()


def test_a_moved_span_re_decomposes():
    """Same document, different offsets: a different block, whatever else it
    resembles."""
    block = _Block(26, 13144, 13290)
    rows = [(DOC, {"char_start": 13144, "char_end": 13289})]
    assert _call(rows, [(block, None)]) == set()


def test_a_withdrawn_requirement_does_not_count_as_done():
    """Withdrawing a requirement must not make the block unreadable for ever.

    The real query excludes WITHDRAWN rows, so the block's only requirement
    disappearing leaves nothing behind -- and the block is decomposed again.
    Without this, a bad merge or an over-eager cleanup would permanently delete
    an obligation from the bid, which is precisely the unrecoverable failure
    this codebase keeps guarding against.
    """
    block = _Block(26, 13144, 13289)
    assert _call([], [(block, None)]) == set()


def test_a_malformed_locator_is_not_treated_as_done():
    """A row whose locator is not a dict tells us nothing about which block it
    came from. Treating it as a match would skip a block on no evidence."""
    block = _Block(26, 13144, 13289)
    assert _call([(DOC, None)], [(block, None)]) == set()


def test_a_skipped_block_still_counts_as_covered():
    """Coverage must not read as a collapse.

    A correct no-op rerun looks at nothing and creates nothing. If skipped
    blocks were left out of the coverage sum, that run would report a coverage
    ratio near zero and raise a gap against every block that is in fact already
    covered -- the readiness gate would then block generation because the
    extraction behaved correctly.
    """
    report = ExtractionReport(blocks_considered=10)
    report.blocks_already_decomposed = 10
    assert report.coverage_ratio == 1.0

    partial = ExtractionReport(blocks_considered=10)
    partial.blocks_already_decomposed = 6
    partial.blocks_covered = 2
    partial.blocks_explicitly_empty = 1
    assert partial.coverage_ratio == 0.9


def test_the_skip_is_reported_not_silent():
    """"Nothing was created" and "nothing was looked at" are different outcomes
    and must not print the same. The count is in the report payload."""
    report = ExtractionReport(blocks_considered=3)
    report.blocks_already_decomposed = 3
    assert report.as_dict()["blocks_already_decomposed"] == 3


def test_a_block_read_and_found_empty_is_still_done():
    """The case that made this table necessary.

    v6 read the tender's executive summary, applied its obligation test and
    rightly raised nothing -- "Jadwa is looking forward for an online portal" is
    background, not a duty. Inferring "already decomposed" from requirements
    alone made that block look unread, so it was re-decomposed on every
    subsequent run, spending a model call each time to reach the same correct
    answer.

    The extraction record says the reading happened; `found=0` says what it
    yielded. Both are facts, and only the first decides whether to look again.
    """
    block = _Block(3, 6274, 6865)
    extraction = [(DOC, 6274, 6865)]
    assert _call([], [(block, None)], extraction_rows=extraction) == {3}


def test_an_existing_requirement_does_not_mark_a_block_as_read():
    """The record is the only source, and this is why.

    Requirements used to count as evidence that their block had been read: a row
    stamped with the current prompt version meant "done". That fallback outranked
    the record, so clearing `block_extraction` to force a re-read -- after the
    matching rules changed -- cleared the table and changed nothing. Twenty-nine
    of the tender's forty-seven blocks were skipped, including every Integration
    and Technical clause, while the run reported success and the row count moved
    because the other eighteen blocks did get read.

    A bid extracted before the table existed now re-decomposes once. That is a
    bounded cost paid once, and it is worth far more than a control that says it
    has cleared something and has not.
    """
    block = _Block(26, 13144, 13289)
    # A live requirement from this block, carrying the current stamp -- the exact
    # shape that used to suppress the read.
    requirement_rows = [(DOC, {"char_start": 13144, "char_end": 13289})]
    assert _call(requirement_rows, [(block, None)], extraction_rows=[]) == set()


def test_clearing_the_record_really_does_force_a_re_read():
    """Stated as the operation rather than the query, because that is how it
    failed: the table was emptied and the block was still skipped."""
    block = _Block(41, 20000, 20114)
    assert _call([], [(block, None)], extraction_rows=[(DOC, 20000, 20114)]) == {41}
    # ...and with the record gone, nothing else may hold it back.
    assert _call([], [(block, None)], extraction_rows=[]) == set()
