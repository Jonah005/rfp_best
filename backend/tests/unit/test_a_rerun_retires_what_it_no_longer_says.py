"""Re-running analysis must not leave the previous run's wording open beside it.

The live bid held 322 open ambiguity findings against a 43-block document. Six
of the twelve widest questions were two questions:

    What is the required scope, format and duration of the source code and
        development knowledge transfer ...
    What is the required scope, format and acceptance criteria for the source
        code and development knowledge ...
    What are the required IP/source ownership terms and the expected format and
        duration of the knowledge ...

A finding's fingerprint hashes the model's own description. Temperature is zero,
but the batch window is not stable across runs -- it moves as extraction changes
the block set -- so the same clause read in a different window is described
differently, `uq_clarification_fingerprint` does not fire, and the run mints a
fresh row beside the old one. Nothing ever closed the old one, because
supersession was keyed to the PROMPT VERSION changing.

The argument already written for prompt supersession is the whole argument here:
"the old run's answers are not evidence about the document; they are evidence
about a question the system has stopped asking". It never depended on the prompt
changing.

The rejected alternative is worth recording, because it looks tidier. Fingerprint
on (issue_type, block) and drop the prose: stable, no closing pass. It would
also have deleted a real question -- block 46 carried two distinct
UNDEFINED_FIELD findings, one asking what "final release" means and one asking
how proposals are weighted, and that key cannot tell them apart. Currency, not
identity.
"""

from __future__ import annotations

import inspect


def test_a_finding_the_rerun_repeated_is_kept():
    """Identity still runs through the fingerprint. Anything this run raised
    again is in `seen` and must survive untouched -- this pass retires, it does
    not re-decide."""
    from app.services import clarifications

    source = inspect.getsource(clarifications.close_findings_this_run_did_not_repeat)
    assert "if issue.fingerprint in seen:" in source
    assert "continue" in source.split("if issue.fingerprint in seen:")[1][:40]


def test_only_findings_about_blocks_this_run_read_can_be_closed():
    """The guard that stops this being data loss.

    A run that fails partway through, or one scoped to part of the document, has
    said nothing about the blocks it never opened. Closing those would be the
    silent-skip shape that has caused three separate bugs in this codebase: the
    numbered-clause exemption, the requirement-based decomposition fallback, and
    the DELETE with no tenant context that reported success against zero rows.
    """
    from app.services import clarifications

    source = inspect.getsource(clarifications.close_findings_this_run_did_not_repeat)
    assert "in covered:" in source
    assert "stale.append(issue)" in source
    # The append must be inside the coverage test, not beside it.
    assert source.index("in covered:") < source.index("stale.append(issue)")


def test_coverage_is_recorded_only_after_the_model_answers():
    """A block counts as read when the call for it returned, not when it was
    queued. Filling `covered` from the batch list up front would let a run that
    died on its second call retire findings about every block in the document."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    call_at = source.index('model_gateway.generate_structured(\n            "ambiguity_analysis"')
    covered_at = source.index("covered_blocks.update(")
    assert call_at < covered_at, "coverage must be recorded after the call returns"


def test_a_question_in_a_person_s_hands_is_never_retired():
    """SENT_TO_CLIENT means somebody asked the customer. A rerun that rephrases
    the question does not un-ask it."""
    from app.services import clarifications

    # The body, not the docstring -- which names the statuses in order to
    # explain that they are exempt.
    body = inspect.getsource(clarifications.close_findings_this_run_did_not_repeat).split(
        '"""'
    )[2]
    assert "ClarificationStatus.OPEN" in body
    assert "SENT_TO_CLIENT" not in body, (
        "the query must select OPEN alone; the other close_* helpers take "
        "SENT_TO_CLIENT too, and copying that here would un-ask a question "
        "somebody has already put to the customer"
    )


def test_the_links_go_with_the_finding():
    """A closed issue holding a live link is a trap for anything that counts
    links rather than statuses -- and the matrix counts links."""
    from app.services import clarifications

    source = inspect.getsource(clarifications.close_findings_this_run_did_not_repeat)
    assert "delete(ClarificationRequirement)" in source


def test_retirement_runs_before_the_link_backfill():
    """Otherwise the backfill hands links to findings that are about to close,
    and they keep them: the backfill reaches every open finding, and it runs
    once."""
    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    assert source.index("close_findings_this_run_did_not_repeat") < source.index(
        "link_open_issues_to_their_blocks(session, ctx, bid_id=bid_id)"
    )


def test_it_is_scoped_to_one_detector_version():
    """The coverage check is about blocks; the detector check is about voice.
    Retiring another detector's findings because the ambiguity model did not
    repeat them would close the placeholder and coverage checks on every run."""
    from app.services import clarifications

    source = inspect.getsource(clarifications.close_findings_this_run_did_not_repeat)
    assert "ClarificationIssue.detected_by == detected_by" in source
