"""The fixture organization's departments and people.

Shared by `seed_fixture_bid.py` and `walk_gate_two.py` because both need it and
two copies would drift. The seeder needs it BEFORE assessment: `generate_tasks`
skips any requirement whose category maps to a department the organization does
not have, so an org with no departments produces no tasks, and every stage after
that is unreachable. The walk needs the same people to act as.

## Enough cast to exercise the governed paths, and no more

Assignment is governed, and correctly: "the system routes work to a department;
the department's head names the person". A head may assign only within their own
department; the bid manager's `task:assign` is a fallback for departments with
no head. So:

  * Security and Commercial have heads, which closes the bid-manager fallback
    on those departments and forces the delegated path.
  * Security has two members, so its head can assign to somebody other than
    themselves -- a head who can only assign to themselves proves nothing that
    unscoped assignment would not.
  * Every other department is created without a head, so the fallback path
    stays reachable too.

Idempotent, and scoped to the fixture organization, so
`seed_fixture_bid.py --reset` wipes it and either script rebuilds it.
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.permissions import COMMITMENT_APPROVER_ROLES, RoleCode
from app.models.enums import DepartmentCode
from app.services import identity

PASSWORD = "Fixture-Dev-2026"

CAST: tuple[tuple[str, str, RoleCode, DepartmentCode], ...] = (
    ("sara@cybranytech.example", "Sara K.", RoleCode.BID_MANAGER, DepartmentCode.BID_MANAGEMENT),
    ("ahmed@cybranytech.example", "Ahmed Hassan", RoleCode.SECURITY, DepartmentCode.SECURITY),
    ("zain@cybranytech.example", "Zain Malik", RoleCode.SECURITY, DepartmentCode.SECURITY),
    ("omar@cybranytech.example", "Omar T.", RoleCode.COMMERCIAL, DepartmentCode.COMMERCIAL),
    ("nadia@cybranytech.example", "Nadia R.", RoleCode.REVIEWER, DepartmentCode.QUALITY),
    # --- everybody a commitment can be routed to -----------------------------
    #
    # A PROPOSED_COMMITMENT is routed to the role that would stand behind the
    # promise, and the drafting model picks that role. It is not told which
    # roles this customer has staffed, and it must not be: a model that trims
    # its answer to the org chart is doing a permission calculation.
    #
    # These people exist because of two runs and one lesson. The first routed a
    # delivery schedule to PROJECT_MANAGER and a technical undertaking to
    # SOLUTION_ARCHITECT; the cast was extended to cover exactly those two. The
    # next run, on a different task, routed an NDA clause to LEGAL and stalled
    # in exactly the same way. Adding LEGAL would have bought one more run.
    #
    # Fix the pattern: every role in `COMMITMENT_APPROVER_ROLES` can turn up, so
    # every one of them is staffed, and `ensure` asserts that below rather than
    # trusting this list to stay in step. A company that cannot approve a legal
    # undertaking or a price cannot bid, so this is what a real organization
    # looks like, not fixture padding.
    #
    # The orphaned-role case is not abandoned: it lives in
    # `test_a_commitment_routed_to_nobody_says_so`, deterministic and free
    # instead of costing a model call and a deliberately broken fixture.
    ("layla@cybranytech.example", "Layla F.", RoleCode.PROJECT_MANAGER, DepartmentCode.PMO),
    (
        "yousef@cybranytech.example",
        "Yousef A.",
        RoleCode.SOLUTION_ARCHITECT,
        DepartmentCode.SOLUTION_ARCHITECTURE,
    ),
    ("rania@cybranytech.example", "Rania S.", RoleCode.LEGAL, DepartmentCode.LEGAL),
    ("khalid@cybranytech.example", "Khalid B.", RoleCode.FINANCE, DepartmentCode.FINANCE),
    ("dana@cybranytech.example", "Dana M.", RoleCode.HR, DepartmentCode.HR),
    ("tariq@cybranytech.example", "Tariq N.", RoleCode.SALES, DepartmentCode.SALES),
    (
        "hassan@cybranytech.example",
        "Hassan I.",
        RoleCode.TECHNICAL_CONTRIBUTOR,
        DepartmentCode.PRODUCT,
    ),
    (
        "mariam@cybranytech.example",
        "Mariam Q.",
        RoleCode.EXECUTIVE_APPROVER,
        DepartmentCode.BID_MANAGEMENT,
    ),
)

HEADS: dict[DepartmentCode, str] = {
    DepartmentCode.SECURITY: "ahmed@cybranytech.example",
    DepartmentCode.COMMERCIAL: "omar@cybranytech.example",
}


DEFAULT_DOMAIN = "cybranytech.example"


def _address(email: str, domain: str) -> str:
    """The same person, at another fixture organization's domain.

    Email addresses are unique across the whole system, not per organization
    (`test_email_uniqueness.py`), so a second fixture org that reused this cast
    verbatim failed on its first user with "already registered". Each fixture
    organization therefore gets the same cast under its own domain.
    """
    return email.rsplit("@", 1)[0] + "@" + domain


def ensure(session, ctx, org, *, quiet: bool = False, domain: str = DEFAULT_DOMAIN):
    """Departments, roles and people. Returns (users by email, departments by code).

    `domain` gives each fixture organization its own addresses; the default is
    the Cybranytech one, which `walk_gate_two.py` signs in with.
    """
    from app.models import Department, User
    from app.models.enums import department_display_name

    departments: dict[DepartmentCode, Department] = {}
    for code in DepartmentCode:
        row = session.execute(
            select(Department).where(Department.org_id == org.id, Department.code == code)
        ).scalar_one_or_none()
        if row is None:
            row = Department(org_id=org.id, code=code, name=department_display_name(code))
            session.add(row)
            session.flush()
        departments[code] = row

    identity.sync_permission_catalogue(session)
    identity.provision_roles(session, org.id)

    users: dict[str, User] = {}
    for cast_email, name, role, dept in CAST:
        email = _address(cast_email, domain)
        row = session.execute(
            select(User).where(User.org_id == org.id, User.email == email)
        ).scalar_one_or_none()
        if row is None:
            row = identity.create_user(
                session,
                ctx,
                email=email,
                display_name=name,
                password=PASSWORD,
                role_codes_to_assign=[role.value],
                department_ids_to_assign=[departments[dept].id],
            )
        users[email] = row

    for dept_code, email in HEADS.items():
        head = users[_address(email, domain)]
        # Two separate facts, and both are needed: the role says this person MAY
        # act as a head, the column says of WHICH department. Either alone
        # authorises nothing.
        identity.assign_role(
            session, ctx, user_id=head.id, role_code=RoleCode.DEPARTMENT_HEAD.value
        )
        if departments[dept_code].head_user_id != head.id:
            departments[dept_code].head_user_id = head.id
    session.flush()

    # Every role a commitment can be routed to has somebody behind it.
    #
    # This is the assertion the cast kept failing by hand. Twice the walk got
    # to stage 7, found a promise routed to a role nobody held, and reported it
    # three stages after the cause -- once for PROJECT_MANAGER and
    # SOLUTION_ARCHITECT, once for LEGAL. The gap is knowable here, at setup,
    # before a single model call has been paid for, so it is checked here.
    #
    # Read from the database rather than from CAST: a person is staffed when
    # the role rows exist, and a list that merely claims to cover the set is
    # what produced the last two rounds of this.
    unstaffed = sorted(
        COMMITMENT_APPROVER_ROLES
        - {code for user in users.values() for code in identity.role_codes(session, user)}
    )
    if unstaffed:
        raise SystemExit(
            "The fixture organization staffs nobody for "
            + ", ".join(unstaffed)
            + ".\nA commitment routed to one of those roles can never be approved, and the "
            "walk\nwould report it at stage 7 rather than here. Add a person to CAST for "
            "each,\nor narrow COMMITMENT_APPROVER_ROLES if the role genuinely cannot own a "
            "promise."
        )

    if not quiet:
        print(
            f"organization set up: {len(departments)} departments, {len(users)} people, "
            f"{len(HEADS)} head(s), every approver role staffed"
        )
    return users, departments
