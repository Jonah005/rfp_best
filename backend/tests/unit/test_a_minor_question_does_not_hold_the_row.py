"""A MINOR customer question is shown on the row and does not hold it.

With questions linked only to the requirements they concern, 22 of the 28 rows
waiting on the customer in the Wadi Lumen fixture had current, approved proof,
and most of what held them was MINOR. See `assessment.holds_the_row`.
"""

from __future__ import annotations

import pytest

from app.models.enums import ClarificationSeverity
from app.services.assessment import holds_the_row


@pytest.mark.parametrize(
    ("question", "severity", "holds"),
    [
        ("Is C# mandatory or a preference?", ClarificationSeverity.MAJOR, True),
        ("When is the deadline?", ClarificationSeverity.BLOCKING, True),
        ("Which browser versions?", ClarificationSeverity.MINOR, False),
        (None, ClarificationSeverity.MAJOR, False),  # an internal note never gates
        ("   ", ClarificationSeverity.BLOCKING, False),
        ("Which browser versions?", "MINOR", False),  # raw column values too
        ("Which browser versions?", "MAJOR", True),
    ],
)
def test_holds_the_row(question, severity, holds):
    assert holds_the_row(question, severity) is holds
