"""A department offered in "Ask another department" must be able to answer."""

from app.core.permissions import ROLE_PERMISSIONS, Perm, RoleCode

DEPARTMENT_ROLES = [
    RoleCode.SALES,
    RoleCode.HR,
    RoleCode.COMMERCIAL,
    RoleCode.FINANCE,
    RoleCode.LEGAL,
    RoleCode.TECHNICAL_CONTRIBUTOR,
    RoleCode.SOLUTION_ARCHITECT,
    RoleCode.PROJECT_MANAGER,
    RoleCode.SECURITY,
    RoleCode.BID_MANAGER,
]


def test_each_working_role_can_answer_a_request_but_not_grant_by_default():
    for role in DEPARTMENT_ROLES:
        assert Perm.REQUEST_RESOLVE in ROLE_PERMISSIONS[role], role
    for role in (RoleCode.SOLUTION_ARCHITECT, RoleCode.PROJECT_MANAGER, RoleCode.SECURITY):
        assert Perm.GRANT_CREATE not in ROLE_PERMISSIONS[role]
