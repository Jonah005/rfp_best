from __future__ import annotations

from app.core.permissions import ALL_PERMISSIONS, ROLE_PERMISSIONS, Perm, RoleCode


def test_every_role_has_a_permission_set():
    for role in RoleCode:
        assert role in ROLE_PERMISSIONS, f"{role} has no permission set"


def test_auditor_can_read_but_never_write():
    perms = ROLE_PERMISSIONS[RoleCode.AUDITOR]
    assert Perm.AUDIT_READ in perms
    writes = {
        p
        for p in perms
        if any(p.value.endswith(s) for s in (":write", ":manage", ":approve", ":decide"))
    }
    assert writes == set(), f"auditor must not hold write permissions, has {writes}"


def test_restricted_classes_are_not_granted_broadly():
    """Restricted data must not leak in through a general contributor role."""
    restricted = {
        Perm.PRICING_READ,
        Perm.HR_RECORD_READ,
        Perm.LEGAL_PRIVILEGED_READ,
        Perm.PERSONAL_DATA_READ,
    }
    for role in (
        RoleCode.TECHNICAL_CONTRIBUTOR,
        RoleCode.SOLUTION_ARCHITECT,
        RoleCode.PROJECT_MANAGER,
        RoleCode.SECURITY,
    ):
        overlap = ROLE_PERMISSIONS[role] & restricted
        assert overlap == set(), f"{role} unexpectedly holds {overlap}"


def test_hr_holds_hr_records_and_commercial_holds_pricing():
    assert Perm.HR_RECORD_READ in ROLE_PERMISSIONS[RoleCode.HR]
    assert Perm.PRICING_READ in ROLE_PERMISSIONS[RoleCode.COMMERCIAL]
    assert Perm.PRICING_READ not in ROLE_PERMISSIONS[RoleCode.HR]
    assert Perm.HR_RECORD_READ not in ROLE_PERMISSIONS[RoleCode.COMMERCIAL]


def test_release_approval_is_separated_from_release_submission():
    """Maker-checker starts here: no single role holds both halves."""
    for role, perms in ROLE_PERMISSIONS.items():
        assert not (Perm.RELEASE_SUBMIT in perms and Perm.RELEASE_APPROVE in perms), (
            f"{role} holds both submit and approve"
        )


def test_permission_codes_are_unique():
    codes = [p.value for p in ALL_PERMISSIONS]
    assert len(codes) == len(set(codes))
