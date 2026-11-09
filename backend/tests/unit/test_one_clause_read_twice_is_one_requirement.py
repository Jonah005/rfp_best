"""The extraction defects a review found in the live Jadwa export.

Four of them, and they are not four bugs. Each is the model answering from the
shape of a sentence rather than from what the clause does:

    R-001  "These requirements are the property of Jadwa ... may not be
           circulated, copied or distributed" -> CUSTOMER. The sentence asserts
           the customer's property right and names no actor, but the duty it
           creates is the reader's, and the reader is the bidder. Filed as the
           customer's, a confidentiality undertaking leaves the obligations
           anyone reviews.

    R-033  listed five purposes for the Core OMS integration; R-034 listed
    R-034  three of the same five, because a page break cut the list in two and
           each half was decomposed on its own. Both reached the matrix, to be
           evidenced twice and answered twice.

    R-037  "Jadwa will provide SMS service connection details to send
    R-042  validation SMS to applicant" -> one CUSTOMER requirement. Two things
           happen: the customer hands over credentials, the bidder sends the
           messages. As one customer row, the integration work is on nobody's
           list.

    R-044  "Document Management System (Qoute Separate): its intended use is
           Upload client submitted document to Cabinet" -- the product name and
           the SDK the clause names are gone, and a requirement that does not
           name the product cannot be evidenced.

Two are fixed in code, because code can be sure: a passive prohibition binds
the reader, and a requirement wholly contained in its neighbour adds nothing.
Two are prompt rules, because only a reader of the clause can tell which verb
belongs to whom.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from app.gateway.registry import get as get_call_type
from app.models.enums import ObligationOwner
from app.services.requirements import CONTAINMENT, PAGE_BREAK_SPAN, resolved_obligation_owner

CONFIDENTIALITY = (
    "These requirements are the property of Jadwa Investment Company and considered "
    "strictly confidential. It contains information that may not be circulated, copied "
    "or distributed in any form whatsoever without the express written permission of "
    "the Management of Jadwa Investment Company."
)


def test_a_prohibition_on_the_reader_is_the_bidders_obligation():
    assert (
        resolved_obligation_owner(CONFIDENTIALITY, ObligationOwner.CUSTOMER)
        is ObligationOwner.BIDDER
    )


def test_the_usual_passive_prohibitions_are_all_caught():
    for text in (
        "The contents shall not be disclosed to any third party.",
        "This document must not be reproduced without consent.",
        "Pricing information may not be shared with other bidders.",
        "The materials may not be used for any other purpose.",
    ):
        assert (
            resolved_obligation_owner(text, ObligationOwner.CUSTOMER) is ObligationOwner.BIDDER
        ), text


def test_a_customer_right_is_still_the_customers():
    """The override only ever moves a label toward the bidder, and only on a
    prohibition. A customer reserving a discretion keeps it."""
    for text in (
        "The Authority shall be under no obligation to accept the lowest offer.",
        "The customer may waive any minor non-conformity.",
        "Jadwa will provide access to the Core OMS system.",
    ):
        assert (
            resolved_obligation_owner(text, ObligationOwner.CUSTOMER) is ObligationOwner.CUSTOMER
        ), text


def test_nothing_is_ever_promoted_to_the_customer():
    """The direction that loses information. A bidder obligation relabelled
    CUSTOMER takes a real risk off the review list; the reverse only clutters
    it."""
    assert (
        resolved_obligation_owner(CONFIDENTIALITY, ObligationOwner.BIDDER) is ObligationOwner.BIDDER
    )


def test_the_page_break_fold_needs_containment_and_nearness_together():
    """Similarity alone would merge two obligations that are 0.9 alike and
    genuinely two. What makes this safe is that every word of the shorter is in
    the longer AND they come from the same document a few thousand characters
    apart -- the signature of one clause split across a page."""
    from app.services import requirements

    source = inspect.getsource(requirements.fold_page_break_duplicates)
    assert "PAGE_BREAK_SPAN" in source
    assert "len(small & large) / len(small)" in source
    assert "_similarity(" not in source, "containment, not similarity"
    assert CONTAINMENT >= 0.95
    assert PAGE_BREAK_SPAN <= 10_000


def test_the_longer_requirement_is_the_one_that_survives():
    """The shorter is the tail of the list: it is missing part of what the
    clause said. Keeping it and withdrawing the complete one would lose
    obligations."""
    from app.services import requirements

    source = inspect.getsource(requirements.fold_page_break_duplicates)
    assert "(other, requirement) if len(theirs) < len(mine) else (requirement, other)" in source
    assert "if len(small) == len(large):" in source, "equal length means neither contains the other"


def test_a_fold_goes_through_withdraw_so_nothing_is_left_pointing_at_it():
    """`withdraw` is the one place that moves clarification links onto the
    survivor and retires the assessment. A fold that set the lifecycle by hand
    would leave both behind -- the failure that has already cost this codebase
    two rounds."""
    from app.services import requirements

    source = inspect.getsource(requirements.fold_page_break_duplicates)
    assert "withdraw(" in source
    assert "successors=[longer.id]" in source
    assert "lifecycle_state =" not in source


def test_the_fold_is_reported_not_silent():
    from app.services import requirements

    source = inspect.getsource(requirements.extract_requirements)
    assert "report.page_break_folds = fold_page_break_duplicates(" in source


def test_the_prompt_splits_a_clause_whose_verbs_have_different_actors():
    prompt = get_call_type("requirement_decomposition").system_prompt
    assert "provides something SO THAT something is done is two requirements" in prompt
    assert "handing something over is the customer's, using it is the bidder's" in prompt
    # And does not over-split: a purpose that is itself the customer's own act
    # stays one row.
    assert "does it stay one customer requirement" in prompt


def test_the_prompt_keeps_the_names_that_make_a_row_evidenceable():
    prompt = get_call_type("requirement_decomposition").system_prompt
    assert "Keep every name the clause uses" in prompt
    assert "cannot be evidenced" in prompt


def test_the_prompt_marks_the_rating_clause_as_scored():
    prompt = get_call_type("requirement_decomposition").system_prompt
    assert "RATED, SCORED, EVALUATED" in prompt
    assert "set is_scored true" in prompt
    # Still one requirement, still the customer's act.
    assert "It stays ONE requirement owned by the CUSTOMER" in prompt


def test_the_ambiguity_detector_is_told_what_was_already_found():
    """The outline rule stopped absence claims about missing SECTIONS. These
    were about missing CONTENT -- "the evaluation criteria are not stated" on a
    tender whose R-073 lists all seven -- and from inside a ten-block window
    the claim looked true."""
    prompt = get_call_type("ambiguity_analysis").system_prompt
    assert "`requirements_already_extracted` lists every requirement" in prompt
    assert "do not report it missing" in prompt
    assert "what weight and scoring method apply to them" in prompt


def test_that_list_actually_reaches_the_model():
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    assert "requirements_in_document=extracted" in source
    payload = inspect.getsource(tender_analysis._payload)
    assert '"requirements_already_extracted"' in payload
    # Other callers of _payload must be byte-identical, or their cached answers
    # silently stop matching.
    assert "if requirements_in_document is not None:" in payload


def test_a_withdrawn_requirement_is_not_offered_as_context():
    """A folded duplicate must not come back as evidence that something exists:
    the list is of LIVE requirements."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis._extracted_requirements)
    assert "lifecycle_state != RequirementLifecycle.WITHDRAWN" in source


