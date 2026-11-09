"""A prompt change must retire what the old prompt said.

`detected_by` is "llm:<name>@<prompt_version>". Bump the version and the new
run's findings land under a new string, leaving the previous version's issues
open for ever with nothing pointing at them.

That is not a tidiness problem. `ambiguity_analysis` moved to v2 because its v1
prompt invited the model to report anything outside its batch as missing from
the document. v2 then raised twenty-four issues and not one blocker -- the fix
worked exactly as intended -- while all eight v1 blockers stayed OPEN, still
asserting the tender had no technical requirements, no evaluation criteria and
no submission deadline. The export still read "8 blocking", the readiness gate
still saw eight reasons not to generate, and the fix looked like it had done
nothing.

The old findings were never evidence about the document. They were evidence
about a question the system has stopped asking.

Closed, not deleted: the run happened, someone may have read those questions,
and the trail should record that the detector was superseded rather than that it
never spoke.
"""

from __future__ import annotations

import uuid

from app.models.enums import ClarificationStatus


class _Issue:
    def __init__(self, detected_by, status=ClarificationStatus.OPEN):
        self.id = uuid.uuid4()
        self.detected_by = detected_by
        self.status = status


def _matches(issue: _Issue, detector: str, current: str) -> bool:
    """The predicate `close_superseded_detector_versions` applies, in Python.

    The service expresses it in SQL against a live table; this states the same
    rule so the cases can be enumerated, and the last test ties the two
    together.
    """
    return (
        issue.detected_by.startswith(f"{detector}@")
        and issue.detected_by != current
        and issue.status in (ClarificationStatus.OPEN, ClarificationStatus.SENT_TO_CLIENT)
    )


DETECTOR = "llm:ambiguity_analysis"
CURRENT = "llm:ambiguity_analysis@v2"


def test_the_previous_version_is_retired():
    """The eight stale blockers, in one assertion."""
    assert _matches(_Issue("llm:ambiguity_analysis@v1"), DETECTOR, CURRENT)


def test_the_current_version_is_never_touched():
    """The run that just happened must survive its own cleanup, which runs
    before it raises anything."""
    assert not _matches(_Issue(CURRENT), DETECTOR, CURRENT)


def test_a_different_detector_is_never_touched():
    """Scoped by detector name. Retiring one detector's history must not close
    another's -- least of all a person's own questions."""
    for other in (
        "llm:requirement_decomposition@v1",
        "requirement_identity_drift@v1",
        "human",
        "llm:ambiguity_analysis_extra@v1",
    ):
        assert not _matches(_Issue(other), DETECTOR, CURRENT), other


def test_an_already_answered_issue_is_left_alone():
    """A closed or answered issue carries a decision somebody made. Reopening or
    rewriting it would lose that, and the count of what was closed would stop
    meaning anything."""
    for status in (ClarificationStatus.CLOSED, ClarificationStatus.ANSWERED):
        assert not _matches(_Issue("llm:ambiguity_analysis@v1", status), DETECTOR, CURRENT)


def test_an_issue_sent_to_the_client_is_retired_too():
    """Uncomfortable but right: a question already put to the customer that the
    system no longer believes in should stop gating generation. It stays in the
    record as asked."""
    assert _matches(
        _Issue("llm:ambiguity_analysis@v1", ClarificationStatus.SENT_TO_CLIENT),
        DETECTOR,
        CURRENT,
    )


def test_the_service_retires_before_it_raises():
    """Order matters and is easy to get wrong.

    Cleanup must run BEFORE the batch loop. Run it after, and it closes the
    findings the same call just produced -- the detector would silently erase
    itself, and the failure would look like "the analysis found nothing".
    """
    import inspect

    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    assert "close_superseded_detector_versions" in source, (
        "a prompt bump would strand the previous version's blockers"
    )
    cleanup = source.index("close_superseded_detector_versions")
    loop = source.index("for batch in batches:")
    assert cleanup < loop, "cleanup must run before the run it belongs to raises anything"


def test_the_service_scopes_the_cleanup_by_name_not_by_full_string():
    """Passing `detected_by` where `detector` belongs would match nothing, and
    the function would report zero closed while the stale blockers stayed open
    -- a silent no-op of exactly the kind this codebase keeps producing."""
    import inspect

    from app.services import tender_analysis

    source = inspect.getsource(tender_analysis.analyze_ambiguity)
    assert 'detector=f"llm:{call_type.name}"' in source
    assert "current=detected_by," in source
