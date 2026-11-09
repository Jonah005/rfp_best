"""A requirement with no evidence: the AI suggests, a person decides, and it is all recorded."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.db.session import apply_tenant_context
from app.models import RequirementPosition
from app.services import requirement_positions as positions
from app.services import tasks as task_service
from app.services import writeup
from tests.integration import test_copilot as _copilot_tests
from tests.integration.test_assessment_readiness import _company_version, _requirement
from tests.integration.test_copilot import _scene, user_ctx

fresh_org = _copilot_tests.fresh_org
env = _copilot_tests.env

pytestmark = pytest.mark.integration


class _Model:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls: list[str] = []

    def generate_structured(self, name, user_content, **_):
        from app.schemas.requirement_positions import (
            FormalAnswer,
            PositionOption,
            PositionOptions,
        )

        self.calls.append(name)
        if self.fail:
            raise RuntimeError("model down")
        if name == "requirement_options":
            return PositionOptions(
                summary="No approved evidence was found.",
                options=[
                    PositionOption(
                        label="Commit after award",
                        text="We will provide this after award.",
                        is_commitment=False,  # the code must override this
                        caution="Get it approved.",
                    ),
                    PositionOption(label="Offer an alternative", text="We offer X instead."),
                ],
            )
        return FormalAnswer(text="We will provide this capability following contract award.")


def _setup(s, env, org):
    ctx = ExecutionContext(org_id=org)
    apply_tenant_context(s, ctx)
    dept, user, _o, bid, _t = _scene(s, ctx, env)
    version = _company_version(s, ctx, env)
    r1 = _requirement(s, ctx, bid=bid, doc_version=version, code="R-1", text="Hold ISO 9001")
    r2 = _requirement(s, ctx, bid=bid, doc_version=version, code="R-2", text="Provide a rota")
    task = task_service.create_task(
        s,
        ctx,
        bid_id=bid.id,
        title="Mixed",
        department_id=dept.id,
        owner_user_id=user.id,
        requirement_ids=[r1.id, r2.id],
    )
    return user_ctx(org, user.id), task, r1, r2


def test_it_says_nothing_was_found_and_offers_ways_to_handle_it(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, r2 = _setup(s, env, org)
        out = positions.propose(
            s, me, task_id=task.id, requirement_ids=[r1.id, r2.id], gateway=_Model()
        )
        assert out["found"] == "none"  # decided by the system from the assessments
        assert [o["label"] for o in out["options"]] == [
            "Commit after award",
            "Offer an alternative",
        ]
        # With nothing approved behind them, every option is a promise.
        assert all(o["is_commitment"] for o in out["options"])
        rows = s.execute(select(RequirementPosition)).scalars().all()
        assert {r.status for r in rows} == {"proposed"}
        assert all(r.proposed["options"] for r in rows)

        failed = positions.propose(
            s, me, task_id=task.id, requirement_ids=[r1.id], gateway=_Model(fail=True)
        )
        assert failed["options"] == [] and failed["error"]
        s.commit()


def test_the_chosen_option_is_worded_formally_and_a_person_submits_it(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, r2 = _setup(s, env, org)
        model = _Model()
        positions.propose(s, me, task_id=task.id, requirement_ids=[r1.id], gateway=model)
        formal = positions.formalise(
            s,
            me,
            task_id=task.id,
            requirement_ids=[r1.id],
            option=0,
            own_words=None,
            instruction=None,
            gateway=model,
        )
        assert formal["text"].startswith("We will provide")
        with pytest.raises(ValidationError):
            positions.formalise(
                s,
                me,
                task_id=task.id,
                requirement_ids=[r1.id],
                option=7,
                own_words=None,
                instruction=None,
                gateway=model,
            )
        # Nothing is decided until a person submits it.
        assert writeup._task_requirements(s, me, task.id)[0]["company_position"] is None

        positions.submit(
            s, me, task_id=task.id, requirement_ids=[r1.id], text=formal["text"], source="model"
        )
        item = {i["code"]: i for i in writeup._task_requirements(s, me, task.id)}["R-1"]
        assert item["company_position"] == formal["text"]
        assert item["stance"] == "commit_only"
        shown = positions.decisions(s, me, task_id=task.id)
        assert shown[0]["status"] == "submitted" and shown[0]["ai_suggested"]
        s.commit()


def test_ignored_requirements_leave_the_work_but_are_kept_on_record(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, r2 = _setup(s, env, org)
        positions.propose(s, me, task_id=task.id, requirement_ids=[r2.id], gateway=_Model())
        positions.ignore(
            s, me, task_id=task.id, requirement_ids=[r2.id], reason="Not relevant to our scope"
        )
        item = {i["code"]: i for i in writeup._task_requirements(s, me, task.id)}["R-2"]
        assert item["stance"] == "not_ours" and item["set_aside"] is True
        _, _, rows = writeup._current_rows(s, me, task.id)
        states = {
            c["code"]: c["state"] for c in writeup.coverage_for(s, me, task_id=task.id, rows=rows)
        }
        assert states["R-2"] == "set_aside"

        record = positions.decisions(s, me, task_id=task.id)
        assert (
            record[0]["status"] == "ignored" and record[0]["reason"] == "Not relevant to our scope"
        )
        assert record[0]["ai_suggested"], "what the AI proposed is still on the record"
        assert record[0]["decided_by"]

        positions.restore(s, me, task_id=task.id, requirement_id=r2.id)
        item = {i["code"]: i for i in writeup._task_requirements(s, me, task.id)}["R-2"]
        assert item["set_aside"] is False
        row = s.execute(select(RequirementPosition)).scalars().first()
        assert row.status == "restored" and row.proposed  # the record is not erased
        with pytest.raises(NotFound):
            positions.restore(s, me, task_id=task.id, requirement_id=r2.id)
        s.commit()


def test_a_requirement_from_elsewhere_is_refused(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, _r2 = _setup(s, env, org)
        import uuid

        with pytest.raises(ValidationError):
            positions.ignore(s, me, task_id=task.id, requirement_ids=[uuid.uuid4()], reason=None)
        s.commit()


def test_a_missing_part_chat_sees_its_own_requirements_in_full_and_the_rest_in_brief(
    app_sessionmaker, env
):
    import uuid

    from app.models import ClarificationRequirement
    from app.models.enums import ClarificationIssueType, ClarificationSeverity
    from app.schemas.task_gaps import GapChatTurn
    from app.services import clarifications, copilot, task_gaps

    org, *_ = env
    prompts: list[str] = []

    class _Talker:
        def generate_structured(self, name, user_content, **_):
            prompts.append(user_content)
            return GapChatTurn(reply="ok")

    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, r2 = _setup(s, env, org)
        gap = clarifications.raise_issue(
            s,
            me,
            bid_id=task.bid_id,
            issue_type=list(ClarificationIssueType)[0],
            description="The rota format is not stated.",
            severity=ClarificationSeverity.MAJOR,
            fingerprint=uuid.uuid4().hex,
            detected_by="test",
        )
        s.add(
            ClarificationRequirement(
                org_id=me.org_id, clarification_id=gap.id, requirement_id=r2.id
            )
        )
        s.flush()
        task_gaps.attach(s, me, task_id=task.id, clarification_id=gap.id)
        listed = task_gaps.list_for_task(s, me, task_id=task.id)["items"]
        assert listed[0]["requirement_codes"] == ["R-2"]
        conversation = copilot.create_conversation(s, me, task_id=task.id)
        task_gaps.gap_chat(
            s,
            me,
            task_id=task.id,
            clarification_id=gap.id,
            message="what now?",
            current="",
            conversation_id=conversation.id,
            gateway=_Talker(),
        )
        prompt = prompts[-1]
        assert "Provide a rota" in prompt
        assert "[R-1] (not tied to this part)" in prompt
        s.rollback()


def test_the_ai_links_a_missing_part_to_the_requirement_it_leaves_a_hole_in(app_sessionmaker, env):
    import uuid

    from app.models.enums import ClarificationIssueType, ClarificationSeverity
    from app.schemas.task_gaps import GapJudgement, GapRelevanceResult
    from app.services import clarifications, task_gaps

    class _Linker:
        def generate_structured(self, name, user_content, **_):
            assert name == "task_gap_relevance"
            return GapRelevanceResult(
                items=[
                    GapJudgement(
                        n=1,
                        relevant=True,
                        missing="The rota format is not stated.",
                        why_needed="The rota cannot be written.",
                        requirement_codes=["R-2", "R-99"],  # R-99 is not on this task
                    )
                ]
            )

    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, r2 = _setup(s, env, org)
        clarifications.raise_issue(
            s,
            me,
            bid_id=task.bid_id,
            issue_type=list(ClarificationIssueType)[0],
            description="The rota format is not stated.",
            severity=ClarificationSeverity.MAJOR,
            fingerprint=uuid.uuid4().hex,
            detected_by="test",
        )
        task_gaps.check(s, me, task_id=task.id, gateway=_Linker())
        items = task_gaps.list_for_task(s, me, task_id=task.id)["items"]
        assert items[0]["requirement_codes"] == ["R-2"]
        s.rollback()


def test_the_ai_folds_repeats_and_names_the_piece_but_code_decides_and_a_person_can_undo(
    app_sessionmaker, env
):
    import uuid

    from app.models.enums import ClarificationIssueType, ClarificationSeverity
    from app.schemas.task_gaps import GapJudgement, GapRelevanceResult
    from app.services import clarifications, task_gaps

    class _Judge:
        def generate_structured(self, name, user_content, **_):
            return GapRelevanceResult(
                items=[
                    GapJudgement(
                        n=1,
                        relevant=True,
                        missing="Field list absent.",
                        why_needed="w",
                        requirement_codes=["R-2"],
                        part="rota form",
                    ),
                    # says the same as gap 1: folded
                    GapJudgement(
                        n=2,
                        relevant=True,
                        missing="No list of fields.",
                        why_needed="w",
                        requirement_codes=["R-2"],
                        part="rota form",
                        same_as=1,
                    ),
                    # points at a gap that is itself a repeat: ignored, stays on its own
                    GapJudgement(
                        n=3,
                        relevant=True,
                        missing="Format unclear.",
                        why_needed="w",
                        requirement_codes=["R-2"],
                        part="rota format",
                        same_as=2,
                    ),
                    # points forward / at itself: ignored
                    GapJudgement(n=4, relevant=True, missing="Other.", why_needed="w", same_as=4),
                ]
            )

    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, r2 = _setup(s, env, org)
        ids = []
        for text in ("a", "b", "c", "d"):
            ids.append(
                clarifications.raise_issue(
                    s,
                    me,
                    bid_id=task.bid_id,
                    issue_type=list(ClarificationIssueType)[0],
                    description=f"gap {text}",
                    severity=ClarificationSeverity.MAJOR,
                    fingerprint=uuid.uuid4().hex,
                    detected_by="test",
                ).id
            )
        task_gaps.check(s, me, task_id=task.id, gateway=_Judge())
        items = task_gaps.list_for_task(s, me, task_id=task.id)["items"]
        assert len(items) == 3  # the repeat is folded away
        first = next(i for i in items if i["part"] == "rota form")
        assert [x["missing"] for x in first["similar"]] == ["No list of fields."]

        task_gaps.separate(
            s, me, task_id=task.id, clarification_id=uuid.UUID(first["similar"][0]["id"])
        )
        assert len(task_gaps.list_for_task(s, me, task_id=task.id)["items"]) == 4
        # asking again does not fold it a second time
        task_gaps.check(s, me, task_id=task.id, gateway=_Judge(), force=True)
        assert len(task_gaps.list_for_task(s, me, task_id=task.id)["items"]) == 4
        s.rollback()


def test_a_task_checked_with_an_older_version_of_the_check_is_checked_again_once(
    app_sessionmaker, env
):
    import uuid

    from app.models.enums import ClarificationIssueType, ClarificationSeverity
    from app.schemas.task_gaps import GapJudgement, GapRelevanceResult
    from app.services import clarifications, task_gaps

    calls = []

    class _Judge:
        def generate_structured(self, name, user_content, **_):
            calls.append(1)
            return GapRelevanceResult(
                items=[GapJudgement(n=1, relevant=True, missing="m", why_needed="w", part="p")]
            )

    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, r2 = _setup(s, env, org)
        clarifications.raise_issue(
            s,
            me,
            bid_id=task.bid_id,
            issue_type=list(ClarificationIssueType)[0],
            description="gap",
            severity=ClarificationSeverity.MAJOR,
            fingerprint=uuid.uuid4().hex,
            detected_by="test",
        )
        task_gaps.check(s, me, task_id=task.id, gateway=_Judge())
        assert task_gaps.list_for_task(s, me, task_id=task.id)["checked"] is True
        task_gaps.check(s, me, task_id=task.id, gateway=_Judge())  # nothing new: no second ask
        assert len(calls) == 1

        task.gaps_check_version = "v1"  # an older version of the check
        assert task_gaps.list_for_task(s, me, task_id=task.id)["checked"] is False
        task_gaps.check(s, me, task_id=task.id, gateway=_Judge())
        assert len(calls) == 2
        assert task_gaps.list_for_task(s, me, task_id=task.id)["checked"] is True
        s.rollback()


def test_one_check_covers_every_task_of_the_bid_and_a_failing_task_does_not_stop_it(
    app_sessionmaker, env
):
    import uuid

    from app.models.enums import ClarificationIssueType, ClarificationSeverity
    from app.schemas.task_gaps import GapJudgement, GapRelevanceResult
    from app.services import clarifications, task_gaps

    class _Judge:
        def __init__(self):
            self.asked = 0

        def generate_structured(self, name, user_content, **_):
            self.asked += 1
            return GapRelevanceResult(
                items=[GapJudgement(n=1, relevant=True, missing="m", why_needed="w", part="p")]
            )

    class _Same:
        """The test's own session, standing in for the worker's, so uncommitted rows are seen."""

        def __init__(self, session):
            self._s = session

        def __getattr__(self, name):
            return getattr(self._s, name)

        def begin(self):
            pass

        def commit(self):
            self._s.flush()

        def rollback(self):
            pass

        def close(self):
            pass

    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        me, task, r1, r2 = _setup(s, env, org)
        second = task_service.create_task(
            s,
            me,
            bid_id=task.bid_id,
            title="Second",
            department_id=task.department_id,
            owner_user_id=task.owner_user_id,
            requirement_ids=[r1.id],
        )
        clarifications.raise_issue(
            s,
            me,
            bid_id=task.bid_id,
            issue_type=list(ClarificationIssueType)[0],
            description="gap",
            severity=ClarificationSeverity.MAJOR,
            fingerprint=uuid.uuid4().hex,
            detected_by="test",
        )
        judge = _Judge()
        plan = [(task.id, task.code), (uuid.uuid4(), "GHOST"), (second.id, second.code)]
        task_gaps._bid_runs[task.bid_id] = {
            "org_id": me.org_id,
            "running": True,
            "total": 3,
            "done": 0,
            "failed": 0,
            "current": None,
            "finished_at": None,
        }
        task_gaps.run_bid_check(task.bid_id, me, plan, factory=lambda: _Same(s), gateway=judge)
        status = task_gaps.bid_check_status(task.bid_id, me.org_id)
        assert (status["running"], status["done"], status["failed"]) == (False, 2, 1)
        for t in (task, second):
            assert task_gaps.list_for_task(s, me, task_id=t.id)["checked"] is True
        s.rollback()
