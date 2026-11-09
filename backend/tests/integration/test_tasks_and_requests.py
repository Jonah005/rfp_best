"""Phase I and K: task isolation, lifecycle, and the department boundary.

The cases that matter here are the ones where the product declines to do the
convenient thing: a task that stays visibly blocked instead of quietly
resuming, a request that yields an answer without yielding access, and a
grant that dies with its task.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import IllegalTransition, NotFound, ValidationError
from app.db.session import apply_tenant_context
from app.models import (
    AccessGrant,
    AuditEvent,
    Department,
    Task,
    TaskConversation,
    User,
    UserDepartment,
)
from app.models.enums import (
    ConfidentialityClass,
    DepartmentCode,
    GrantResourceType,
    ParticipantRole,
    RequestState,
    RequestType,
    TaskDependencyType,
    TaskState,
)
from app.services import requests as request_service
from app.services import tasks as task_service
from tests.integration.test_assessment_readiness import (
    ISO_TEXT,
    TODAY,
    _company_version,
    _evidence,
    _new_bid,
    _requirement,
)

pytestmark = pytest.mark.integration

from app.gateway.embeddings import HashingEmbeddingProvider  # noqa: E402
from app.ingestion.safety import EicarAwareScanner  # noqa: E402
from app.models.enums import RequirementCategory  # noqa: E402
from app.services import assessment as assessment_service  # noqa: E402
from app.services.storage import InMemoryStorage  # noqa: E402


@pytest.fixture
def fresh_org(owner_engine):
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization

    org_id = uuid.uuid4()
    Owner = sessionmaker(bind=owner_engine, future=True)
    with Owner() as s:
        s.add(
            Organization(id=org_id, name=f"Tasks {org_id.hex[:6]}", slug=f"tasks-{org_id.hex[:10]}")
        )
        s.commit()
    return org_id


@pytest.fixture
def env(fresh_org):
    return fresh_org, InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider()


def _departments(session, org_id, codes):
    made = {}
    for code in codes:
        dept = Department(org_id=org_id, code=code, name=code.value.title())
        session.add(dept)
        made[code] = dept
    session.flush()
    return made


def _user(session, org_id, label):
    user = User(
        org_id=org_id,
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        display_name=label.title(),
    )
    session.add(user)
    session.flush()
    return user


# --- task creation and scope -------------------------------------------------


def test_a_task_carries_its_own_retrieval_ceiling(app_sessionmaker, env):
    """Two contributors on the same bid do not share a context.

    The scope is stored on the task, not derived per request from whoever is
    asking, so isolation does not depend on every call site remembering to
    filter.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts = _departments(s, org, [DepartmentCode.SECURITY, DepartmentCode.HR])
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        security_req = _requirement(
            s, ctx, bid=bid, doc_version=version, code="SEC-1", text=ISO_TEXT
        )
        hr_req = _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="HR-1",
            text="Two OSCP-certified testers are named in the delivery team.",
            category=RequirementCategory.STAFFING,
        )

        security_task = task_service.create_task(
            s,
            ctx,
            bid_id=bid.id,
            title="Security requirements",
            department_id=depts[DepartmentCode.SECURITY].id,
            requirement_ids=[security_req.id],
        )
        hr_task = task_service.create_task(
            s,
            ctx,
            bid_id=bid.id,
            title="Staffing requirements",
            department_id=depts[DepartmentCode.HR].id,
            requirement_ids=[hr_req.id],
        )

        assert security_task.retrieval_scope["departments"] == ["SECURITY"]
        assert hr_task.retrieval_scope["departments"] == ["HR"]
        assert security_task.retrieval_scope != hr_task.retrieval_scope
        # HR material is more sensitive by default, and the ceiling says so.
        assert (
            hr_task.retrieval_scope["max_confidentiality"] == ConfidentialityClass.RESTRICTED.value
        )
        assert (
            security_task.retrieval_scope["max_confidentiality"]
            == ConfidentialityClass.CONFIDENTIAL.value
        )
        s.commit()


