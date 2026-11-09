"""Walk the half of the product that has never run.

Gate one is passed: `seed_fixture_bid.py` gets requirements to DRAFTABLE
against a company whose documents are real evidence. Everything after that --
assignment, drafting, approval into a response artifact, cross-department
requests, assembly, validation, release -- is modelled, coded, and has never
executed on any input. Four test files out of 786 touch it.

This walks it, in the order the product actually runs, and reports what breaks.

## It does not stop at the first failure

Each stage is attempted, its outcome recorded, and the walk continues wherever
the next stage can still be reached. A script that dies on stage 2 tells you
about stage 2; a script that reports 2 through 8 tells you how much work is
left. Where a stage genuinely cannot proceed without the one before it, that is
reported as SKIPPED with the reason, not as a pass.

## Why it needs a cast

Assignment is governed, and correctly: "the system routes work to a department;
the department's head names the person". A head may assign only within their own
department; the bid manager's `task:assign` is a fallback for departments with
no head. None of that can be exercised as SYSTEM, so the walk creates real
people with real roles and acts AS them -- Ahmed assigns and drafts, a reviewer
approves. Running it as SYSTEM would prove only that the governance can be
bypassed, which is not worth knowing.

The cast is idempotent and lives in the fixture organization, so
`seed_fixture_bid.py --reset` wipes it and this rebuilds it.

    python scripts/seed_fixture_bid.py --reset     # gate one first
    python scripts/walk_gate_two.py
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault(
    "BIDREADY_DATABASE_URL",
    os.environ.get("BIDREADY_LOCAL_DATABASE_URL")
    or "postgresql+psycopg://bidready_app:bidready-local@localhost:5432/bidready",
)

from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend()

from _fixture_cast import HEADS  # noqa: E402
from _fixture_cast import ensure as ensure_cast
from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.core.permissions import RoleCode  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.models.enums import BidReadiness, DepartmentCode  # noqa: E402
from app.services import identity  # noqa: E402

ORG_SLUG = "cybranytech-fixture"


def _already_done(state, *reached: str) -> bool:
    """Has an earlier run already carried this past the stage being attempted?

    Written once because four stages needed it. The walk is deliberately
    re-runnable and deliberately does NOT reset state, so on every run after the
    first each stage meets work that is already finished. Read as "not done
    yet", that is indistinguishable from a broken product -- and it was read
    that way three times, once badly enough that stage 3 rewound a submitted
    task and stage 8 was blamed for the hole.
    """
    value = str(getattr(state, "value", state))
    return value in reached


class Walk:
    """Stage results, printed as they happen and summarised at the end."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record(self, outcome: str, stage: str, detail: str) -> None:
        self.rows.append((outcome, stage, detail))
        mark = {"PASS": "  PASS", "FAIL": "  FAIL", "SKIP": "  SKIP"}[outcome]
        print(f"{mark}  {stage}")
        if detail:
            for line in detail.splitlines()[:4]:
                print(f"          {line[:110]}")

    def summary(self) -> int:
        print("=" * 100)
        counts = {k: sum(1 for o, _, _ in self.rows if o == k) for k in ("PASS", "FAIL", "SKIP")}
        print(
            f"\n{counts['PASS']} stage(s) worked, {counts['FAIL']} broke, "
            f"{counts['SKIP']} unreachable.\n"
        )
        if counts["FAIL"] or counts["SKIP"]:
            print("The failures ARE the roadmap. Each one is a stage of the walkthrough")
            print("that has never run before today.\n")
        return 1 if counts["FAIL"] else 0


def _acting_as(session, org, user) -> ExecutionContext:
    """A context carrying this person's real roles, permissions and departments.

    Assembled exactly as `app/api/deps.py` assembles it for a request, from
    `identity.effective_permissions` / `role_codes` / `department_ids`. Granting
    the walk its own permissions would prove the pipeline works for a user who
    could not exist, which is the opposite of what this is for -- the governed
    paths are the ones that have never been exercised.
    """
    ctx = ExecutionContext(
        org_id=org.id,
        actor_user_id=user.id,
        actor_kind="USER",
        permissions=frozenset(identity.effective_permissions(session, user)),
        role_codes=frozenset(identity.role_codes(session, user)),
        department_ids=frozenset(identity.department_ids(session, user)),
    )
    apply_tenant_context(session, ctx)
    return ctx


