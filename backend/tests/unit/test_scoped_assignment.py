"""Who may put whom on a task.

The system routes work to a department; the department's head names the person.
That division is the whole design, and it is sharper than it first looks: a
headed department is the head's *exclusively*, not somewhere the bid manager may
also reach. Sara decides what the bid needs and who is accountable for it. She
does not decide which of Leila's people is free on Thursday, because she does
not know, and a staffing decision made by someone who does not know is how work
lands on whoever was least able to refuse it.

So there are two doors and only one is open at a time:

* A department WITH a head: only that head, holding
  `task:assign_in_department`, and only onto a member of that department.
  `task:assign` does not open this door.
* A department with NO head, or a task belonging to no department: the bid
  manager's `task:assign`, any owner. A fallback that keeps work movable, and
  one that closes the moment a head is named.

Three properties are worth stating because each of them is a way this could go
quietly wrong:

1. The bid manager being refused is not an accident of permissions -- it is the
   rule. A test that lets `task:assign` through everywhere would still pass on
   the old code and prove nothing about this one.
2. The owner must be a member of the department. A head who can assign outside
   their own department can hand work -- and with it, that bid's evidence -- to
   any account in the organization, which is unscoped assignment again, one step
   down and harder to see.
3. Nothing is unassignable. Take the fallback away and a department nobody has
   headed yet has tasks no living user can staff.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.context import ExecutionContext
from app.core.errors import PermissionDenied
from app.core.permissions import ROLE_PERMISSIONS, Perm, RoleCode
from app.services.tasks import _authorise_assignment, assignable_owners

ORG = uuid.uuid4()

SECURITY = uuid.uuid4()  # headed by Ahmed
LEGAL = uuid.uuid4()  # no head

AHMED = uuid.uuid4()  # head of Security, and in it
NADIA = uuid.uuid4()  # in Security, not its head
LEILA = uuid.uuid4()  # in Legal
SARA = uuid.uuid4()  # bid manager, in neither


class _Person:
    def __init__(self, user_id, name):
        self.id = user_id
        self.display_name = name
        self.email = f"{name.lower()}@acme-cyber.example.com"
        self.is_active = True


PEOPLE = {
    AHMED: _Person(AHMED, "Ahmed"),
    NADIA: _Person(NADIA, "Nadia"),
    LEILA: _Person(LEILA, "Leila"),
    SARA: _Person(SARA, "Sara"),
}


class _Department:
    def __init__(self, dept_id, name, head_user_id):
        self.id = dept_id
        self.name = name
        self.head_user_id = head_user_id


DEPARTMENTS = {
    SECURITY: _Department(SECURITY, "Security", AHMED),
    LEGAL: _Department(LEGAL, "Legal", None),
}

MEMBERSHIP = {(AHMED, SECURITY), (NADIA, SECURITY), (LEILA, LEGAL)}


class _Task:
    def __init__(self, department_id):
        self.id = uuid.uuid4()
        self.org_id = ORG
        self.department_id = department_id


class _Session:
    """Departments, people, membership and one task -- what the rule reads."""

    def __init__(self, task=None):
        self._task = task

    def get(self, model, key):
        if model.__name__ == "Task":
            return self._task if self._task and key == self._task.id else None
        if model.__name__ == "User":
            return PEOPLE.get(key)
        return DEPARTMENTS.get(key)

    def execute(self, statement):
        text = str(statement)
        params = statement.compile().params
        assert params.get("org_id_1") == ORG, "every lookup here must be tenant-scoped"

        if "user_id_1" in params:
            # The backwards check: is this one person in this one department?
            pair = (params["user_id_1"], params["department_id_1"])
            return _One(pair in MEMBERSHIP)
        if "department_id_1" in params:
            # The forwards answer: everyone in this department.
            department_id = params["department_id_1"]
            return _Rows([PEOPLE[u] for u, d in sorted(MEMBERSHIP) if d == department_id])
        if "app_user" in text:
            return _Rows(list(PEOPLE.values()))
        raise AssertionError(f"unexpected query: {text[:90]}")


class _One:
    def __init__(self, found):
        self._found = found

    def scalar_one_or_none(self):
        return object() if self._found else None


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


def _ctx(actor, *perms):
    return ExecutionContext(
        org_id=ORG, actor_user_id=actor, permissions=frozenset(p.value for p in perms)
    )


BID_MANAGER = _ctx(SARA, Perm.TASK_ASSIGN)
HEAD = _ctx(AHMED, Perm.TASK_ASSIGN_IN_DEPARTMENT)
NOBODY = _ctx(NADIA, Perm.TASK_READ)


def _assign(ctx, task, owner):
    _authorise_assignment(_Session(task), ctx, task=task, owner_user_id=owner)


def _offer(ctx, task):
    return assignable_owners(_Session(task), ctx, task_id=task.id)


def _ids(offer):
    return {c["id"] for c in offer["candidates"]}


# --- the head names the person ----------------------------------------------


def test_a_head_assigns_within_their_own_department():
    _assign(HEAD, _Task(SECURITY), NADIA)


def test_a_head_may_take_the_work_themselves():
    _assign(HEAD, _Task(SECURITY), AHMED)


def test_a_head_is_offered_their_own_department_and_nobody_else():
    offer = _offer(HEAD, _Task(SECURITY))
    assert offer["scope"] == "DEPARTMENT"
    assert _ids(offer) == {str(AHMED), str(NADIA)}


# --- the bid manager picks from the same people -----------------------------


def test_the_bid_manager_assigns_within_a_headed_department():
    """Sara may name anyone *in* the department. Having a head means the work is
    routed to that department, not that she is locked out of choosing."""
    _assign(BID_MANAGER, _Task(SECURITY), NADIA)


def test_the_bid_manager_cannot_reach_outside_a_headed_department():
    with pytest.raises(PermissionDenied) as exc:
        _assign(BID_MANAGER, _Task(SECURITY), SARA)
    assert "not in Security" in str(exc.value)


def test_the_bid_manager_is_offered_the_departments_people_head_first():
    offer = _offer(BID_MANAGER, _Task(SECURITY))
    assert offer["scope"] == "DEPARTMENT"
    assert [c["id"] for c in offer["candidates"]][0] == str(AHMED)
    assert _ids(offer) == {str(AHMED), str(NADIA)}
    assert "headed by Ahmed" in offer["reason"]


def test_a_person_is_labelled_head_or_team_member_in_their_department():
    from app.services.tasks import _person

    class _Dept:
        def __init__(self, name, head):
            self.name, self.head_user_id = name, head

    person = PEOPLE[NADIA]
    person.departments = [_Dept("Security", AHMED)]
    person.roles = []
    assert _person(person)["positions"] == [{"department": "Security", "position": "Team member"}]
    person.departments = [_Dept("Security", NADIA)]
    assert _person(person)["positions"] == [{"department": "Security", "position": "Head"}]


# --- but nothing is left unassignable ---------------------------------------


def test_the_bid_manager_staffs_a_department_with_no_head():
    """The fallback that keeps work movable.

    Legal has nobody named. Without this, a department that has not been given
    a head yet has tasks no living user can assign -- and the ADX bid has four
    such departments today.
    """
    _assign(BID_MANAGER, _Task(LEGAL), LEILA)


def test_the_bid_manager_is_not_bound_by_membership_in_an_unheaded_department():
    """No head means no department opinion to respect, so she may reach outside
    it. Sara is in neither department and can still be given the work."""
    _assign(BID_MANAGER, _Task(LEGAL), SARA)


def test_the_bid_manager_staffs_a_task_with_no_department_at_all():
    _assign(BID_MANAGER, _Task(None), NADIA)


def test_the_fallback_is_offered_as_the_whole_organization():
    offer = _offer(BID_MANAGER, _Task(LEGAL))
    assert offer["scope"] == "UNHEADED"
    assert _ids(offer) == {str(u) for u in PEOPLE}


def test_naming_a_head_limits_the_bid_manager_to_that_department():
    """The two branches are the same department in two states, so this asserts
    the transition rather than two unrelated cases."""
    legal = DEPARTMENTS[LEGAL]
    _assign(BID_MANAGER, _Task(LEGAL), SARA)
    legal.head_user_id = LEILA
    try:
        _assign(BID_MANAGER, _Task(LEGAL), LEILA)
        with pytest.raises(PermissionDenied) as exc:
            _assign(BID_MANAGER, _Task(LEGAL), SARA)
        assert "not in Legal" in str(exc.value)
    finally:
        legal.head_user_id = None


# --- the refusals that protect the evidence ---------------------------------


def test_a_head_cannot_assign_outside_their_department():
    """The half people skip.

    Ahmed heads Security, so the task passes. Leila is in Legal, and letting
    this through would let any head route any of their tasks -- and that bid's
    evidence -- to any account in the organization.
    """
    with pytest.raises(PermissionDenied) as exc:
        _assign(HEAD, _Task(SECURITY), LEILA)
    assert "not in Security" in str(exc.value)


def test_a_head_cannot_touch_another_department_s_task():
    with pytest.raises(PermissionDenied):
        _assign(HEAD, _Task(LEGAL), LEILA)


def test_a_head_is_offered_nobody_on_another_department_s_task():
    offer = _offer(HEAD, _Task(LEGAL))
    assert offer["scope"] == "NONE"
    assert offer["candidates"] == []


def test_being_named_head_without_the_role_is_not_enough():
    """Both facts are required, and this is the half the column cannot enforce.

    `department.head_user_id` says which department; the role says the person
    may act as a head at all. Someone named in the column but stripped of the
    role -- a leaver mid-handover -- must not still be assigning.
    """
    named_but_unroled = _ctx(AHMED, Perm.TASK_READ)
    with pytest.raises(PermissionDenied) as exc:
        _assign(named_but_unroled, _Task(SECURITY), NADIA)
    assert "task:assign_in_department" in str(exc.value)


def test_holding_the_role_while_heading_nothing_is_not_enough():
    """And the other half. If the role alone sufficed, granting it would be
    granting assignment over every department at once."""
    stranger = _ctx(uuid.uuid4(), Perm.TASK_ASSIGN_IN_DEPARTMENT)
    with pytest.raises(PermissionDenied):
        _assign(stranger, _Task(SECURITY), NADIA)


def test_someone_with_neither_permission_is_refused_everywhere():
    for task in (_Task(SECURITY), _Task(LEGAL), _Task(None)):
        with pytest.raises(PermissionDenied):
            _assign(NOBODY, task, NADIA)
        assert _offer(NOBODY, task)["candidates"] == []


# --- the shape of the grant --------------------------------------------------


def test_a_department_head_cannot_assign_unscoped():
    granted = ROLE_PERMISSIONS[RoleCode.DEPARTMENT_HEAD]
    assert Perm.TASK_ASSIGN not in granted
    assert Perm.TASK_ASSIGN_IN_DEPARTMENT in granted


def test_a_department_head_gets_nothing_else():
    """A staffing role is a staffing role. It reads people and tasks to do that
    job and holds no approval, release or evidence power."""
    granted = {p.value for p in ROLE_PERMISSIONS[RoleCode.DEPARTMENT_HEAD]}
    assert granted == {"task:assign_in_department", "task:read", "user:read"}


def test_the_route_cannot_decide_this_alone():
    """The router admits both permissions and the service picks between them.

    If the route went back to `require(Perm.TASK_ASSIGN)`, a department head
    would be rejected before the scoped check ran and the feature would be
    silently dead. If it dropped the guard entirely, the scoped check would be
    the only thing between any authenticated user and assignment.
    """
    import inspect

    from app.api.v1 import tasks as route_module

    decorator = inspect.getsource(route_module).split("def assign_owner")[0]
    guard = decorator.rsplit("@router.post", 1)[-1]
    assert "require_any(Perm.TASK_ASSIGN, Perm.TASK_ASSIGN_IN_DEPARTMENT)" in guard


def test_the_service_re_checks_rather_than_trusting_the_route():
    import inspect

    from app.services import tasks as task_service

    assert "_authorise_assignment" in inspect.getsource(task_service.assign_owner)


# --- and the two answers must agree ------------------------------------------


def test_everyone_offered_would_actually_be_accepted():
    """The property that keeps the forward answer honest.

    A name in the dropdown that `_authorise_assignment` would reject is a screen
    offering a control that 403s. Checked across every actor and every kind of
    department, since each takes a different branch on both sides.
    """
    for ctx in (BID_MANAGER, HEAD, NOBODY):
        for department in (SECURITY, LEGAL, None):
            task = _Task(department)
            for candidate in _offer(ctx, task)["candidates"]:
                _assign(ctx, task, uuid.UUID(candidate["id"]))


def test_an_empty_offer_means_the_check_would_refuse():
    """The other direction: no candidates must not mean "we did not look"."""
    for ctx, department in (
        (HEAD, LEGAL),
        (HEAD, None),
        (NOBODY, SECURITY),
    ):
        task = _Task(department)
        assert _offer(ctx, task)["candidates"] == []
        for owner in (AHMED, NADIA, LEILA):
            with pytest.raises(PermissionDenied):
                _assign(ctx, task, owner)


def test_the_offer_never_decides_anything():
    """It shapes a screen; it does not grant. `assign_owner` re-checks."""
    import inspect

    from app.services import tasks as task_service

    assert "assignable_owners" not in inspect.getsource(task_service.assign_owner)
