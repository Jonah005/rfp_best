"""Questions a single customer reply would answer are one question.

The end of a chain. Clarifications were invisible; linking them made every row
light up; the fragments behind that were merged; what is left is the same
question stored several times, because a finding is fingerprinted on the model's
own prose and a clause re-read in a different batch window is described
differently.

On the live tender: 234 open ambiguity findings, 79 distinct question texts, and
five of those 79 asking one thing --

    What is the required scope, format and duration of the source code and
        development knowledge transfer?
    What are the required IP/source ownership terms and the expected format and
        duration of the knowledge transfer?

-- each landing on R-027 and R-124 to R-127, so R-124 shows eleven questions
where there are four.

Two deterministic rules were tried against this data first, and their failures
are the argument for a model call. Fingerprinting on (issue_type, block) cannot
tell "what does final release mean" from "how are proposals weighted", which are
two real questions about one paragraph. Closing whatever a rerun did not repeat
closed 44 real questions, the submission deadline among them, because the prompt
caps output at twelve issues per batch and no single run can enumerate a
document this size.

Character similarity fails from both directions at once: two phrasings of the
knowledge-transfer question share few words and are one question, while two
different questions about one clause share their whole lead-in and are two. So
the model judges meaning and the code records the verdict with a version stamp,
exactly as every other detector in this codebase does.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.clarification_dedupe import QuestionDedupeResult, QuestionGroup


def test_the_test_is_the_answer_not_the_wording():
    """The prompt must ask whether one reply settles both.

    "Are these similar?" is the phrasing that collapses distinct questions about
    one clause, and that is the failure that cannot be undone from the matrix.
    """
    from app.gateway.registry import get as get_call_type

    prompt = get_call_type("question_equivalence").system_prompt
    assert "a single reply from the customer would answer both completely" in prompt
    assert "they are DIFFERENT" in prompt
    assert "even when they concern the same clause" in prompt


def test_a_question_cannot_restate_itself():
    with pytest.raises(ValidationError):
        QuestionGroup(canonical_index=2, restatement_indexes=[1, 2])


def test_a_question_belongs_to_at_most_one_group():
    """Otherwise two groups both claim it and the last write decides, which is
    how a question ends up pointing at a canonical that is itself a
    restatement."""
    with pytest.raises(ValidationError):
        QuestionDedupeResult(
            groups=[
                QuestionGroup(canonical_index=0, restatement_indexes=[1]),
                QuestionGroup(canonical_index=2, restatement_indexes=[1]),
            ]
        )


def test_a_group_of_one_is_not_a_group():
    """A question with no duplicate is left out entirely. Permitting a
    single-member group would make "grouped" and "has a duplicate" different
    things, and every reader would have to know which."""
    from app.gateway.registry import get as get_call_type

    prompt = get_call_type("question_equivalence").system_prompt
    assert "Return groups of two or more only" in prompt


def test_an_out_of_range_index_discards_the_whole_group():
    """The model miscounting is the failure mode with teeth.

    Applying the in-range half of a miscounted answer attaches a verdict to
    whichever question happens to sit at that index -- silently marking a real
    question as a restatement of something unrelated. Better to dedupe nothing.
    """
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "if any(index >= len(candidates) or index < 0 for index in indexes):" in source
    guard_at = source.index("if any(index >= len(candidates)")
    write_at = source.index("restatement.restates_clarification_id = canonical.id")
    assert guard_at < write_at, "every index must be checked before anything is written"


def test_a_restatement_holds_no_links():
    """The canonical question carries the row. Leaving the links on would make
    the matrix count one question four times against one requirement, which is
    the thing being fixed."""
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "delete(ClarificationRequirement)" in source


def test_the_backfill_does_not_hand_the_links_straight_back():
    """`link_open_issues_to_their_blocks` reaches every open finding, so without
    this filter the link pass undoes the dedupe on the same run."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.link_open_issues_to_their_blocks)
    assert "ClarificationIssue.restates_clarification_id.is_(None)" in source


def test_dedupe_runs_before_the_link_pass():
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    assert source.index("dedupe_open_questions") < source.index(
        "link_open_issues_to_their_blocks(session, ctx, bid_id=bid_id)"
    )


def test_a_restatement_is_closed_rather_than_left_open():
    """This reverses an earlier decision, on evidence.

    A restatement used to stay OPEN, on the argument that "asked four ways" is
    worth seeing. The live export settled it the other way: of 123 open
    questions 48 pointed at another open question, so the number a bid manager
    reads as "what we are waiting on the customer for" was inflated by two
    thirds -- and both BLOCKING submission-deadline questions were the same
    question, which the clarification letter would have asked twice.

    Visibility was never what was at stake. The row keeps its text, its pointer
    to the canonical and the reason, so a merge can still be found and argued
    with. What changes is that it stops counting as outstanding.
    """
    from app.services import clarification_dedupe

    # The whole function, not a docstring-split slice: the judging loop now
    # lives in a nested helper with a docstring of its own, and slicing on
    # triple quotes silently cut the part being asserted about.
    body = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "restatement.status = ClarificationStatus.CLOSED" in body
    assert "restatement.resolution_note" in body
    assert "restatement.equivalence_reason" in body
    # The canonical is the question still being asked, so nothing here touches
    # its status.
    assert re.search(r"canonical\.status\s*=\s*[^=]", body) is None