def test_generated_tasks_explain_why_their_requirements_are_together(app_sessionmaker, env):
    """A grouping nobody can explain is a grouping nobody can correct."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _departments(s, org, [DepartmentCode.SECURITY, DepartmentCode.HR])
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        _requirement(s, ctx, bid=bid, doc_version=version, code="SEC-1", text=ISO_TEXT)
        _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="HR-1",
            text="Two OSCP-certified testers are named in the delivery team.",
            category=RequirementCategory.STAFFING,
        )

        created = task_service.generate_tasks(s, ctx, bid_id=bid.id)
        assert len(created) == 2
        assert all(t.grouping_rationale for t in created)

        # Re-running must not duplicate work that is already grouped.
        assert task_service.generate_tasks(s, ctx, bid_id=bid.id) == []
        s.commit()


def test_a_requirement_that_does_not_apply_is_never_given_to_anyone(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _departments(s, org, [DepartmentCode.SECURITY])
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        from app.models.enums import RequirementApplicability

        _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="SEC-9",
            text=ISO_TEXT,
            applicability=RequirementApplicability.NOT_APPLICABLE,
        )
        assert task_service.generate_tasks(s, ctx, bid_id=bid.id) == []
        s.rollback()


def test_initial_state_follows_the_evidence_and_never_claims_completion(app_sessionmaker, env):
    """`AI_READY_FOR_REVIEW` means a human has not looked yet."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        _departments(s, org, [DepartmentCode.SECURITY, DepartmentCode.BID_MANAGEMENT])
        bid = _new_bid(s, ctx)
        version = _company_version(s, ctx, env)
        _evidence(s, ctx, doc_version=version, excerpt=ISO_TEXT, expiry=TODAY + timedelta(365))
        _requirement(s, ctx, bid=bid, doc_version=version, code="SEC-1", text=ISO_TEXT)
        _requirement(
            s,
            ctx,
            bid=bid,
            doc_version=version,
            code="ELG-1",
            text="Audited annual accounts covering three fiscal years accompany every offer.",
            category=RequirementCategory.ELIGIBILITY,
        )

        assessment_service.assess_bid(s, ctx, bid_id=bid.id, today=TODAY)
        task_service.generate_tasks(s, ctx, bid_id=bid.id)
        moved = task_service.seed_initial_states(s, ctx, bid_id=bid.id)

        assert moved.get(TaskState.AI_READY_FOR_REVIEW.value) == 1
        assert moved.get(TaskState.BLOCKED.value) == 1
        assert TaskState.COMPLETED.value not in moved
        s.commit()


# --- lifecycle ---------------------------------------------------------------


def _simple_task(s, ctx, env, *, title="Task"):
    depts = _departments(s, ctx.org_id, [DepartmentCode.SECURITY])
    bid = _new_bid(s, ctx)
    return task_service.create_task(
        s,
        ctx,
        bid_id=bid.id,
        title=title,
        department_id=depts[DepartmentCode.SECURITY].id,
    )


