"""The person approving the library does not get drafting work by default."""

from types import SimpleNamespace

from app.services.tasks import can_work_tasks


def _who(*codes):
    return SimpleNamespace(is_active=True, roles=[SimpleNamespace(code=c) for c in codes])


def test_a_knowledge_admin_cannot_be_the_default_owner():
    assert not can_work_tasks(None, _who("KNOWLEDGE_ADMIN"))
    assert not can_work_tasks(None, _who("KNOWLEDGE_ADMIN", "DEPARTMENT_HEAD"))


def test_a_contributor_can():
    assert can_work_tasks(None, _who("SOLUTION_ARCHITECT"))
    assert can_work_tasks(None, _who("BID_MANAGER", "DEPARTMENT_HEAD"))
