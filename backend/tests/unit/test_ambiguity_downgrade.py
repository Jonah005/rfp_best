"""An uncited contradiction must not cost the whole batch.

Observed against the live model three runs in a row: it reported a
CONTRADICTORY_DATE or CONTRADICTORY_QUANTITY whose second block sat outside the
batch it could see, the strict validator rejected the entire response, and the
repair loop then escalated — each retry produced more issues until the reply hit
the token cap and came back as truncated JSON. One unciteable finding lost
twenty-five good ones and failed the request.
"""

from __future__ import annotations

import pytest

from app.models.enums import ClarificationIssueType
from app.schemas.tender_extraction import AmbiguityAnalysisResult, DetectedAmbiguity


def _issue(**overrides):
    base = dict(
        issue_type=ClarificationIssueType.CONTRADICTORY_DATE,
        description="Two different closing dates are given.",
        severity="MAJOR",
        source_block_ordinal=3,
        confidence=0.8,
    )
    base.update(overrides)
    return DetectedAmbiguity(**base)


@pytest.mark.parametrize(
    "issue_type",
    [
        ClarificationIssueType.CONTRADICTORY_DATE,
        ClarificationIssueType.CONTRADICTORY_QUANTITY,
        ClarificationIssueType.CONFLICTING_INSTRUCTION,
    ],
)
def test_an_uncited_contradiction_is_downgraded_not_rejected(issue_type):
    issue = _issue(issue_type=issue_type)
    assert issue.issue_type is ClarificationIssueType.INCOMPLETE_SCOPE


def test_the_downgrade_says_so_in_the_description():
    """A reviewer must see that the conflict was never verified."""
    issue = _issue()
    assert "Two different closing dates are given." in issue.description
    assert "not cited" in issue.description
    assert "unverified" in issue.description


def test_a_properly_cited_contradiction_keeps_its_type():
    issue = _issue(conflicting_block_ordinal=9)
    assert issue.issue_type is ClarificationIssueType.CONTRADICTORY_DATE
    assert "unverified" not in issue.description


def test_one_uncited_issue_no_longer_invalidates_the_whole_batch():
    """The actual regression: 26 findings lost because of one."""
    payload = {
        "issues": [
            {
                "issue_type": "BLANK",
                "description": "The submission address is left blank.",
                "severity": "MAJOR",
                "source_block_ordinal": 1,
                "confidence": 0.9,
            },
            {
                "issue_type": "CONTRADICTORY_QUANTITY",
                "description": "Seat count disagrees with an earlier clause.",
                "severity": "MAJOR",
                "source_block_ordinal": 5,
                "confidence": 0.7,
            },
        ]
    }
    result = AmbiguityAnalysisResult.model_validate(payload)
    assert len(result.issues) == 2
    assert result.issues[0].issue_type is ClarificationIssueType.BLANK
    assert result.issues[1].issue_type is ClarificationIssueType.INCOMPLETE_SCOPE
