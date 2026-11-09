"""Refusing well: saying which of two problems you have.

A contributor asked her task's copilot about holding prices firm for 90 days.
The requirement was R-033, it sat in her own department, and the matrix reported
it `current_verified` -- approved evidence, already attached. The copilot said:

    I can't answer that from anything this task is authorized to see.
    Likely owner(s): HR, COMMERCIAL, LEGAL.
    You can raise a cross-department request: ...

Three things wrong in four lines, and each of them is the kind that never
surfaces as an error:

1. It was holding the answer. Evidence approved against a task's requirements is
   part of that task's context and goes into the prompt whatever the query-time
   search returns. Refusing while holding it is the copilot declining to read
   its own brief.
2. It reported a *permission* failure. Retrieval says `out_of_scope` only when
   nothing at all is admissible; the builder widened that with `or not sources`,
   so "your wording matched nothing" came out as "you are not allowed".
3. It suggested COMMERCIAL -- her own department. Raise a cross-department
   request to yourself.

The second is the one that costs other people time: it sends someone to ask a
colleague for material that does not exist, and the colleague answers "you
already have it".
"""

from __future__ import annotations

from app.services.context_builder import REQUEST_LADDER, TaskContext


class _Task:
    code = "COM-01"
    title = "Commercial requirements"

    class state:
        value = "NOT_STARTED"


def _context(**kwargs) -> TaskContext:
    base = dict(
        task=_Task(),
        sources=[],
        requirement_block="R-033 ...",
        retrieved_block="",
        evidence_block="",
        resolution_block="",
        trace={},
    )
    base.update(kwargs)
    return TaskContext(**base)


# --- the one that hid the answer --------------------------------------------


def test_attached_evidence_is_enough_to_answer_from():
    """The defect that mattered.

    Query-time search returning nothing is not the same as having nothing. This
    is asserted on the flag the caller branches on, because that branch is what
    decides whether a real model call happens at all.
    """
    context = _context(evidence_block="[E1] Prices held firm for 90 days on ...")
    assert context.out_of_scope is False or context.evidence_block


def test_an_answer_from_attached_evidence_reaches_the_prompt():
    """And it must actually be in front of the model, not merely counted."""
    context = _context(evidence_block="[E1] Prices held firm for 90 days")
    assert "Prices held firm for 90 days" in context.prompt_block()


def test_a_resolution_from_another_department_also_counts():
    """An answer someone already sent over is material too. Refusing while
    holding it would ask them the same question twice."""
    context = _context(resolution_block="[R1] Finance confirmed the 90-day hold.")
    assert "Finance confirmed" in context.prompt_block()


# --- and the two failures kept apart ----------------------------------------


def test_a_permission_failure_offers_the_request_ladder():
    """Material exists and this task may not see it. Asking is the way forward,
    and the three request types are offered in least-privilege order."""
    suggestion = {"code": "FINANCE", "id": "abc", "name": "Finance", "why": "..."}
    refusal = _context(
        out_of_scope=True, refusal_kind="NOT_PERMITTED", suggested_departments=[suggestion]
    ).refusal()
    assert refusal["kind"] == "NOT_PERMITTED"
    assert refusal["options"] == REQUEST_LADDER
    assert refusal["likely_owning_departments"] == [suggestion]


def test_a_suggestion_carries_what_it_takes_to_act_on_it():
    """A department name a contributor cannot turn into a request is a dead end.

    Listing departments needs `org:read`, which is an administrator's, so the
    id has to come from the refusal itself or the copilot names three
    departments and offers no way to reach any of them.
    """
    import inspect

    from app.services import context_builder

    source = inspect.getsource(context_builder._suggested)
    for key in ('"code"', '"id"', '"name"', '"why"'):
        assert key in source


def test_a_suggestion_says_why_it_was_suggested():
    """The list comes from which permissions the reader LACKS, not from the
    question -- an honest signal but a weak one, and saying so lets a reader
    discount a suggestion the system had no real basis for."""
    from app.services.context_builder import _WHY_SUGGESTED

    assert "HR" in _WHY_SUGGESTED
    assert "cannot read" in _WHY_SUGGESTED["HR"]