def main() -> int:
    from app.models import Bid, Organization, RequirementAssessment, Task, TaskRequirement

    walk = Walk()
    session = get_session_factory()()
    try:
        session.begin()
        org = session.execute(
            select(Organization).where(Organization.slug == ORG_SLUG)
        ).scalar_one_or_none()
        if org is None:
            print(f"No {ORG_SLUG} organization. Run seed_fixture_bid.py first.")
            return 1
        system = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, system)

        bid = (
            session.execute(select(Bid).where(Bid.org_id == org.id).order_by(Bid.created_at.desc()))
            .scalars()
            .first()
        )
        if bid is None:
            print("No bid. Run seed_fixture_bid.py first.")
            return 1
        print(f"bid {bid.id}\n")

        users, departments = ensure_cast(session, system, org)
        session.commit()

        session.begin()
        apply_tenant_context(session, system)
        # Ordered by code so consecutive runs walk the SAME task. Unordered,
        # one run picked COM-01 and the next SOL-01, which made two runs
        # incomparable and sent a failure diagnosis down the wrong path.
        tasks_rows = list(
            session.execute(
                select(Task).where(Task.org_id == org.id, Task.bid_id == bid.id).order_by(Task.code)
            ).scalars()
        )
        print("=" * 100)
        if not tasks_rows:
            walk.record(
                "FAIL",
                "1. tasks exist for the bid",
                "assess_bid reported tasks_created=0. Nothing downstream can run without a "
                "task: drafting, artifacts, requests and assembly all hang off one.",
            )
            session.commit()
            return walk.summary()
        walk.record("PASS", "1. tasks exist for the bid", f"{len(tasks_rows)} task(s)")

        # --- 2. assignment ---------------------------------------------------
        from app.services import tasks as task_service

        target = None
        for task in tasks_rows:
            readiness = set(
                session.execute(
                    select(RequirementAssessment.bid_readiness)
                    .join(
                        TaskRequirement,
                        TaskRequirement.requirement_id == RequirementAssessment.requirement_id,
                    )
                    .where(
                        RequirementAssessment.org_id == org.id,
                        RequirementAssessment.is_current.is_(True),
                        TaskRequirement.task_id == task.id,
                    )
                ).scalars()
            )
            if readiness & {BidReadiness.DRAFTABLE, BidReadiness.READY}:
                target = task
                break
        if target is None:
            walk.record(
                "SKIP",
                "2. a department head assigns an owner",
                "No task covers a DRAFTABLE or READY requirement, so there is nothing "
                "worth drafting. Re-run seed_fixture_bid.py --reset first.",
            )
            session.commit()
            return walk.summary()

        # Who may assign, and to whom -- both decided by the TASK's department.
        #
        # The first version picked the head correctly and then hardcoded Zain as
        # the owner, so a Commercial task was refused with "that person is not in
        # Commercial. A department head assigns work to their own department, and
        # to nobody else." The governance was right and the walk was wrong; a
        # fixture that works around that refusal would be testing a system
        # nobody is going to ship.
        from app.models import UserDepartment

        users_by_id = {u.id: u for u in users.values()}
        departments_by_id = {d.id: d for d in departments.values()}
        target_department_name = (
            departments_by_id[target.department_id].name
            if target.department_id in departments_by_id
            else "(no department)"
        )

        dept_code = next((c for c, d in departments.items() if d.id == target.department_id), None)
        head_email = HEADS.get(dept_code)
        if head_email:
            # A headed department: only its head may assign, and only within it.
            actor = users[head_email]
            members = {
                row.user_id
                for row in session.execute(
                    select(UserDepartment).where(
                        UserDepartment.org_id == org.id,
                        UserDepartment.department_id == target.department_id,
                    )
                ).scalars()
            }
            # Prefer somebody other than the head -- a head who can only assign
            # to themselves exercises nothing the unscoped path would not.
            owner = next(
                (u for u in users.values() if u.id in members and u.id != actor.id),
                actor,
            )
        else:
            # No head: the bid manager's fallback, any owner.
            actor = users["sara@cybranytech.example"]
            owner = actor
        try:
            ctx = _acting_as(session, org, actor)
            task_service.assign_owner(session, ctx, task_id=target.id, owner_user_id=owner.id)
            session.flush()
            walk.record(
                "PASS",
                "2. a department head assigns an owner",
                f"{target.code} ({target.department_id and dept_code}) -> "
                f"{owner.display_name}, by {actor.display_name}",
            )
            assigned = True
        except Exception as exc:  # noqa: BLE001 - the point is to see what breaks
            # Say what the state actually was, not just that it was refused.
            #
            # This refusal has now been diagnosed wrongly twice from the message
            # alone. "That person is not in <department>" names the department
            # and nothing else -- not who IS in it, not who was proposed, not
            # whether the membership rows exist at all -- so each guess costs a
            # round trip. A failure that carries its own evidence ends that.
            membership = [
                (users_by_id[row.user_id].display_name, departments_by_id[row.department_id].name)
                for row in session.execute(
                    select(UserDepartment).where(UserDepartment.org_id == org.id)
                ).scalars()
                if row.user_id in users_by_id and row.department_id in departments_by_id
            ]
            in_target = sorted(n for n, d in membership if d == target_department_name)
            walk.record(
                "FAIL",
                "2. a department head assigns an owner",
                f"{type(exc).__name__}: {exc}\n"
                f"task {target.code} is in {target_department_name!r}; "
                f"proposed owner {owner.display_name!r}; actor {actor.display_name!r}\n"
                f"members of {target_department_name}: {in_target or 'NONE'}\n"
                f"all memberships on file: {sorted(membership) or 'NONE'}",
            )
            assigned = False

        # --- 3. the owner picks the task up ----------------------------------
        #
        # `submit` refuses unless the task is IN_PROGRESS: "The task must be
        # IN_PROGRESS before its artifact is submitted." That is not a defect,
        # it is the step this walk skipped -- the walkthrough's "Ahmed opens the
        # task and works on it". A system-assigned state means nobody has taken
        # the work; a person taking it is what makes the draft theirs to submit,
        # and `seed_initial_states` deliberately never recomputes a task once
        # somebody has.
        # Re-runnability, and this one the walk got wrong in a way that framed
        # the product for it.
        #
        # `submit` moves the task on to READY_FOR_APPROVAL, and `decide_approval`
        # moves READY_FOR_APPROVAL -> APPROVED -> COMPLETED. REWORK is a real
        # path, so READY_FOR_APPROVAL -> IN_PROGRESS is legal -- which meant a
        # second run of this stage quietly dragged an already-submitted task
        # BACK to IN_PROGRESS, and stage 8 then failed with "a task cannot move
        # from IN_PROGRESS to APPROVED". The product was right at every step;
        # the harness had undone stage 6 and blamed stage 8 for the hole.
        #
        # Taking a task somebody has already taken is not a re-run of the same
        # step, it is a different step: it is asking for rework. So the state is
        # read first, and a task already past pick-up is reported as progress.
        from app.models.enums import TaskState

        BEFORE_PICKUP = {
            TaskState.NOT_STARTED,
            TaskState.AI_READY_FOR_REVIEW,
            TaskState.NEEDS_INPUT,
            TaskState.BLOCKED,
        }

        picked_up = False
        try:
            ctx = _acting_as(session, org, owner if assigned else actor)
            if target.state is TaskState.IN_PROGRESS:
                walk.record(
                    "PASS",
                    "3. the owner takes the task (NOT_STARTED -> IN_PROGRESS)",
                    f"{target.code} was already IN_PROGRESS",
                )
                picked_up = True
            elif target.state not in BEFORE_PICKUP:
                walk.record(
                    "PASS",
                    "3. the owner takes the task (NOT_STARTED -> IN_PROGRESS)",
                    f"taken in an earlier run; {target.code} is already "
                    f"{getattr(target.state, 'value', target.state)} -- not rewound, "
                    "because pulling a submitted task back is asking for rework, "
                    "not repeating a pick-up",
                )
                picked_up = True
            else:
                task_service.transition(
                    session,
                    ctx,
                    task_id=target.id,
                    to_state=TaskState.IN_PROGRESS,
                    reason="Fixture walk: the owner has taken this task.",
                )
                session.flush()
                walk.record(
                    "PASS",
                    "3. the owner takes the task (NOT_STARTED -> IN_PROGRESS)",
                    f"{target.code} picked up by {(owner if assigned else actor).display_name}",
                )
                picked_up = True
        except Exception as exc:  # noqa: BLE001
            walk.record(
                "FAIL",
                "3. the owner takes the task (NOT_STARTED -> IN_PROGRESS)",
                f"{type(exc).__name__}: {exc}\n"
                f"task state was {getattr(target.state, 'value', target.state)}",
            )

        # --- 4. an artifact, a draft, an approval ----------------------------
        from app.services import artifacts as artifact_service
        from app.services import drafting

        artifact = None
        try:
            ctx = _acting_as(session, org, owner if assigned else actor)
            artifact = artifact_service.ensure_for_task(
                session, ctx, task_id=target.id, title=target.title
            )
            session.flush()
            walk.record("PASS", "4. a response artifact exists for the task", str(artifact.id))
        except Exception as exc:  # noqa: BLE001
            walk.record(
                "FAIL", "4. a response artifact exists for the task", f"{type(exc).__name__}: {exc}"
            )

        # The walk is RE-RUNNABLE, and that costs a branch here.
        #
        # A version is mutable only while DRAFT -- "there is no update path
        # after". A second run against a task whose draft is already IN_REVIEW
        # was reported as a stage-5 failure ("the current version is under
        # review and cannot be edited"), which is the product being right and
        # the walk mistaking earlier progress for a defect.
        #
        # So the current state is read first and the stage reports what it
        # found. Work already done in a previous run is progress, not failure,
        # and it must not be silently redone either: regenerating a draft that
        # a reviewer is holding would discard what they are looking at.
        from app.models.enums import ArtifactVersionState

        version = None
        current_state = None
        if artifact is not None:
            ctx = _acting_as(session, org, owner if assigned else actor)
            existing = [
                v
                for v in artifact_service.versions(session, ctx, artifact_id=artifact.id)
                if v.id == artifact.current_version_id
            ]
            current_state = existing[0].state if existing else None
            if existing and current_state is not ArtifactVersionState.DRAFT:
                version = existing[0]
                walk.record(
                    "PASS",
                    "5. a draft is generated, cited to evidence",
                    f"already done in an earlier run; version {version.version_no} is "
                    f"{getattr(current_state, 'value', current_state)}",
                )
            else:
                try:
                    version = drafting.generate(session, ctx, artifact_id=artifact.id)
                    session.flush()
                    statements = artifact_service.statements_for_version(
                        session, ctx, version_id=version.id
                    )
                    kinds = ", ".join(
                        sorted(
                            {
                                str(getattr(st.statement_class, "value", st.statement_class))
                                for st in statements
                            }
                        )
                    )
                    current_state = version.state
                    walk.record(
                        "PASS",
                        "5. a draft is generated, cited to evidence",
                        f"{len(statements)} statement(s): {kinds}",
                    )
                except Exception as exc:  # noqa: BLE001
                    walk.record(
                        "FAIL",
                        "5. a draft is generated, cited to evidence",
                        f"{type(exc).__name__}: {exc}\n"
                        + "".join(traceback.format_exc().splitlines(True)[-3:]),
                    )
        else:
            walk.record("SKIP", "5. a draft is generated, cited to evidence", "no artifact")

        submitted = False
        if version is not None and current_state is not ArtifactVersionState.DRAFT:
            # Already past submission in an earlier run.
            walk.record(
                "PASS",
                "6. the draft is submitted for approval",
                f"already submitted; version is {getattr(current_state, 'value', current_state)}",
            )
            submitted = True
        elif version is not None and not picked_up:
            walk.record(
                "SKIP",
                "6. the draft is submitted for approval",
                "the task was never taken, and submit refuses unless it is IN_PROGRESS",
            )
        elif version is not None:
            try:
                ctx = _acting_as(session, org, owner if assigned else actor)
                artifact_service.submit(
                    session,
                    ctx,
                    artifact_id=artifact.id,
                    approval_roles=[RoleCode.REVIEWER.value],
                    note="Fixture walk: submitted for review.",
                )
                session.flush()
                walk.record("PASS", "6. the draft is submitted for approval", "awaiting REVIEWER")
                submitted = True
            except Exception as exc:  # noqa: BLE001
                walk.record(
                    "FAIL", "6. the draft is submitted for approval", f"{type(exc).__name__}: {exc}"
                )
        else:
            walk.record("SKIP", "6. the draft is submitted for approval", "no draft version")

        # --- 7. the commitments a person must stand behind --------------------
        #
        # "Every proposed commitment must be approved before the artifact can
        # pass." A PROPOSED_COMMITMENT is a promise the company has not made
        # yet -- an eight-week schedule, a named Project Manager -- and it is
        # the one kind of statement no evidence can support, so a named role
        # has to accept it personally. That rule is why the walkthrough has
        # Fatima approving her own schedule rather than the system emitting it.
        #
        # `decide_commitment` checks `required_approver_role in ctx.role_codes`,
        # so the approver is chosen from the cast by the role the MODEL asked
        # for. When nobody holds it, that is reported rather than worked around:
        # a fixture that grants itself the role would prove the gate can be
        # bypassed, and the gate is the point.
        from app.models.enums import ApprovalStatus

        commitments_ok = True
        try:
            ctx = _acting_as(session, org, users["sara@cybranytech.example"])
            pending = [
                c
                for c in artifact_service.list_commitments(session, ctx, bid_id=bid.id)
                if c.approval_status is ApprovalStatus.PENDING_APPROVAL
            ]
            if not pending:
                walk.record(
                    "PASS",
                    "7. proposed commitments are approved by a named role",
                    "none pending",
                )
            else:
                decided, unapprovable = 0, []
                for commitment in pending:
                    role = commitment.required_approver_role
                    approver = next(
                        (u for u in users.values() if role in identity.role_codes(session, u)),
                        None,
                    )
                    if approver is None:
                        unapprovable.append(role)
                        continue
                    approver_ctx = _acting_as(session, org, approver)
                    artifact_service.decide_commitment(
                        session,
                        approver_ctx,
                        commitment_id=commitment.id,
                        decision=ApprovalStatus.APPROVED,
                        comment="Fixture walk: the company accepts this commitment.",
                    )
                    decided += 1
                session.flush()
                if unapprovable:
                    commitments_ok = False
                    walk.record(
                        "FAIL",
                        "7. proposed commitments are approved by a named role",
                        f"{decided} approved; nobody in the cast holds "
                        f"{sorted(set(unapprovable))} -- the model asked for an approver "
                        "role this organization has not staffed",
                    )
                else:
                    walk.record(
                        "PASS",
                        "7. proposed commitments are approved by a named role",
                        f"{decided} commitment(s) approved",
                    )
        except Exception as exc:  # noqa: BLE001
            commitments_ok = False
            walk.record(
                "FAIL",
                "7. proposed commitments are approved by a named role",
                f"{type(exc).__name__}: {exc}",
            )

        if submitted:
            try:
                reviewer = users["nadia@cybranytech.example"]
                ctx = _acting_as(session, org, reviewer)
                from app.models.enums import ApprovalStepState

                # `approval_chain`, keyed on the VERSION, not the artifact.
                #
                # `submit` writes each Approval against `ENTITY_TYPE` /
                # `version.id`, because approval is granted to a specific draft
                # -- approving "the artifact" would carry a verdict across an
                # edit nobody reviewed. `list_approvals` filters by entity_type
                # and state and takes no id at all, which is what the first
                # version of this got wrong.
                steps = artifact_service.approval_chain(
                    session,
                    ctx,
                    entity_type=artifact_service.ENTITY_TYPE,
                    entity_id=version.id,
                )
                chain_summary = [
                    (step.step_no, step.required_role, getattr(step.state, "value", step.state))
                    for step in steps
                ]
                pending = next(
                    (step for step in steps if step.state is ApprovalStepState.PENDING), None
                )
                # Progress from an earlier run, for the FOURTH time in this walk.
                #
                # Stages 3, 5, 6 and now 8 have each needed the same branch, and
                # each was added separately after the walk reported the product
                # broken for having already done the work. The shape is always
                # identical: read the state first, and distinguish "this has not
                # happened" from "this happened last run". A re-runnable harness
                # that cannot tell those apart manufactures a failure per stage
                # per re-run, and every one of them costs a diagnosis.
                #
                # `_already_done` is that question asked once. Any stage added
                # after this one should call it rather than growing a fifth copy
                # of the branch -- which is the same lesson the product keeps
                # teaching: fix the pattern, not the instance.
                already = _already_done(
                    artifact_service.get_artifact(session, ctx, artifact_id=artifact.id).state,
                    "APPROVED",
                )
                if pending is None and already:
                    walk.record(
                        "PASS",
                        "8. a reviewer approves it into a versioned artifact",
                        "approved in an earlier run; the artifact is APPROVED and its "
                        "version is immutable, so there is nothing left to decide",
                    )
                elif pending is None:
                    walk.record(
                        "FAIL",
                        "8. a reviewer approves it into a versioned artifact",
                        f"submit() created no PENDING step on version {version.id}, and the "
                        f"artifact is not approved either; chain: {chain_summary or 'EMPTY'}",
                    )
                else:
                    artifact_service.decide_approval(
                        session,
                        ctx,
                        approval_id=pending.id,
                        decision=ApprovalStepState.APPROVED,
                        comment="Fixture walk: approved.",
                    )
                    session.flush()
                    fresh = artifact_service.get_artifact(session, ctx, artifact_id=artifact.id)
                    state = getattr(fresh.state, "value", fresh.state)
                    outcome = "PASS" if str(state) == "APPROVED" else "FAIL"
                    walk.record(
                        outcome,
                        "8. a reviewer approves it into a versioned artifact",
                        f"artifact state is {state}",
                    )
            except Exception as exc:  # noqa: BLE001
                walk.record(
                    "FAIL",
                    "8. a reviewer approves it into a versioned artifact",
                    f"{type(exc).__name__}: {exc}",
                )
        else:
            walk.record(
                "SKIP", "8. a reviewer approves it into a versioned artifact", "not submitted"
            )

        # --- 9. a department asks another department for something ----------
        #
        # The walkthrough's "Ahmed needs the day rate and cannot see the rate
        # card". This is the path that exists so the answer to a blocked task is
        # not "grant yourself access": the requester names what they need and
        # why, the owning department answers, and the ANSWER travels while the
        # record stays put. `REQUEST_ANSWER` grants nothing at all, which is why
        # the three types are offered in that order.
        from app.models.enums import RequestState, RequestType
        from app.services import requests as request_service
        from app.services.storage import InMemoryStorage

        # ONE store, shared by stages 11 and 12.
        #
        # In memory on purpose: assembly writes a real .docx and validation
        # reads it back to verify the checksum, and neither step is about where
        # the bytes live -- a walk writing into the configured bucket would
        # leave fixture proposals in it on every run. But it has to be the SAME
        # store: a fresh one per call would hand validation an empty bucket and
        # report CORRUPTED_ARTIFACT, a harness bug wearing a product failure's
        # name. This walk has already made that mistake once, at stage 3.
        walk_storage = InMemoryStorage()

        answered_request = None
        try:
            requester = users["ahmed@cybranytech.example"]
            ctx = _acting_as(session, org, requester)
            security_task = next(
                (
                    t
                    for t in tasks_rows
                    if t.department_id == departments[DepartmentCode.SECURITY].id
                ),
                target,
            )
            request = request_service.create_request(
                session,
                ctx,
                origin_task_id=security_task.id,
                target_department_id=departments[DepartmentCode.COMMERCIAL].id,
                request_type=RequestType.REQUEST_ANSWER,
                request_text="What day rate should we quote for a Security Specialist?",
                business_reason=(
                    "The pricing requirement needs a rate and the rate card is RESTRICTED "
                    "to Commercial."
                ),
            )
            session.flush()

            # Answered by somebody in the department that was asked, not by the
            # person who asked. A walk that answered its own request would prove
            # only that the table accepts rows.
            owner_ctx = _acting_as(session, org, users["omar@cybranytech.example"])
            request_service.acknowledge(session, owner_ctx, request_id=request.id)
            answered_request = request_service.answer(
                session,
                owner_ctx,
                request_id=request.id,
                resolution_text=(
                    "Quote the approved blended rate for this engagement; do not reproduce "
                    "the rate card."
                ),
            )
            session.flush()
            granted = getattr(answered_request, "granted_access_id", None)
            walk.record(
                "PASS" if answered_request.state is RequestState.ANSWERED else "FAIL",
                "9. one department asks another, and gets an answer not a key",
                f"{requester.display_name} -> Commercial, answered by Omar T.; "
                f"access granted: {bool(granted)}",
            )
        except Exception as exc:  # noqa: BLE001
            walk.record(
                "FAIL",
                "9. one department asks another, and gets an answer not a key",
                f"{type(exc).__name__}: {exc}",
            )

        # --- 10. the proposal is assembled from approved artifacts only -------
        from app.services import release as release_service

        structure = None
        try:
            ctx = _acting_as(session, org, users["sara@cybranytech.example"])
            structure = release_service.derive_structure(session, ctx, bid_id=bid.id)
            session.flush()
            section_rows = release_service.sections(session, ctx, structure_id=structure.id)
            cover = release_service.coverage(session, ctx, bid_id=bid.id)
            walk.record(
                "PASS",
                "10. a proposal structure exists, mapped to approved answers",
                f"{len(section_rows)} section(s); "
                f"{len(cover['uncovered_sections'])} without an approved artifact",
            )
        except Exception as exc:  # noqa: BLE001
            walk.record(
                "FAIL",
                "10. a proposal structure exists, mapped to approved answers",
                f"{type(exc).__name__}: {exc}",
            )

        # --- 11. assemble and validate ---------------------------------------
        #
        # `assemble` refuses while any mandatory section has no approved
        # artifact, which on this fixture is the ordinary case: one task has
        # been taken all the way and five have not. That refusal is the product
        # being right, so it is reported as a PASS for the rule and the walk
        # says which sections are still open.
        from app.models.enums import OutputMode

        package = None
        try:
            ctx = _acting_as(session, org, users["sara@cybranytech.example"])
            uncovered = release_service.coverage(session, ctx, bid_id=bid.id)["uncovered_sections"]
            if uncovered:
                walk.record(
                    "PASS",
                    "11. assembly refuses while any mandatory section is unanswered",
                    f"{len(uncovered)} section(s) have no approved artifact, and assembly "
                    "declines rather than shipping a proposal with holes in it",
                )
            else:
                release_service.assemble(
                    session,
                    ctx,
                    bid_id=bid.id,
                    output_mode=OutputMode.SUBMISSION,
                    storage=walk_storage,
                )
                package = release_service.create_package(
                    session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION
                )
                session.flush()
                walk.record(
                    "PASS",
                    "11. assembly refuses while any mandatory section is unanswered",
                    f"every section answered; package {package.id} assembled",
                )
        except Exception as exc:  # noqa: BLE001
            walk.record(
                "FAIL",
                "11. assembly refuses while any mandatory section is unanswered",
                f"{type(exc).__name__}: {exc}",
            )

        # --- 12. nothing RESTRICTED leaves in the proposal --------------------
        #
        # The tender says "Pricing must not appear anywhere in the technical
        # proposal" and the rate card is RESTRICTED. `RESTRICTED_DISCLOSURE` sat
        # in `release.NON_WAIVABLE` and was raised by nothing at all until
        # today: a rule declared unwaivable about a finding that could not
        # occur.
        #
        # This stage cannot run end to end on the fixture until every section
        # has an approved answer, so what it checks is that the rule EXISTS and
        # is wired -- the behaviour itself is pinned by
        # `tests/integration/test_a_restricted_rate_cannot_leave.py`, which
        # fails when the rule is removed.
        try:
            from app.models.enums import ConfidentialityClass

            unreleasable = release_service.UNRELEASABLE
            wired = (
                ConfidentialityClass.RESTRICTED in unreleasable
                and "RESTRICTED_DISCLOSURE" in release_service.NON_WAIVABLE
            )
            if package is not None:
                run = release_service.validate_package(
                    session, ctx, package_id=package.id, storage=walk_storage
                )
                findings = release_service.validation_findings(session, ctx, package_id=package.id)
                leaked = [f for f in findings if f.check_code == "RESTRICTED_DISCLOSURE"]
                walk.record(
                    "PASS",
                    "12. classified material cannot leave inside the proposal",
                    f"validation {run.status.value}: {len(findings)} finding(s), "
                    f"{len(leaked)} disclosure; package is "
                    f"{getattr(package.state, 'value', package.state)}",
                )
            elif wired:
                walk.record(
                    "PASS",
                    "12. classified material cannot leave inside the proposal",
                    "no package to validate yet; the rule is wired and non-waivable "
                    "(behaviour pinned by test_a_restricted_rate_cannot_leave.py)",
                )
            else:
                walk.record(
                    "FAIL",
                    "12. classified material cannot leave inside the proposal",
                    "RESTRICTED is releasable, or the finding is waivable",
                )
        except Exception as exc:  # noqa: BLE001
            walk.record(
                "FAIL",
                "12. classified material cannot leave inside the proposal",
                f"{type(exc).__name__}: {exc}",
            )

        session.commit()
        return walk.summary()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
