"""Development seed.

Creates the permission catalogue, one demo organization with the full role
catalogue, departments and a few users. Idempotent: safe to run repeatedly.

Run with:  python -m app.db.seed
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.permissions import RoleCode
from app.db.session import apply_tenant_context, get_session_factory
from app.models import Department, Organization
from app.models.enums import DepartmentCode, department_display_name
from app.services import identity

DEMO_ORG_SLUG = "acme-cyber"
DEMO_PASSWORD = "ChangeMe-Dev-2026"

DEMO_USERS = [
    (
        "sara.k@acme-cyber.example.com",
        "Sara K.",
        RoleCode.BID_MANAGER,
        DepartmentCode.BID_MANAGEMENT,
    ),
    ("ahmed.h@acme-cyber.example.com", "Ahmed Hassan", RoleCode.SECURITY, DepartmentCode.SECURITY),
    # A second person in Security, so delegated assignment has somewhere to go.
    # With one member, a department head can only ever assign to themselves and
    # the scoped path proves nothing that unscoped assignment would not.
    (
        "youssef.a@acme-cyber.example.com",
        "Youssef A.",
        RoleCode.TECHNICAL_CONTRIBUTOR,
        DepartmentCode.SECURITY,
    ),
    ("nadia.r@acme-cyber.example.com", "Nadia R.", RoleCode.HR, DepartmentCode.HR),
    ("omar.r@acme-cyber.example.com", "Omar R.", RoleCode.SALES, DepartmentCode.SALES),
    (
        "leila.m@acme-cyber.example.com",
        "Leila M.",
        RoleCode.COMMERCIAL,
        DepartmentCode.COMMERCIAL,
    ),
    # Leila's second, for the same reason as Youssef above: a head with one
    # member can only assign to themselves.
    (
        "hana.s@acme-cyber.example.com",
        "Hana S.",
        RoleCode.COMMERCIAL,
        DepartmentCode.COMMERCIAL,
    ),
    # Without this account nobody in the demo org holds evidence:approve, and
    # the approval gate is unreachable: extracted evidence stays pending
    # forever, assessment matches nothing, and the whole company side reads as
    # broken when it is only ungoverned.
    (
        "knowledge@acme-cyber.example.com",
        "Knowledge Admin",
        RoleCode.KNOWLEDGE_ADMIN,
        DepartmentCode.QUALITY,
    ),
    # Same omission as the knowledge admin above, one gate further along.
    # release:approve and exception:approve belong to EXECUTIVE_APPROVER alone,
    # so without this account a bid can be assembled, validated and submitted
    # for release — and then stops, with no user in the org able to approve it
    # or to waive a validation finding. The last gate of the workflow is
    # unreachable, which reads as a broken release step rather than a missing
    # person.
    (
        "exec@acme-cyber.example.com",
        "Executive Approver",
        RoleCode.EXECUTIVE_APPROVER,
        DepartmentCode.BID_MANAGEMENT,
    ),
    # A department nobody belongs to is a dead end: a request raised to it is saved
    # and nobody is told. One person in each department a request can be sent to.
    (
        "arch@acme-cyber.example.com",
        "Rania A.",
        RoleCode.SOLUTION_ARCHITECT,
        DepartmentCode.SOLUTION_ARCHITECTURE,
    ),
    ("pmo@acme-cyber.example.com", "Karim P.", RoleCode.PROJECT_MANAGER, DepartmentCode.PMO),
    ("finance@acme-cyber.example.com", "Dina F.", RoleCode.FINANCE, DepartmentCode.FINANCE),
    ("legal@acme-cyber.example.com", "Tariq L.", RoleCode.LEGAL, DepartmentCode.LEGAL),
    # Product had nobody, so its work could only ever sit with the bid manager.
    (
        "product@acme-cyber.example.com",
        "Maya T.",
        RoleCode.TECHNICAL_CONTRIBUTOR,
        DepartmentCode.PRODUCT,
    ),
    ("admin@acme-cyber.example.com", "Org Admin", RoleCode.ORG_ADMIN, DepartmentCode.QUALITY),
    ("auditor@acme-cyber.example.com", "Auditor", RoleCode.AUDITOR, DepartmentCode.QUALITY),
]

# Who staffs each department's own work.
#
# Without a head, every task on every bid is assigned by the bid manager, and
# `task:assign_in_department` is code nothing exercises. Naming one head here is
# what makes the scoped path real in the demo: Ahmed can put his own people on
# Security tasks, Sara still owns the bid and still reassigns anything stuck,
# and Ahmed still cannot touch Legal's tasks or hand Security work to Leila.
#
# A head is a contributor first -- Ahmed keeps SECURITY and gains DEPARTMENT_HEAD
# alongside it. The staffing authority is an addition to someone's job, not a
# separate person who only assigns.
DEPARTMENT_HEADS: dict[DepartmentCode, str] = {
    DepartmentCode.SECURITY: "ahmed.h@acme-cyber.example.com",
    # Which departments a bid produces tasks for depends entirely on the tender.
    # The first real one here -- an exchange's IT services RFP -- generated work
    # for PMO, Commercial, Legal, Bid Management and Quality, and none at all
    # for Security: there were no security-category requirements to group. A
    # single head, on a department that happens to get no work, is a feature
    # nobody can see. So name a second, in a department that tenders reliably
    # touch, and the delegated path is exercised on ordinary bids.
    DepartmentCode.COMMERCIAL: "leila.m@acme-cyber.example.com",
}


def _remove_if_unused(session, dept) -> bool:
    """Delete a department nobody is in and nothing refers to. False if anything does."""
    from sqlalchemy.exc import IntegrityError

    from app.models import UserDepartment

    # Membership rows would be deleted with the department, so a department
    # anyone has been put in is never removed.
    if session.execute(
        select(UserDepartment.user_id).where(UserDepartment.department_id == dept.id).limit(1)
    ).first():
        return False
    try:
        with session.begin_nested():
            session.delete(dept)
            session.flush()
        return True
    except IntegrityError:
        return False


def seed() -> uuid.UUID:
    factory = get_session_factory()
    session = factory()
    try:
        session.begin()

        added = identity.sync_permission_catalogue(session)
        print(f"permissions: {added} added")

        org = session.execute(
            select(Organization).where(Organization.slug == DEMO_ORG_SLUG)
        ).scalar_one_or_none()
        if org is None:
            org = Organization(name="Acme Cyber", slug=DEMO_ORG_SLUG, residency_region="ae-central")
            session.add(org)
            session.flush()
            print(f"organization: created {org.id}")
        else:
            print(f"organization: exists {org.id}")

        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        # A department exists because someone works in it. Creating one for every
        # code in the enum left rooms nobody was in, and a request sent to one was
        # saved and never seen. So only the departments the demo people belong to
        # are made, and any older empty one is removed if nothing refers to it.
        staffed_codes = {code for _e, _n, _r, code in DEMO_USERS}
        departments: dict[DepartmentCode, Department] = {}
        for code in DepartmentCode:
            dept = session.execute(
                select(Department).where(Department.org_id == org.id, Department.code == code)
            ).scalar_one_or_none()
            if code not in staffed_codes:
                if dept is not None and _remove_if_unused(session, dept):
                    print(f"  removed empty department {code.value}")
                continue
            if dept is None:
                dept = Department(org_id=org.id, code=code, name=department_display_name(code))
                session.add(dept)
                session.flush()
            else:
                # Repair a name that predates the display-name table.
                expected = department_display_name(code)
                if dept.name != expected:
                    dept.name = expected
            departments[code] = dept
        print(f"departments: {len(departments)}")

        roles = identity.provision_roles(session, org.id)
        print(f"roles: {len(roles)}")

        for email, name, role, dept_code in DEMO_USERS:
            from app.models import User

            existing = session.execute(
                select(User).where(User.org_id == org.id, User.email == email)
            ).scalar_one_or_none()
            if existing:
                continue
            identity.create_user(
                session,
                ctx,
                email=email,
                display_name=name,
                password=DEMO_PASSWORD,
                role_codes_to_assign=[role.value],
                department_ids_to_assign=[departments[dept_code].id],
            )
        print(f"users: {len(DEMO_USERS)} ensured")

        from app.models import User

        for dept_code, email in DEPARTMENT_HEADS.items():
            head = session.execute(
                select(User).where(User.org_id == org.id, User.email == email)
            ).scalar_one_or_none()
            if head is None:
                print(f"  ! no account {email}; {dept_code.value} left without a head")
                continue
            department = departments[dept_code]
            # Two separate facts, and both are needed. The role says this person
            # may act as a head; the column says of which department. Either one
            # alone authorises nothing.
            identity.assign_role(
                session, ctx, user_id=head.id, role_code=RoleCode.DEPARTMENT_HEAD.value
            )
            if department.head_user_id != head.id:
                department.head_user_id = head.id
                session.flush()
        print(f"department heads: {len(DEPARTMENT_HEADS)} ensured")

        # Every department with people has a head; the rest are named here. Then
        # work nobody has picked up goes to its department's head.
        from app.services import tasks as task_service

        named = identity.settle_department_heads(session, ctx)
        print(f"department heads: {named} more named so every department has one")
        counts = task_service.settle_task_owners(session, ctx)
        print(
            f"tasks: {counts['filled']} given to a department head, "
            f"{counts['moved']} moved to the right department's head"
        )
        from app.services import clarifications as clarification_service

        routed = clarification_service.settle_clarification_owners(session, ctx)
        print(f"clarifications: {routed} routed to a department and its head")

        session.commit()
        print(f"\nseed complete. org_id={org.id}")
        print(f"sign in with any seeded email and password: {DEMO_PASSWORD}")
        return org.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def print_accounts() -> None:
    """Say which seeded account to use for what.

    Roles here are deliberately separated: the org admin manages users and
    cannot write bids, and release approval needs someone who did not submit.
    That is the product working as designed, but it is confusing at a blank
    login screen, so the seed says it out loud.
    """
    print("\n  WHO CAN DO WHAT (password for all: " + DEMO_PASSWORD + ")")
    print("    sara.k@acme-cyber.example.com     BID_MANAGER   the whole bid workflow")
    print("    knowledge@acme-cyber.example.com  KNOWLEDGE_ADMIN  approves evidence for reuse")
    print("    ahmed.h@acme-cyber.example.com    SECURITY + DEPARTMENT_HEAD (Security)")
    print("                                      contributes, and staffs Security's own tasks")
    print("    youssef.a@acme-cyber.example.com  TECHNICAL     in Security; Ahmed staffs him")
    print("    leila.m@acme-cyber.example.com    COMMERCIAL + DEPARTMENT_HEAD (Commercial)")
    print("    hana.s@acme-cyber.example.com     COMMERCIAL    in Commercial; Leila staffs her")
    print("    nadia.r@acme-cyber.example.com    HR            answers cross-department requests")
    print("    exec@acme-cyber.example.com       EXEC_APPROVER approves release, waives findings")
    print("    auditor@acme-cyber.example.com    AUDITOR       reads the trail, changes nothing")
    print("    arch@acme-cyber.example.com       SOLUTION_ARCHITECT  in Solution Architecture")
    print("    pmo@acme-cyber.example.com        PROJECT_MANAGER     in PMO")
    print("    finance@acme-cyber.example.com    FINANCE             in Finance")
    print("    legal@acme-cyber.example.com      LEGAL               in Legal")
    print("    product@acme-cyber.example.com    TECHNICAL     head of Product")
    print("    admin@acme-cyber.example.com      ORG_ADMIN     users and roles, NOT bid work\n")


if __name__ == "__main__":
    seed()
    print_accounts()
