"""A department's name is not decoration.

It goes into the copilot's authorized context -- the model reads "Asked HR:"
and may attribute a claim to that name -- into the department picker on a
cross-department request, and into every notification title. It was being
derived as `code.value.replace("_", " ").title()`, which is correct for eight
of the eleven departments and wrong for every acronym: HR rendered as "Hr" and
PMO as "Pmo".

The failure is small and permanent, and it lands on exactly the departments
people say out loud. A rule that is right most of the time is worse here than
a list that is right every time.
"""

from __future__ import annotations

import pytest

from app.models.enums import DEPARTMENT_DISPLAY_NAMES, DepartmentCode, department_display_name


@pytest.mark.parametrize("code", list(DepartmentCode), ids=lambda c: c.value)
def test_every_department_has_a_name_written_for_people(code: DepartmentCode):
    """A code added without a name would silently fall back to the old rule,
    which is how the acronyms broke in the first place."""
    assert code in DEPARTMENT_DISPLAY_NAMES, (
        f"{code.value} has no display name, so it falls back to .title() -- "
        "check whether that is right for this code before deleting this test."
    )


def test_acronyms_survive():
    """The whole point. `.title()` produced "Hr" and "Pmo"."""
    assert department_display_name(DepartmentCode.HR) == "HR"
    assert department_display_name(DepartmentCode.PMO) == "PMO"


def test_ordinary_names_are_still_readable():
    assert department_display_name(DepartmentCode.BID_MANAGEMENT) == "Bid Management"
    assert department_display_name(DepartmentCode.SECURITY) == "Security"


def test_an_unknown_code_degrades_rather_than_raising():
    """A department code that reaches this before somebody names it should
    render readably, not take down the copilot's context assembly."""
    assert department_display_name("FUTURE_TEAM") == "Future Team"


def test_seeding_repairs_a_name_that_drifted():
    """Existing organizations already hold "Hr". Seeding owns the name and is
    idempotent, so re-running it is the repair path -- otherwise the fix only
    reaches organizations created after it."""
    import inspect

    from app.db import seed

    source = inspect.getsource(seed)
    assert "department_display_name" in source
    assert "dept.name = expected" in source