R_033 = (
    "Tradenet Core OMS System: Jadwa will provide access to Core OMS system for the "
    "following purpose: Check client already exsist; Create client account by adding all "
    "collected information; Create Beneficary accounts; Create Electronic trading account "
    "with defined settings; Auto approval of compliance based on new system approval"
)
R_034 = (
    "Jadwa will provide access to Core OMS system for the following purpose: Create "
    "Beneficary accounts; Create Electronic trading account with defined settings; Auto "
    "approval of compliance based on new system approval"
)


def _containment(a: str, b: str) -> float:
    """The decision the fold makes, on the two texts it has to get right."""
    from app.services.requirements import _comparable

    left, right = set(_comparable(a).split()), set(_comparable(b).split())
    small, large = (left, right) if len(left) < len(right) else (right, left)
    return len(small & large) / len(small)


def test_the_real_page_break_pair_folds():
    assert _containment(R_033, R_034) >= CONTAINMENT


def test_two_obligations_that_merely_overlap_do_not_fold():
    """The margin this threshold is buying. These two share their whole lead-in
    and differ in what must be supported -- two requirements, and a rule loose
    enough to merge them would lose one of them silently."""
    near = _containment(
        "The vendor must support English and Arabic across the portal",
        "The vendor must support English across the portal and the admin site",
    )
    assert near < CONTAINMENT


