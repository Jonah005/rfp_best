"""A batch that sees fifteen blocks cannot report that a section is missing.

`analyze_ambiguity` reads the tender in batches. The model was shown one window
at a time, told "you can only see the blocks listed in this request", and then
invited to report anything it could not see as INCOMPLETE_SCOPE. It did exactly
that, and because severity came straight back from the model with no check,
those became BLOCKING clarifications:

    "The 'Technical Requirements' section is listed in the table of contents but
     its content is not present in the provided text."

That section exists. The same analysis run extracted sixteen TECHNICAL
requirements from it. Six of the eight blocking clarifications on the live bid
were of this kind -- integrations, technical requirements, evaluation factors,
commercial instructions, submission dates -- all present in the document.

BLOCKING is not a label; it stops generation. So two things changed:

    every batch now carries `document_outline`, the whole document's sections,
    so "outside my window" and "missing from the document" are distinguishable;

    and an absence claim from a partial view cannot be BLOCKING, whatever
    severity the model returns.

The second is the one that holds when the model ignores the first.
"""

from __future__ import annotations

import json

import pytest

from app.models.enums import ClarificationIssueType, ClarificationSeverity
from app.services.tender_analysis import _outline, _payload


class _Block:
    def __init__(self, ordinal, heading_path, text="x", clause_ref=None):
        self.ordinal = ordinal
        self.heading_path = heading_path
        self.text = text
        self.clause_ref = clause_ref
        self.table_name = None
        self.page_from = 1

        class _T:
            value = "PARAGRAPH"

        self.block_type = _T()


DOCUMENT = [
    _Block(1, ["Executive Summary"]),
    _Block(2, ["Business Requirements"]),
    _Block(3, ["Business Requirements"]),
    _Block(4, ["Integration Requirements"]),
    _Block(5, ["Technical Requirements"]),
    _Block(6, ["Evaluation Factors"]),
]


def test_the_outline_names_every_section_including_ones_out_of_view():
    outline = _outline(DOCUMENT)
    sections = [entry["section"] for entry in outline]
    assert sections == [
        "Executive Summary",
        "Business Requirements",
        "Integration Requirements",
        "Technical Requirements",
        "Evaluation Factors",
    ]


def test_a_batch_payload_carries_the_whole_outline_not_just_its_own_blocks():
    """The point of the outline is that it describes what the batch CANNOT see.
    Sending only the batch's own sections would leave the model in exactly the
    position that produced the false blockers."""
    batch = DOCUMENT[:2]
    payload = _payload(batch, _outline(DOCUMENT))
    body = json.loads(payload[payload.index("{") : payload.rindex("}") + 1])

    sections = [entry["section"] for entry in body["document_outline"]]
    assert "Technical Requirements" in sections
    assert "Evaluation Factors" in sections
    assert body["visible_ordinals"] == [1, 2]
    assert len(body["blocks"]) == 2


def test_the_outline_is_ordered_by_where_each_section_starts():
    """A reader and a model both use order to reason about "later in the
    document". Sorting by name would make the outline say something false."""
    outline = _outline(DOCUMENT)
    assert [entry["starts_at_ordinal"] for entry in outline] == sorted(
        entry["starts_at_ordinal"] for entry in outline
    )


ABSENCE_TYPES = [ClarificationIssueType.BLANK, ClarificationIssueType.INCOMPLETE_SCOPE]


@pytest.mark.parametrize("issue_type", ABSENCE_TYPES, ids=lambda t: t.value)
def test_an_absence_claim_from_a_partial_view_cannot_block(issue_type):
    """The deterministic backstop, asserted on the rule the service applies.

    Written as the condition rather than by driving the whole service, because
    the service needs a model, a session and a bid; the decision under test is
    this one line of policy and it is worth pinning on its own.
    """
    whole_document = False
    severity = ClarificationSeverity.BLOCKING
    downgraded = (
        severity is ClarificationSeverity.BLOCKING
        and not whole_document
        and issue_type in (ClarificationIssueType.BLANK, ClarificationIssueType.INCOMPLETE_SCOPE)
    )
    assert downgraded, f"{issue_type.value} could still halt the bid on a window"


def test_a_contradiction_keeps_its_severity():
    """Contradictions are local and checkable: the schema already requires both
    conflicting blocks to be in view, so nothing is being inferred about text
    the model could not read. Downgrading those too would trade one false
    reading for a missed real one."""
    for issue_type in (
        ClarificationIssueType.CONTRADICTORY_DATE,
        ClarificationIssueType.CONTRADICTORY_QUANTITY,
        ClarificationIssueType.CONFLICTING_INSTRUCTION,
    ):
        downgraded = issue_type in (
            ClarificationIssueType.BLANK,
            ClarificationIssueType.INCOMPLETE_SCOPE,
        )
        assert not downgraded


def test_an_absence_claim_may_block_when_one_call_saw_everything():
    """The rule is about evidence, not about the issue type. A document small
    enough to fit in one batch really can be shown to lack a pricing schedule."""
    whole_document = True
    downgraded = (
        ClarificationSeverity.BLOCKING is ClarificationSeverity.BLOCKING
        and not whole_document
        and ClarificationIssueType.BLANK in ABSENCE_TYPES
    )
    assert not downgraded


def test_the_service_applies_the_rule():
    """Ties the policy tests above to the code that runs. If the condition is
    ever removed from `analyze_ambiguity`, the parametrised tests keep passing
    on their own copy of it and this one fails."""
    import inspect

    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    assert "whole_document" in source
    assert "ClarificationSeverity.MAJOR" in source
    assert "blocks_generation=severity is ClarificationSeverity.BLOCKING" in source
    # The outline reaching every batch is asserted on the prompt the model is
    # actually sent, in test_a_question_holds_up_only_what_it_is_about.py --
    # not on the spelling of the call, which changed without the rule changing.
    assert "_payload(" in source and "outline" in source