def test_an_illegal_transition_is_refused_and_says_what_is_allowed(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        task = _simple_task(s, ctx, env)

        with pytest.raises(IllegalTransition, match="AI_READY_FOR_REVIEW"):
            task_service.transition(s, ctx, task_id=task.id, to_state=TaskState.COMPLETED)
        s.rollback()


def test_blocking_a_task_requires_a_reason_and_keeps_it(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        task = _simple_task(s, ctx, env)

        with pytest.raises(ValidationError, match="reason"):
            task_service.transition(s, ctx, task_id=task.id, to_state=TaskState.BLOCKED)

        task_service.transition(
            s,
            ctx,
            task_id=task.id,
            to_state=TaskState.BLOCKED,
            reason="No evidence of the certification.",
        )
        assert task.blocker_note == "No evidence of the certification."

        # Clearing the block clears the note; a stale blocker reads as a live one.
        task_service.transition(s, ctx, task_id=task.id, to_state=TaskState.IN_PROGRESS)
        assert task.blocker_note is None
        s.commit()


def test_every_state_change_is_audited(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        task = _simple_task(s, ctx, env)
        task_service.transition(s, ctx, task_id=task.id, to_state=TaskState.AI_READY_FOR_REVIEW)
        events = (
            s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org, AuditEvent.action == "task.state_changed"
                )
            )
            .scalars()
            .all()
        )
        assert events[-1].before_state["state"] == TaskState.NOT_STARTED.value
        assert events[-1].after_state["state"] == TaskState.AI_READY_FOR_REVIEW.value
        s.commit()


# --- participants and conversations -----------------------------------------


def test_joining_a_task_creates_exactly_one_private_conversation(app_sessionmaker, env):
    """Per (task, user). A shared thread would leak one person's exploration
    into another's context."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        task = _simple_task(s, ctx, env)
        alice = _user(s, org, "alice")
        bob = _user(s, org, "bob")
        actor_ctx = ExecutionContext(org_id=org, actor_user_id=alice.id)

        task_service.add_participant(
            s,
            actor_ctx,
            task_id=task.id,
            user_id=alice.id,
            participant_role=ParticipantRole.CONTRIBUTOR,
        )
        task_service.add_participant(
            s,
            actor_ctx,
            task_id=task.id,
            user_id=bob.id,
            participant_role=ParticipantRole.REVIEWER,
        )

        conversations = (
            s.execute(
                select(TaskConversation).where(
                    TaskConversation.org_id == org, TaskConversation.task_id == task.id
                )
            )
            .scalars()
            .all()
        )
        assert {c.user_id for c in conversations} == {alice.id, bob.id}

        # Adding the same person again must not create a second conversation.
        task_service.add_participant(
            s,
            actor_ctx,
            task_id=task.id,
            user_id=alice.id,
            participant_role=ParticipantRole.CONTRIBUTOR,
        )
        assert (
            len(
                s.execute(
                    select(TaskConversation).where(
                        TaskConversation.org_id == org,
                        TaskConversation.task_id == task.id,
                    )
                )
                .scalars()
                .all()
            )
            == 2
        )
        s.commit()


def test_removing_a_participant_records_it_rather_than_deleting_the_row(app_sessionmaker, env):
    """Who could see this task last month is a question an auditor may ask."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        task = _simple_task(s, ctx, env)
        user = _user(s, org, "carol")
        actor_ctx = ExecutionContext(org_id=org, actor_user_id=user.id)
        task_service.add_participant(
            s,
            actor_ctx,
            task_id=task.id,
            user_id=user.id,
            participant_role=ParticipantRole.CONTRIBUTOR,
        )
        task_service.remove_participant(
            s,
            actor_ctx,
            task_id=task.id,
            user_id=user.id,
            participant_role=ParticipantRole.CONTRIBUTOR,
        )

        assert task_service.participants(s, actor_ctx, task_id=task.id) == []
        history = task_service.participants(s, actor_ctx, task_id=task.id, include_removed=True)
        assert len(history) == 1
        assert history[0].removed_at is not None
        assert not task_service.is_on_task(s, actor_ctx, task_id=task.id, user_id=user.id)
        s.commit()


# --- dependencies ------------------------------------------------------------


def test_a_dependency_cycle_is_refused(app_sessionmaker, env):
    """Two contributors each waiting on the other is a bid that stops moving
    and nobody notices until the deadline."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts = _departments(s, org, [DepartmentCode.SECURITY])
        bid = _new_bid(s, ctx)
        a, b, c = (
            task_service.create_task(
                s,
                ctx,
                bid_id=bid.id,
                title=name,
                department_id=depts[DepartmentCode.SECURITY].id,
            )
            for name in ("A", "B", "C")
        )
        task_service.add_dependency(
            s,
            ctx,
            from_task_id=a.id,
            to_task_id=b.id,
            dependency_type=TaskDependencyType.WAITS_FOR,
        )
        task_service.add_dependency(
            s,
            ctx,
            from_task_id=b.id,
            to_task_id=c.id,
            dependency_type=TaskDependencyType.WAITS_FOR,
        )

        with pytest.raises(ValidationError, match="cycle"):
            task_service.add_dependency(
                s,
                ctx,
                from_task_id=c.id,
                to_task_id=a.id,
                dependency_type=TaskDependencyType.WAITS_FOR,
            )
        with pytest.raises(ValidationError, match="itself"):
            task_service.add_dependency(
                s,
                ctx,
                from_task_id=a.id,
                to_task_id=a.id,
                dependency_type=TaskDependencyType.WAITS_FOR,
            )
        s.commit()


def test_dependencies_are_visible_from_both_ends(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts = _departments(s, org, [DepartmentCode.SECURITY])
        bid = _new_bid(s, ctx)
        a = task_service.create_task(
            s,
            ctx,
            bid_id=bid.id,
            title="A",
            department_id=depts[DepartmentCode.SECURITY].id,
        )
        b = task_service.create_task(
            s,
            ctx,
            bid_id=bid.id,
            title="B",
            department_id=depts[DepartmentCode.SECURITY].id,
        )
        task_service.add_dependency(
            s,
            ctx,
            from_task_id=a.id,
            to_task_id=b.id,
            dependency_type=TaskDependencyType.WAITS_FOR,
        )
        assert len(task_service.dependencies(s, ctx, task_id=a.id)["outgoing"]) == 1
        assert len(task_service.dependencies(s, ctx, task_id=b.id)["incoming"]) == 1
        s.commit()


# --- cross-department requests ----------------------------------------------


def _staff(session, org_id, department, label="member"):
    """Someone in the department, so a request to it has somewhere to go."""
    user = _user(session, org_id, label)
    session.add(UserDepartment(org_id=org_id, user_id=user.id, department_id=department.id))
    session.flush()
    return user


def _request_scenario(s, ctx, env):
    org = ctx.org_id
    depts = _departments(s, org, [DepartmentCode.SECURITY, DepartmentCode.HR])
    _staff(s, org, depts[DepartmentCode.HR], "hr-member")
    bid = _new_bid(s, ctx)
    version = _company_version(s, ctx, env)
    requester = _user(s, org, "security-eng")
    task = task_service.create_task(
        s,
        ctx,
        bid_id=bid.id,
        title="Staffing answer",
        department_id=depts[DepartmentCode.SECURITY].id,
        owner_user_id=requester.id,
    )
    task_service.transition(s, ctx, task_id=task.id, to_state=TaskState.IN_PROGRESS)
    return depts, bid, task, requester, version


def test_an_answer_resolves_a_request_without_granting_any_access(app_sessionmaker, env):
    """The whole point of the least-privilege ordering.

    Security learns the engineer is available. Security does not gain the
    ability to read HR records.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, task, requester, _ = _request_scenario(s, ctx, env)
        requester_ctx = ExecutionContext(org_id=org, actor_user_id=requester.id)

        request = request_service.create_request(
            s,
            requester_ctx,
            origin_task_id=task.id,
            target_department_id=depts[DepartmentCode.HR].id,
            request_type=RequestType.REQUEST_ANSWER,
            request_text="Is Zain Malik available for eight weeks from 15 October?",
            business_reason="R-042 requires two OSCP testers before this can be drafted.",
        )
        assert request.state is RequestState.PENDING
        # The task is visibly waiting, not quietly in progress.
        assert task.state is TaskState.WAITING_FOR_DEPENDENCY

        request_service.answer(
            s, ctx, request_id=request.id, resolution_text="Yes, available from 8 October."
        )
        assert request.state is RequestState.ANSWERED
        assert task.state is TaskState.IN_PROGRESS

        grants = (
            s.execute(
                select(AccessGrant).where(
                    AccessGrant.org_id == org, AccessGrant.origin_request_id == request.id
                )
            )
            .scalars()
            .all()
        )
        assert grants == [], "answering must not create any access grant"
        s.commit()


def test_sharing_evidence_creates_narrow_task_scoped_expiring_grants(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, task, requester, version = _request_scenario(s, ctx, env)
        requester_ctx = ExecutionContext(org_id=org, actor_user_id=requester.id)
        granter = _user(s, org, "hr-lead")
        granter_ctx = ExecutionContext(org_id=org, actor_user_id=granter.id)

        evidence_a = _evidence(s, ctx, doc_version=version, excerpt="CV: Zain Malik, OSCP.")
        evidence_b = _evidence(s, ctx, doc_version=version, excerpt="CV: Lara Haddad, OSCP.")

        request = request_service.create_request(
            s,
            requester_ctx,
            origin_task_id=task.id,
            target_department_id=depts[DepartmentCode.HR].id,
            request_type=RequestType.REQUEST_EVIDENCE,
            request_text="Please share the OSCP certifications for the named testers.",
            business_reason="The technical response must cite the certifications.",
        )
        _, grants = request_service.share_evidence(
            s,
            granter_ctx,
            request_id=request.id,
            evidence_ids=[evidence_a.id, evidence_b.id],
            reason="Two CVs shared for the staffing answer.",
        )

        assert len(grants) == 2
        assert all(g.task_id == task.id for g in grants), "grants must die with the task"
        assert all(g.grantee_user_id == requester.id for g in grants)
        assert all(g.expires_at > datetime.now(timezone.utc) for g in grants)
        assert {g.resource_id for g in grants} == {evidence_a.id, evidence_b.id}
        assert all(g.resource_type is GrantResourceType.EVIDENCE for g in grants)
        assert request.state is RequestState.EVIDENCE_SHARED
        s.commit()


def test_one_reply_carries_the_answer_and_the_evidence_behind_it(app_sessionmaker, env):
    """The only kind of request the screen offers now: ask for an answer. The
    colleague replies in words and attaches what proves it, in one step."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, task, requester, version = _request_scenario(s, ctx, env)
        requester_ctx = ExecutionContext(org_id=org, actor_user_id=requester.id)
        granter_ctx = ExecutionContext(org_id=org, actor_user_id=_user(s, org, "legal").id)
        evidence = _evidence(s, ctx, doc_version=version, excerpt="Licence: vendor and client.")

        request = request_service.create_request(
            s,
            requester_ctx,
            origin_task_id=task.id,
            target_department_id=depts[DepartmentCode.HR].id,
            request_type=RequestType.REQUEST_ANSWER,
            request_text="What are the licence terms?",
            business_reason="R-061 needs them.",
        )
        request_service.answer(
            s,
            granter_ctx,
            request_id=request.id,
            resolution_text="Both parties may use it; see the licence.",
            evidence_ids=[evidence.id],
        )
        assert request.state is RequestState.ANSWERED
        assert request.shared_evidence_ids == [evidence.id]
        grants = (
            s.execute(select(AccessGrant).where(AccessGrant.origin_request_id == request.id))
            .scalars()
            .all()
        )
        assert [g.resource_id for g in grants] == [evidence.id]
        assert grants[0].task_id == task.id, "the grant dies with the asking task"
        s.commit()


def test_a_rejected_request_leaves_the_task_blocked_not_quietly_in_progress(app_sessionmaker, env):
    """The contributor still has a gap. Pretending otherwise is how an
    unsubstantiated claim reaches a customer."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, task, requester, _ = _request_scenario(s, ctx, env)
        requester_ctx = ExecutionContext(org_id=org, actor_user_id=requester.id)

        request = request_service.create_request(
            s,
            requester_ctx,
            origin_task_id=task.id,
            target_department_id=depts[DepartmentCode.HR].id,
            request_type=RequestType.REQUEST_ACCESS,
            request_text="Read access to the HR staffing roster.",
            business_reason="Needed to confirm availability across the engagement.",
            requested_resource_id=uuid.uuid4(),
        )
        assert task.state is TaskState.WAITING_FOR_ACCESS

        with pytest.raises(ValidationError, match="reason"):
            request_service.reject(s, ctx, request_id=request.id, reason="")

        request_service.reject(
            s,
            ctx,
            request_id=request.id,
            reason="The roster contains personal data outside the bid's scope.",
        )
        assert request.state is RequestState.REJECTED
        assert task.state is TaskState.BLOCKED
        assert "rejected" in (task.blocker_note or "")
        s.commit()


def test_a_task_waiting_on_two_requests_is_not_released_by_one(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, task, requester, _ = _request_scenario(s, ctx, env)
        requester_ctx = ExecutionContext(org_id=org, actor_user_id=requester.id)

        first = request_service.create_request(
            s,
            requester_ctx,
            origin_task_id=task.id,
            target_department_id=depts[DepartmentCode.HR].id,
            request_type=RequestType.REQUEST_ANSWER,
            request_text="Availability of the first tester?",
            business_reason="Needed before drafting.",
        )
        request_service.create_request(
            s,
            requester_ctx,
            origin_task_id=task.id,
            target_department_id=depts[DepartmentCode.HR].id,
            request_type=RequestType.REQUEST_ANSWER,
            request_text="Availability of the second tester?",
            business_reason="Needed before drafting.",
        )

        request_service.answer(s, ctx, request_id=first.id, resolution_text="Available.")
        assert task.state is TaskState.WAITING_FOR_DEPENDENCY
        s.commit()


def test_a_request_needs_a_business_reason(app_sessionmaker, env):
    """Not paperwork: it is what lets the other side decide how much to give."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, task, requester, _ = _request_scenario(s, ctx, env)
        requester_ctx = ExecutionContext(org_id=org, actor_user_id=requester.id)

        with pytest.raises(ValidationError, match="business reason"):
            request_service.create_request(
                s,
                requester_ctx,
                origin_task_id=task.id,
                target_department_id=depts[DepartmentCode.HR].id,
                request_type=RequestType.REQUEST_ANSWER,
                request_text="Availability?",
                business_reason="   ",
            )

        with pytest.raises(ValidationError, match="already owns"):
            request_service.create_request(
                s,
                requester_ctx,
                origin_task_id=task.id,
                target_department_id=depts[DepartmentCode.SECURITY].id,
                request_type=RequestType.REQUEST_ANSWER,
                request_text="Anything?",
                business_reason="A reason.",
            )
        s.rollback()


def test_an_expired_request_does_not_release_the_task(app_sessionmaker, env):
    """Silence resolved nothing."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, task, requester, _ = _request_scenario(s, ctx, env)
        requester_ctx = ExecutionContext(org_id=org, actor_user_id=requester.id)

        request_service.create_request(
            s,
            requester_ctx,
            origin_task_id=task.id,
            target_department_id=depts[DepartmentCode.HR].id,
            request_type=RequestType.REQUEST_ANSWER,
            request_text="Availability?",
            business_reason="Needed before drafting.",
            due_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert request_service.expire_overdue(s, ctx) == 1
        assert task.state is TaskState.WAITING_FOR_DEPENDENCY
        s.commit()


def test_the_resolution_summary_names_every_grant_it_created(app_sessionmaker, env):
    """'Resolved' says nothing about whether the boundary held. The grants do."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, task, requester, version = _request_scenario(s, ctx, env)
        requester_ctx = ExecutionContext(org_id=org, actor_user_id=requester.id)
        granter_ctx = ExecutionContext(org_id=org, actor_user_id=_user(s, org, "hr").id)
        evidence = _evidence(s, ctx, doc_version=version, excerpt="CV: Zain Malik, OSCP.")

        request = request_service.create_request(
            s,
            requester_ctx,
            origin_task_id=task.id,
            target_department_id=depts[DepartmentCode.HR].id,
            request_type=RequestType.REQUEST_EVIDENCE,
            request_text="Share the OSCP certificate.",
            business_reason="To cite it in the technical response.",
        )
        request_service.share_evidence(
            s,
            granter_ctx,
            request_id=request.id,
            evidence_ids=[evidence.id],
            reason="One CV shared.",
        )

        summary = request_service.resolution_summary(s, ctx, request_id=request.id)
        assert summary["state"] == RequestState.EVIDENCE_SHARED.value
        assert len(summary["grants_created"]) == 1
        assert summary["grants_created"][0]["task_id"] == str(task.id)
        assert summary["grants_created"][0]["expires_at"]
        s.commit()


def test_a_resolved_request_cannot_be_resolved_again(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, task, requester, _ = _request_scenario(s, ctx, env)
        requester_ctx = ExecutionContext(org_id=org, actor_user_id=requester.id)
        request = request_service.create_request(
            s,
            requester_ctx,
            origin_task_id=task.id,
            target_department_id=depts[DepartmentCode.HR].id,
            request_type=RequestType.REQUEST_ANSWER,
            request_text="Availability?",
            business_reason="Needed before drafting.",
        )
        request_service.answer(s, ctx, request_id=request.id, resolution_text="Yes.")
        with pytest.raises(IllegalTransition):
            request_service.answer(s, ctx, request_id=request.id, resolution_text="Actually, no.")
        s.rollback()


def test_tasks_and_requests_do_not_cross_tenants(app_sessionmaker, seeded_orgs, env):
    org_a = env[0]
    org_b = seeded_orgs[1]
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        task = _simple_task(s, ctx, env)
        task_id = task.id
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        other = ExecutionContext(org_id=org_b)
        apply_tenant_context(s, other)
        assert s.execute(select(Task).where(Task.id == task_id)).scalars().all() == []
        with pytest.raises(NotFound):
            task_service.get_task(s, other, task_id=task_id)
        s.rollback()


def test_a_request_to_a_department_nobody_is_in_is_refused(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, task, requester, _ = _request_scenario(s, ctx, env)
        empty = _departments(s, org, [DepartmentCode.LEGAL])[DepartmentCode.LEGAL]
        requester_ctx = ExecutionContext(org_id=org, actor_user_id=requester.id)
        with pytest.raises(ValidationError, match="Nobody is in"):
            request_service.create_request(
                s,
                requester_ctx,
                origin_task_id=task.id,
                target_department_id=empty.id,
                request_type=RequestType.REQUEST_ANSWER,
                request_text="Is this clause acceptable?",
                business_reason="Needed for the response.",
            )
        # Nothing was saved and the task was not left waiting.
        assert task.state is TaskState.IN_PROGRESS


def test_asking_a_department_marks_the_draft_part_until_a_person_fixes_it(app_sessionmaker, env):
    from app.models import DraftHold
    from app.services import draft_holds

    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        depts, bid, _task, requester, version = _request_scenario(s, ctx, env)
        req = _requirement(
            s, ctx, bid=bid, doc_version=version, code="R-77", text="Staff two testers"
        )
        task = task_service.create_task(
            s,
            ctx,
            bid_id=bid.id,
            title="Staffing",
            department_id=depts[DepartmentCode.SECURITY].id,
            owner_user_id=requester.id,
            requirement_ids=[req.id],
        )
        me = ExecutionContext(org_id=org, actor_user_id=requester.id)
        request = request_service.create_request(
            s,
            me,
            origin_task_id=task.id,
            target_department_id=depts[DepartmentCode.HR].id,
            request_type=RequestType.REQUEST_ANSWER,
            request_text="Who is available?",
            business_reason="R-77 needs two testers.",
            requirement_ids=[req.id],
        )
        marks = draft_holds.for_task(s, me, task_id=task.id)
        assert [m["requirement_code"] for m in marks] == ["R-77"]
        assert marks[0]["reply_arrived"] is False
        assert marks[0]["placeholder"].startswith("[Waiting for")

        request_service.answer(s, ctx, request_id=request.id, resolution_text="Zain and Lee.")
        marks = draft_holds.for_task(s, me, task_id=task.id)
        assert marks[0]["reply_arrived"] is True and marks[0]["reply"] == "Zain and Lee."

        draft_holds.fix(s, me, task_id=task.id, hold_id=uuid.UUID(marks[0]["id"]))
        assert draft_holds.for_task(s, me, task_id=task.id) == []
        assert s.execute(select(DraftHold)).scalars().first().state == "fixed"
        s.rollback()