def test_a_prompt_bump_retires_the_old_reading_of_the_same_clause():
    """R-044 (v10) and R-081 (v11) are both "Integration Requirements / 7".

    v11 named the product and the SDK URL that v10 had dropped, so the two were
    too differently worded to pair, and both stayed live: one stale row mixed
    in with 74 current ones, the same obligation twice, and nothing saying
    which to read. A prompt bump re-reads every block, and the new reading of a
    clause is the authoritative one.
    """
    from app.services import requirements

    source = inspect.getsource(requirements.retire_stale_clause_readings)
    # Same clause of the same document, or it is not the same obligation.
    assert "RequirementVersion.clause_ref.is_not(None)" in source
    assert "by_clause[(document_id, version.clause_ref)]" in source
    # Only retires where the CURRENT version has also read that clause.
    assert "if not current:" in source
    # Only an earlier version of the same call type -- never a person's edit,
    # never another detector.
    assert 'startswith(f"{call_type}@")' in source
    assert "withdraw(" in source and "successors=[r.id for r in current]" in source


def test_the_retirement_runs_before_the_page_break_fold():
    """Order matters: folding first would compare a live requirement against a
    stale one and keep whichever was longer, which is not the same question."""
    from app.services import requirements

    source = inspect.getsource(requirements.extract_requirements)
    assert source.index("retire_stale_clause_readings(") < source.index(
        "fold_page_break_duplicates("
    )


def test_a_rekeyed_deliverable_is_adopted_not_duplicated():
    """The regression this pass caused and then fixed.

    Changing how the deliverable key is computed re-keys every future run while
    the stored rows keep the old key, so the next run matches nothing and
    writes a second copy of the whole sheet: 15 deliverables became 24, nine of
    them exact-name twins. The old key is kept only to recognise such a row and
    re-stamp it.
    """
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.extract_deliverables)
    assert "_legacy_fingerprint(proposal)" in source
    assert "existing.fingerprint = fingerprint" in source
    adopt = source.index("existing.fingerprint = fingerprint")
    create = source.index("session.add(")
    assert adopt < create, "a row must be adopted before anything new is written"


def test_twins_already_in_the_table_are_folded_and_say_so():
    """`duplicate_of_deliverable_id` has been in the schema since a review
    objected to duplicates being folded by marking them NOT_APPLICABLE. Nothing
    ever wrote to it, so the sheet's `Duplicate of` stayed blank however many
    twins it listed."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.fold_duplicate_deliverables)
    assert "row.duplicate_of_deliverable_id = first.id" in source
    assert "row.equivalence_reason" in source
    # The oldest row survives: tasks and links already point at it.
    assert "order_by(Deliverable.created_at" in source
    # A folded row is still a required submission -- the pointer says it is the
    # same one, not that it is optional.
    assert "status" not in source.split('"""')[2]


def test_an_internal_note_is_not_counted_as_a_customer_question():
    """15 identity-drift findings were counted in the matrix's open-question
    column. They carry no `recommended_question`, the assessment already
    refuses to let one hold a row, and the export must use the same rule or the
    two disagree about the same requirement."""
    queries = (Path(__file__).resolve().parents[2] / "scripts" / "_export_queries.py").read_text(
        encoding="utf-8"
    )
    assert "ClarificationIssue.recommended_question.is_not(None)" in queries
    # And they stay visible, labelled rather than hidden.
    assert '"Audience"' in queries
    assert '"customer" if (c.recommended_question or "").strip() else "internal QA"' in queries


