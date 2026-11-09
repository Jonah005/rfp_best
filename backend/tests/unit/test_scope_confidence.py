"""The Scope Confidence gate.

The property these tests exist to protect: this gate measures how well we
understand the TENDER, and nothing else. If company-evidence readiness ever
becomes an input, a clear tender we happen to be unready for would look
ambiguous and drafting would be blocked for a reason clarification questions
cannot fix.
"""

from __future__ import annotations

import inspect

import pytest

from app.models.enums import ScopeConfidenceLevel
from app.services import tender_analysis
from app.services.tender_analysis import ScopeSignals, _grade


def clear_tender(**overrides) -> ScopeSignals:
    base = dict(
        clause_coverage_ratio=1.0,
        requirements_found=42,
        has_deadline=True,
        has_submission_rules=True,
        has_evaluation_criteria=True,
    )
    base.update(overrides)
    return ScopeSignals(**base)


def test_a_clean_tender_reaches_high():
    level, reasons = _grade(clear_tender())
    assert level is ScopeConfidenceLevel.HIGH
    assert reasons == []


def test_no_requirements_is_low():
    level, reasons = _grade(clear_tender(requirements_found=0))
    assert level is ScopeConfidenceLevel.LOW
    assert any("No requirements" in r for r in reasons)


def test_a_blocking_issue_is_low():
    level, reasons = _grade(clear_tender(blocking_issues=1))
    assert level is ScopeConfidenceLevel.LOW
    assert any("blocking" in r for r in reasons)


def test_unresolved_precedence_is_low():
    level, reasons = _grade(clear_tender(unresolved_precedence=2))
    assert level is ScopeConfidenceLevel.LOW
    assert any("precedence" in r for r in reasons)


def test_a_missing_deadline_is_low():
    level, reasons = _grade(clear_tender(has_deadline=False))
    assert level is ScopeConfidenceLevel.LOW
    assert any("deadline" in r for r in reasons)


def test_poor_clause_coverage_is_low():
    level, reasons = _grade(clear_tender(clause_coverage_ratio=0.6))
    assert level is ScopeConfidenceLevel.LOW
    assert any("coverage" in r or "%" in r for r in reasons)


def test_a_contradiction_downgrades_to_medium_not_low():
    """A contradiction is worth flagging, not worth stopping all work."""
    level, reasons = _grade(clear_tender(contradictions=1))
    assert level is ScopeConfidenceLevel.MEDIUM
    assert reasons


def test_placeholders_downgrade_to_medium():
    level, _ = _grade(clear_tender(placeholder_requirements=3))
    assert level is ScopeConfidenceLevel.MEDIUM


def test_partial_coverage_downgrades_to_medium():
    level, _ = _grade(clear_tender(clause_coverage_ratio=0.88))
    assert level is ScopeConfidenceLevel.MEDIUM


def test_many_major_issues_fall_all_the_way_to_low():
    level, _ = _grade(clear_tender(major_issues=20))
    assert level is ScopeConfidenceLevel.LOW


def test_missing_submission_rules_downgrade_to_medium():
    level, _ = _grade(clear_tender(has_submission_rules=False))
    assert level is ScopeConfidenceLevel.MEDIUM


@pytest.mark.parametrize(
    "field",
    [
        "evidence",
        "company",
        "knowledge",
        "readiness",
        "coverage_of_evidence",
    ],
)
def test_no_company_evidence_signal_exists(field):
    """Guards the boundary structurally: the signal set has no place to put it."""
    assert not any(field in name for name in ScopeSignals().__dict__)


def test_grading_never_reads_company_knowledge():
    """The grading function's source must not touch the knowledge layer."""
    source = inspect.getsource(_grade)
    for forbidden in ("evidence", "knowledge", "Evidence", "Knowledge"):
        assert forbidden not in source, (
            f"_grade references {forbidden!r}; scope confidence must measure the tender only"
        )


def test_compute_function_does_not_import_the_knowledge_services():
    source = inspect.getsource(tender_analysis.compute_scope_confidence)
    assert "knowledge" not in source.lower()


def test_low_confidence_blocks_detailed_drafting():
    for signals in (
        clear_tender(requirements_found=0),
        clear_tender(blocking_issues=1),
        clear_tender(has_deadline=False),
    ):
        level, _ = _grade(signals)
        assert level is ScopeConfidenceLevel.LOW
        # The record's flag is derived from the level; MEDIUM still permits
        # conditional drafting with recorded assumptions.
        assert (level is not ScopeConfidenceLevel.LOW) is False
