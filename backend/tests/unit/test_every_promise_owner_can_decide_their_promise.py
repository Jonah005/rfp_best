"""A promise routed to a role must be decidable by someone holding that role.

A promise is routed to the role that would stand behind it, and the service
accepts a decision only from a holder of exactly that role. The route used to
require the general ``approval:decide`` permission on top, which most of those
roles do not carry -- so a Security or Solution Architect lead was named as the
approver and then refused. The invariant is that every role a promise can be
routed to reaches the route.
"""

from __future__ import annotations

from app.api.v1.artifacts import router
from app.core.permissions import COMMITMENT_APPROVER_ROLES, ROLE_PERMISSIONS, Perm, RoleCode


def _commitment_route():
    return next(r for r in router.routes if r.path.endswith("/commitments/{commitment_id}/approve"))


def test_the_commitment_route_does_not_demand_the_general_approval_permission():
    route = _commitment_route()
    required = set()
    for dep in route.dependencies:
        closure = getattr(dep.dependency, "__closure__", None) or ()
        for cell in closure:
            value = cell.cell_contents
            if isinstance(value, (tuple, list, set, frozenset)):
                required |= {v for v in value if isinstance(v, Perm)}
            elif isinstance(value, Perm):
                required.add(value)
    assert Perm.APPROVAL_DECIDE not in required


def test_every_role_a_promise_can_be_routed_to_reaches_the_route():
    route = _commitment_route()
    required = set()
    for dep in route.dependencies:
        for cell in getattr(dep.dependency, "__closure__", None) or ():
            value = cell.cell_contents
            if isinstance(value, (tuple, list, set, frozenset)):
                required |= {v for v in value if isinstance(v, Perm)}
            elif isinstance(value, Perm):
                required.add(value)
    for code in COMMITMENT_APPROVER_ROLES:
        granted = ROLE_PERMISSIONS[RoleCode(code)]
        assert required <= granted, f"{code} cannot reach the route that decides its promises"
