"""Three kinds of backing, three places to record them.

A staffing question was answered entirely out of HR's written reply to a
cross-department request. The copilot cited it four times, as [R1], correctly
and in the right places. The line under the reply read "no sources cited".

Nothing threw. The citation regex matched [R1] happily; there was simply no
column it could be written to and no counter that read it, so a reply resting
on a governed cross-department answer displayed as a reply resting on nothing.
That is the worst available failure for this product specifically: the one
claim it makes is that every statement is attributable, and here the
attribution machinery reported failure at the exact moment it had worked.

This is the third bug of its shape in this area -- evidence cited outside the
authorized heading, [E#] emitted in a format the extractor did not read, and
now [R#] recorded nowhere. Each time two correct pieces met and nobody owned
the joint. So these tests assert the joint, not the pieces: that every ref the
prompt can emit has somewhere to land, and that the three kinds stay distinct.
"""

from __future__ import annotations

import uuid

from app.services.context_builder import TaskContext
from app.services.copilot import CITATION


def test_the_citation_pattern_matches_all_three_kinds():
    """[S#] retrieved, [E#] approved evidence, [R#] a colleague's answer. A
    pattern that misses one silently drops that kind of backing."""
    found = set(CITATION.findall("Backed by [S3], [E1] and [R1]."))
    assert found == {"S3", "E1", "R1"}


def test_a_context_can_hand_back_a_request_id_for_every_r_ref():
    """A citation with nothing behind it is a label. [R1] has to resolve to the
    request whose answer it is, or a reviewer cannot check the claim."""
    request_id = uuid.uuid4()
    context = TaskContext(
        task=None,
        sources=[],
        requirement_block="",
        retrieved_block="",
        evidence_block="",
        resolution_block="[R1] Asked HR: who has done this?\n  Answer: nobody on record.",
        trace={},
        resolution_refs={"R1": request_id},
    )
    assert context.resolution_refs["R1"] == request_id


def test_the_three_kinds_are_recorded_separately():
    """Folding resolutions into `cited_evidence_ids` would make a colleague's
    word indistinguishable from an item that passed an approval gate. They are
    weighed differently by a reviewer, so they are stored differently."""
    from app.models import ChatMessage

    columns = ChatMessage.__table__.c
    assert "cited_chunk_ids" in columns
    assert "cited_evidence_ids" in columns
    assert "cited_request_ids" in columns


def test_every_ref_the_prompt_can_emit_has_somewhere_to_land():
    """The joint itself, asserted against the declaration rather than the code.

    Every declared kind either names the column that records it or says why it
    records nowhere. A kind that does neither cannot be constructed at all --
    which is the difference between this and the version that only checked that
    somebody had typed the right variable name into `ask`.
    """
    from app.models import ChatMessage
    from app.services.citations import CONTEXT_KINDS

    columns = ChatMessage.__table__.c
    for kind in CONTEXT_KINDS:
        if kind.records_to is None:
            assert kind.why_unrecorded, f"{kind.key} records nothing and gives no reason"
        else:
            assert kind.records_to in columns, f"{kind.key} names a column that does not exist"


def test_a_resolution_line_names_the_department_not_its_id():
    """The first version printed `target_department_id`, so the model read
    "Asked 5901b49b-..." and any answer attributing a claim to its source
    attributed it to a UUID."""
    import inspect

    from app.services import context_builder

    source = inspect.getsource(context_builder._render_resolutions)
    assert "target_department_id}" not in source
    assert "names.get(" in source