def test_the_migration_never_takes_a_key_another_row_already_holds():
    """The regression the regression-fix caused.

    Where last run had written a twin, BOTH keys were present: re-stamping the
    old-key row onto the new key gave two rows one key, and analysis died on
    uq_deliverable_fingerprint with a 500 before deliverables, ambiguity or
    anything after them had run. The new key is taken only when nothing holds
    it; otherwise the old row is left alone for the fold to point at the
    survivor.
    """
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.extract_deliverables)
    assert "existing = next((m for m in matches if m.fingerprint == fingerprint), None)" in source
    assert "if existing is None and matches:" in source
    # The re-stamp happens ONLY inside that branch.
    restamp = source.index("existing.fingerprint = fingerprint")
    guard = source.index("if existing is None and matches:")
    assert guard < restamp < source.index("elif existing is not None and len(matches) > 1:")


def test_a_failed_write_cannot_take_the_rest_of_the_analysis_with_it():
    """Deliverable extraction runs inside `analyze_bid`, before the ambiguity
    pass. A constraint violation there is not a deliverables problem -- it is
    the whole run, and on the live bid it returned 500 with nothing saved."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_bid)
    assert "extract_deliverables(" in source


def test_a_reparse_does_not_hide_the_stale_reading():
    """Why the first version of this retired nothing.

    R-044 (v10) and R-081 (v11) are both "Integration Requirements / 7", and
    the retirement still left both live, because it grouped by document
    VERSION and the file had been parsed again between the two runs. A
    re-parse does not make it a different clause: "clause 7 of the integration
    requirements in this file" is the same obligation whichever parse found
    it, which is the entire idea of a qualified clause ref.
    """
    from app.services import requirements

    source = inspect.getsource(requirements.retire_stale_clause_readings)
    assert "by_clause[(document_id, version.clause_ref)]" in source
    assert "source_document_version_id, version.clause_ref" not in source
    # The document id has to be joined for, not guessed from the version row.
    assert "DocumentVersion.id == RequirementVersion.source_document_version_id" in source


def test_the_deliverable_sheet_answers_the_checklist_question_itself():
    """A folded row keeps IDENTIFIED and mandatory, because the tender really
    does require that submission -- so a checklist built from `Status` alone
    asks for the same document twice. The answer belongs in the first column,
    not three columns to the right in `Duplicate of`."""
    queries = (Path(__file__).resolve().parents[2] / "scripts" / "_export_queries.py").read_text(
        encoding="utf-8"
    )
    assert '"Submit?"' in queries
    assert '"no -- another row carries it" if d.duplicate_of_deliverable_id else "yes"' in queries
    headers = queries[queries.index("deliv_headers = ["):]
    assert headers.index('"Submit?"') < headers.index('"Deliverable"')


def test_reconciliation_happens_even_when_there_is_nothing_new_to_read():
    """Why R-044 survived three runs after the pass that should have retired it.

    `extract_requirements` returns early when every block has already been
    decomposed by the current prompt version -- and that return sat BEFORE the
    reconciliation passes at the end of the function. So the bump to v11
    re-read every block on one run, and from the next run onwards the early
    return fired and the retirement was unreachable. A defect that only the
    first run after a prompt bump can fix is a defect nobody can fix: by the
    time it is noticed, that run is over.

    Both passes are idempotent and neither calls a model, so the no-op path
    runs them too.
    """
    from app.services import requirements

    source = inspect.getsource(requirements.extract_requirements)
    early = source.index("if not pairs:")
    tail = source.rindex("retire_stale_clause_readings(")
    # Called on BOTH paths, not just the one that did work.
    assert source.count("retire_stale_clause_readings(") == 2
    assert source.count("fold_page_break_duplicates(") == 2
    assert early < source.index("retire_stale_clause_readings(") < tail


def test_the_no_op_path_still_returns_what_it_did():
    """A run that changes the register must say so, or "nothing to do" and
    "something was quietly withdrawn" look identical in the report."""
    from app.services import requirements

    source = inspect.getsource(requirements.extract_requirements)
    head = source[source.index("if not pairs:") : source.index("covered_ordinals")]
    assert "report.stale_readings_retired =" in head
    assert "report.page_break_folds =" in head