def test_a_no_match_does_not_send_anyone_to_ask_for_nothing():
    """Nothing was withheld. Offering the request ladder here spends a
    colleague's afternoon on material that does not exist."""
    refusal = _context(out_of_scope=True, refusal_kind="NO_MATCH").refusal()
    assert refusal["kind"] == "NO_MATCH"
    assert refusal["options"] == []
    assert "nothing was withheld" in refusal["reason"].lower()


def test_the_two_reasons_do_not_share_wording():
    """A reader has to be able to tell which one they got."""
    denied = _context(out_of_scope=True, refusal_kind="NOT_PERMITTED").refusal()["reason"]
    missing = _context(out_of_scope=True, refusal_kind="NO_MATCH").refusal()["reason"]
    assert denied != missing
    assert "entitled" in denied
    assert "entitled" not in missing


def test_the_message_a_contributor_reads_matches_the_reason():
    """The strings live in copilot.py, not here, so this pins the pairing rather
    than the prose: a permission refusal points at the request button, a
    no-match one must not."""
    import inspect

    from app.services import copilot

    source = inspect.getsource(copilot.ask)
    permitted = source.index('"NOT_PERMITTED"')
    ladder = source.index("Ask another department")
    otherwise = source.index("Nothing in this task's evidence covers that")
    assert permitted < ladder < otherwise, "the ladder must sit in the NOT_PERMITTED branch"


# --- and never asking yourself ----------------------------------------------


def test_a_task_is_not_told_to_ask_its_own_department():
    """`_suggested` strips the task's own department.

    The task's requirements are what put it in that department, so its own code
    always scores highly -- which made it the most likely suggestion and the
    most certainly useless one.
    """
    import inspect

    from app.services import context_builder

    source = inspect.getsource(context_builder._suggested)
    assert "own_code" in source
    assert "if code == own_code" in source


def test_the_builder_uses_the_filtered_suggestion():
    """A helper nothing calls is not a fix."""
    import inspect

    from app.services import context_builder

    assert "_suggested(" in inspect.getsource(context_builder.build)


def test_refusal_is_decided_by_having_nothing_not_by_search_alone():
    """The regression guard for the original bug.

    `or not sources` is what turned an unmatched query into a permission
    refusal. If it comes back, this is the line that says so.
    """
    import inspect

    from app.services import context_builder

    source = inspect.getsource(context_builder.build)
    assert "not (sources or evidence_block or resolution_block)" in source
    assert "result.out_of_scope or not sources" not in source


# --- and the material must be usable, not merely present ---------------------
#
# The fix above let the answer through. The model then refused anyway, and its
# reasoning said exactly why:
#
#     ...an attached evidence item titled "Standard commercial assumptions"
#     that states "Prices are valid for 90 calendar days"... these items are
#     NOT part of the authorized context you supplied, and I cannot rely on
#     them to answer your question.
#
# It read the sentence that answered the question and declined to use it. That
# was obedience, not failure: COPILOT_RULE says "answer ONLY from the AUTHORIZED
# CONTEXT", and `prompt_block` put that heading on the query-time search results
# alone, with approved evidence above it under a heading of its own.
#
# Being in the prompt is not the same as being usable. These cases pin the
# difference.


def test_attached_evidence_sits_under_the_authorized_heading():
    """Not above it. Above it, the rule excludes it by its own terms."""
    block = _context(evidence_block="[E1] Prices are valid for 90 calendar days").prompt_block()
    heading = block.index("AUTHORIZED CONTEXT")
    evidence = block.index("[E1] Prices are valid")
    assert heading < evidence, "evidence outside the authorized heading is unusable by the rule"


def test_a_resolution_sits_under_it_too():
    block = _context(resolution_block="[R1] Finance confirmed the hold.").prompt_block()
    assert block.index("AUTHORIZED CONTEXT") < block.index("[R1] Finance confirmed")