def test_the_canonical_is_never_itself_a_restatement():
    """B restates A, A restates C. A three-link chain was visible in the live
    export, and nothing reading that table follows a chain, so the pointer is
    flattened to the root when the verdict is recorded."""
    from app.services import clarification_dedupe

    body = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "while canonical.restates_clarification_id is not None:" in body
    # A cycle in the data must not hang the run.
    assert "seen_canonical" in body
    assert "delete(ClarificationIssue)" not in body


def test_a_verdict_is_stamped_with_the_prompt_that_made_it():
    """Same rule as `detected_by` and `extracted_by`: a judgement that cannot
    name its prompt version cannot be re-judged when the prompt changes."""
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert 'stamp = f"llm:{call_type.name}@{call_type.prompt_version}"' in source
    assert "restatement.equivalence_by = stamp" in source


def test_a_question_already_judged_is_not_paid_for_twice():
    """The block is skipped when everything in it already carries the current
    stamp -- including the questions the model left ungrouped, which is why
    those are stamped too."""
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "pending = [i for i in group if i.equivalence_by != stamp]" in source
    assert "if issue.equivalence_by is None:" in source


def test_comparison_is_scoped_to_one_block():
    """Restatements come from re-reading one clause, so that is where they are.
    Comparing all 79 questions in one call would be a single enormous judgement
    whose mistakes are hard to see, and a question raised against two different
    blocks staying twice is the recoverable direction."""
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "by_block" in source
    assert "MAX_PER_CALL" in source


def test_a_note_about_our_own_processing_is_not_a_question():
    """Identity drift and coverage findings carry no `recommended_question`.
    Two of those being similar is not interesting, and grouping them would
    remove links that gate nothing anyway."""
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert 'if (i.recommended_question or "").strip()' in source


def test_questions_marked_before_folding_meant_closing_are_closed_too():
    """The backlog the change itself created.

    When restatements started closing, the ones already marked kept their old
    status for ever: the selection below skips anything that already carries a
    pointer, so nothing would look at them again. The live bid then reported 98
    open questions of which 39 were duplicates it had already identified --
    forty per cent of the queue, invisible to the fix that was supposed to
    remove it.

    Run every time rather than written as a migration, so it drains on the next
    analysis and cannot be forgotten; idempotent, because a second run finds
    nothing OPEN with a pointer.
    """
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "ClarificationIssue.restates_clarification_id.is_not(None)" in source
    backfill = source.index("restates_clarification_id.is_not(None)")
    selection = source.index("ClarificationIssue.restates_clarification_id.is_(None)")
    assert backfill < selection, "the backlog must be drained before the new judgements"
    # Same two consequences as a fresh merge: closed, and holding no links.
    window = source[backfill : backfill + 1200]
    assert "ClarificationStatus.CLOSED" in window
    assert "delete(ClarificationRequirement)" in window


def test_draftable_is_not_a_claim_that_the_evidence_was_judged():
    """Two requirements became DRAFTABLE on evidence nobody has judged. That is
    the intended rule -- unjudged is not a placeholder, and requiring a verdict
    on 600 excerpts before anyone can start would stall the whole bid -- but a
    workbook that does not say so invites a draft being read as proven."""
    legend = (Path(__file__).resolve().parents[2] / "scripts" / "export_matrix.py").read_text(
        encoding="utf-8"
    )
    assert "DRAFTABLE means a statement can be WRITTEN, not that the claim behind it is proven" in (
        legend
    )
    assert "PROVES CAPABILITY" in legend


def test_a_reference_to_another_question_can_be_found():
    """`Same question as` printed the canonical's question text, truncated to
    200 characters. A reviewer searching for that string reported 18 dangling
    references and 7 ambiguous ones -- every one an artefact of the truncation,
    not of the data. A short id makes the reference exact; the text after it
    keeps the row readable without following it."""
    queries = (Path(__file__).resolve().parents[2] / "scripts" / "_export_queries.py").read_text(
        encoding="utf-8"
    )
    assert '"Question id"' in queries
    assert "str(c.id)[:8]" in queries
    assert 'str(c.restates_clarification_id)[:8]' in queries


