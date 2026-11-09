"""A requirement nobody can own must not vanish quietly.

`generate_tasks` skips any requirement whose category maps to no department, or
to a department this organization has not configured. The skip is right -- the
alternative is filing work somewhere plausible and losing it -- but it was
silent, and silence made it invisible.

An organization with no departments configured skipped EVERY requirement and
reported `tasks_created=0` with no reason. That is what a fixture bid did:
assessment looked healthy at 17 requirements assessed, and every stage after it
-- drafting, artifacts, cross-department requests, assembly, release -- was
unreachable, because all of them hang off a task. The seeded demo organization
has departments, so it never showed there.

Fifth defect in this codebase with the shape "an optimisation that became a
silent skip": a numbered-clause exemption, a decomposition fallback, a DELETE
with no tenant context, a retirement rule, a 25-character minimum that deleted
"Working days".

The decision is tested through `ungrouped_reason`, which was extracted from
`generate_tasks` for the purpose. Asserting it by reading the function's source
would be the cover-that-cannot-fail this project has already been burned by.
"""

from __future__ import annotations

from app.models.enums import DepartmentCode, RequirementCategory
from app.services.tasks import DEPARTMENT_BY_CATEGORY, ungrouped_reason


def test_a_grouped_requirement_has_no_reason() -> None:
    configured = {DepartmentCode.SECURITY}
    assert ungrouped_reason(None, DepartmentCode.SECURITY, configured) is None


def test_an_unmapped_category_says_so() -> None:
    reason = ungrouped_reason(None, None, {DepartmentCode.SECURITY})
    assert reason == "no department owns this category"


def test_an_unconfigured_department_says_something_different() -> None:
    """The two reasons need different work from different people -- a taxonomy
    change versus an administrator adding a department. One combined count
    sends somebody looking in the wrong place."""
    reason = ungrouped_reason(None, DepartmentCode.SECURITY, set())
    assert reason is not None
    assert "SECURITY" in reason
    assert reason != "no department owns this category"


def test_an_organization_with_no_departments_gives_a_reason_for_every_row() -> None:
    """The exact condition that made a whole bid unreachable: nothing
    configured, so every mapped category is skipped -- and now every skip
    carries a reason instead of a bare `continue`."""
    mapped = [c for c in RequirementCategory if DEPARTMENT_BY_CATEGORY.get(c) is not None]
    assert mapped, "DEPARTMENT_BY_CATEGORY has lost its entries"
    for category in mapped:
        reason = ungrouped_reason(category, DEPARTMENT_BY_CATEGORY[category], set())
        assert reason and "not configured" in reason


def test_every_category_still_maps_somewhere() -> None:
    """The mapping going empty would send every requirement down the ungrouped
    path while every other test kept passing -- the same shape as a frozenset
    that quietly lost its contents."""
    mapped = {c for c in RequirementCategory if DEPARTMENT_BY_CATEGORY.get(c) is not None}
    assert len(mapped) >= 5, "DEPARTMENT_BY_CATEGORY has lost most of its entries"