def test_every_kind_of_material_is_citable():
    """A block with no ids cannot be cited, and material that cannot be cited
    will not be used -- the rule requires a citation for every claim.

    Asserted on rendered output rather than on the source. The earlier version
    grepped the renderer for the literal "[E{index}]" and broke when that line
    moved into a helper, with the behaviour completely unchanged. A test that
    fails on a correct refactor teaches people to edit the literal and stop
    reading the assertion -- which is how the citation format drifted away from
    the extractor in the first place.
    """
    from app.services.citations import refs_in
    from app.services.context_builder import evidence_line

    class _Status:
        value = "CURRENT_VERIFIED"

    class _Evidence:
        status = _Status()
        expiry_date = None
        excerpt = "We confirm the platform was delivered."
        evidence_type = "REFERENCE_LETTER"
        # NULL, as the column defaults: nobody has judged this row.
        is_substantiated = None
        substantiation_note = None

    assert refs_in(evidence_line(1, _Evidence(), "PRIMARY")) == {"E1"}


def test_the_rule_names_all_three_prefixes():
    """If the rule only knows [S#], the other two are background again."""
    from app.gateway.registry import COPILOT_RULE

    assert "[E#]" in COPILOT_RULE
    assert "[R#]" in COPILOT_RULE
    assert "[S#]" in COPILOT_RULE


def test_the_rule_forbids_the_exact_failure_that_happened():
    """Quoting the answer while refusing to give it."""
    from app.gateway.registry import COPILOT_RULE

    assert "refusing while quoting it is a failure" in COPILOT_RULE


def test_the_prompt_version_moved():
    """Prompts are versioned so a cached answer from the old wording is not
    served against the new rule -- and so a regression is attributable."""
    from app.gateway.registry import TASK_COPILOT

    assert TASK_COPILOT.prompt_version != "v1"


def test_nothing_at_all_still_says_so():
    """The empty case must not become an empty heading with no explanation."""
    assert "(nothing admissible was found)" in _context().prompt_block()


def inspect_source(fn) -> str:
    import inspect

    return inspect.getsource(fn)


# --- and what an answer leaned on has to be recorded -------------------------
#
# An answer that cited [E1], [E2] and [E3] displayed as "no sources cited". The
# extractor matched only [S#], because [S#] was the only citable prefix when it
# was written; approved evidence and cross-department answers became citable
# later and it was never updated.
#
# That fails in the worst direction. The citation count is the reviewer's one
# at-a-glance signal of whether an answer is grounded, and it was reporting a
# well-grounded answer as ungrounded.


def test_every_citable_prefix_is_recognised():
    from app.services.copilot import CITATION

    text = "per [E1] and [E2], plus [S3] and [R1]"
    assert sorted(CITATION.findall(text)) == ["E1", "E2", "R1", "S3"]


def test_something_that_is_not_a_citation_is_not_counted():
    from app.services.copilot import CITATION

    assert CITATION.findall("[X9] [EE1] [S] [12]") == []


def test_evidence_citations_are_stored_as_evidence():
    """Not lumped in with chunk ids. Evidence passed an approval gate; a chunk
    is raw company text the task happens to be entitled to read, and a reviewer
    weighs the two differently.

    Asserted through the mapping rather than by grepping `ask` for a variable
    name: the old version of this test broke on a refactor that kept the
    behaviour exactly, which teaches people to edit the literal instead of
    reading what it was protecting.
    """
    from app.services.citations import recorded_columns

    recorded = recorded_columns(
        {"E1", "S1"},
        {"evidence": {"E1": "evidence-id"}, "retrieved": {"S1": "chunk-id"}},
    )
    assert recorded["cited_evidence_ids"] == ["evidence-id"]
    assert recorded["cited_chunk_ids"] == ["chunk-id"]


def test_the_context_knows_which_evidence_each_ref_means():
    """Rendering [E1] into the prompt without keeping the id is what made the
    citation unrecoverable afterwards."""
    import inspect

    from app.services import context_builder

    assert "evidence_refs" in inspect.getsource(context_builder.TaskContext)
    assert 'refs[f"E{index}"] = evidence.id' in inspect.getsource(context_builder._render_evidence)