def test_one_obligation_is_a_scope_as_well_as_one_clause():
    """Three deadline questions survived the block pass.

    A MAJOR asking the calendar date, a BLOCKING asking date, time and time
    zone, and a MINOR asking whether the 2017 document still governs -- all
    about the submission deadline, all linked to R-012, and all surviving
    because they cite three DIFFERENT blocks. One reply settles them, and a
    clarification letter that asks the deadline three ways reads as though
    nobody had read the tender.

    So there is a second pass, scoped to the requirement rather than the
    block. Still bounded: a requirement holds a handful of questions, not the
    bid's seventy, so each call stays small enough for its mistakes to be
    visible -- which is the reason the whole bid is never compared at once.
    """
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "_groups_by_requirement(session, ctx, issues)" in source
    assert 'f"{stamp}#by-requirement"' in source
    # The block pass runs first: most restatements are there, and it is the
    # cheaper scope.
    assert source.index("sorted(by_block.items()") < source.index("_groups_by_requirement(")


def test_the_two_passes_do_not_cancel_each_other_out():
    """Each pass records its own stamp. A question settled by the block pass is
    not thereby settled for the requirement pass -- if it were, the second pass
    would skip every question that already had a verdict, which is all of
    them."""
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "def judge(groups: list[list[ClarificationIssue]], stamp: str) -> int:" in source
    assert "pending = [i for i in group if i.equivalence_by != stamp]" in source


def test_a_question_on_several_requirements_is_folded_once():
    """It appears in several groups. The first fold wins; the rest skip it,
    because a restatement is never offered as a candidate again."""
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe._groups_by_requirement)
    assert "grouped.setdefault(requirement_id, []).append(issue)" in source
    doc = clarification_dedupe._groups_by_requirement.__doc__ or ""
    assert "first group to fold it wins" in doc


def test_a_fold_never_makes_a_question_less_serious():
    """The regression the requirement-scoped pass caused immediately.

    Three deadline questions folded into one, and the model kept the MAJOR --
    so the BLOCKING question about the submission deadline closed as a
    duplicate, and a bid with no known deadline stopped blocking generation.
    Deduplication is about how many questions to ASK. It is not a judgement
    about how serious the answer is, and a fold that quietly downgrades a
    blocker is worse than the duplication it removed.
    """
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "if _rank(restatement.severity) > _rank(canonical.severity):" in source
    assert "canonical.blocks_generation = True" in source


def test_severity_is_ordered_worst_last():
    from app.models.enums import ClarificationSeverity
    from app.services.clarification_dedupe import _rank

    assert _rank(ClarificationSeverity.BLOCKING) > _rank(ClarificationSeverity.MAJOR)
    assert _rank(ClarificationSeverity.MAJOR) > _rank(ClarificationSeverity.MINOR)
    # An unknown value must not outrank a real one.
    assert _rank(None) == 0


def test_folds_made_before_the_rule_existed_are_repaired():
    """The blocker was closed into a MAJOR on a run before this rule, and no
    later run would revisit that group -- it is already judged under the
    current prompt version. So the repair runs every time, and is idempotent."""
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "_lift_severity_onto_canonicals(session, ctx, bid_id=bid_id)" in source
    repair = inspect.getsource(clarification_dedupe._lift_severity_onto_canonicals)
    assert "restates_clarification_id.is_not(None)" in repair
    assert "any(i.blocks_generation for i in restatements)" in repair
    # Before the new judgements, so a canonical folded this run is not lifted
    # twice.
    assert source.index("_lift_severity_onto_canonicals(") < source.index("judge(")


def test_the_oldest_question_is_the_one_that_survives():
    """Why consecutive runs over an unchanged tender disagreed.

    The model chose which phrasing was canonical, and it chose differently
    each run -- so the same document produced a different active question set
    every time, 52 new records a run, and a reviewer could not tell a real
    change from a rephrasing. Which wording is "the" question is not a
    judgement a model is better at than a clock. The earliest row is the one
    other things already point at, and it is the same row on every future run.

    The model still decides WHICH questions are one question. That is the
    judgement it is for.
    """
    from app.services import clarification_dedupe

    source = inspect.getsource(clarification_dedupe.dedupe_open_questions)
    assert "canonical = min(group_members, key=lambda i: (i.created_at, str(i.id)))" in source
    # Not the model's index.
    assert "canonical = candidates[group_result.canonical_index]" not in source
    # Ties break on id, so two rows written in the same instant still order.
    assert "str(i.id)" in source


def test_a_blocker_is_never_retired_by_silence():
    """The BLOCKING submission-deadline question disappeared between runs.

    `close_findings_this_run_did_not_repeat` reads "the latest run did not say
    it" as "the run no longer says it". For an ordinary finding that is right.
    For one that STOPS GENERATION it is not: the model rewords itself, so the
    blocker survived only as long as it happened to be described the same way,
    and the bid was left with a MAJOR reworded twin and no known deadline
    blocking anything.

    A blocker ends when the customer answers it or a person closes it.
    """
    from app.services import clarifications

    source = inspect.getsource(clarifications.close_findings_this_run_did_not_repeat)
    assert "if issue.blocks_generation:" in source
    guard = source.index("if issue.blocks_generation:")
    appended = source.index("stale.append(issue)")
    assert guard < appended, "the guard must run before anything is marked stale"
