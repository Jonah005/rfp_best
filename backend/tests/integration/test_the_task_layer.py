"""The middle of the walkthrough: work packages, a draft waiting, a person's dashboard.

Sarah's bid is assessed; BidReady groups the requirements into packages a
person can own ("Accreditations and certifications", not "Security
requirements"); the package whose evidence is strong opens with a draft already
in it; and the owner's dashboard counts it under "ready for approval".

Built on the uploaded-document scene of
`test_a_real_document_reaches_draftable.py`, with the model scripted to answer
the two new calls -- grouping and drafting -- from what it is shown.
"""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.models import (
    ArtifactStatement,
    Department,
    ResponseArtifact,
    Task,
    TaskRequirement,
)
from app.models.enums import DepartmentCode, TaskState
from app.services import assessment, drafting, knowledge, tasks
from app.services.storage import InMemoryStorage
from app.services.tasks import bucket_for, packages_from
from tests.integration.test_a_real_document_reaches_draftable import (
    Reader,
    _tender_requirements,
    _upload_company_pack,
    org,  # noqa: F401 - pytest fixture
)

# ruff: noqa: F811 - `org` is a fixture, imported and then requested by name

pytestmark = pytest.mark.integration


class WorkflowReader(Reader):
    """The document reader, plus the grouping and drafting calls."""

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        blob = "\n".join(m.content for m in messages)
        if '"title": "TaskGroupingResult"' in blob:
            self.seen.append("task_grouping")
            codes = re.findall(r'"code":\s*"(R-[^"]+)"', blob)
            accreditation = [c for c in codes if c == "R-2.1"]
            rest = [c for c in codes if c != "R-2.1"]
            return self._reply(
                {
                    "packages": [
                        {
                            "title": "Accreditations and certifications",
                            "requirement_codes": accreditation,
                            "rationale": "The accreditation the tender demands.",
                        },
                        {
                            "title": "Certificates and methodology",
                            "requirement_codes": rest,
                            "rationale": "Remaining security answers.",
                        },
                    ]
                }
            )
        if '"title": "DraftStatementResult"' in blob:
            self.seen.append("draft_statement_generation")
            payload = json.loads(
                blob.split("<untrusted_document_content>\n", 1)[1].split(
                    "\n</untrusted_document_content>", 1
                )[0]
            )
            evidence = payload["evidence_sources"]
            assert evidence, "a ready task must be drafted from its evidence"
            return self._reply(
                {
                    "statements": [
                        {
                            "statement_class": "EVIDENCE_BACKED_FACT",
                            "text": "Cybranytech holds a current pen-testing accreditation.",
                            "evidence_refs": [evidence[0]["ref"]],
                        }
                    ]
                }
            )
        return super().generate(messages, temperature=temperature, max_tokens=max_tokens)


def _scene(app_sessionmaker, org_id, monkeypatch):
    reader = WorkflowReader()
    gateway = ModelGateway(reader, HashingEmbeddingProvider())
    import app.gateway.gateway as gateway_module

    monkeypatch.setattr(assessment, "build_gateway", lambda: gateway)
    monkeypatch.setattr(drafting, "build_gateway", lambda: gateway)
    monkeypatch.setattr(gateway_module, "build_gateway", lambda: gateway)

    session = app_sessionmaker()
    session.begin()
    ctx = ExecutionContext(org_id=org_id, actor_kind="SYSTEM")
    apply_tenant_context(session, ctx)
    session.add(Department(org_id=org_id, code=DepartmentCode.SECURITY, name="Security"))
    session.flush()
    _upload_company_pack(session, ctx, gateway, InMemoryStorage())
    knowledge.approve_evidence_batch(session, ctx, evidence_ids=None, reason="Reviewed.")
    scene = _tender_requirements(session, ctx, InMemoryStorage())
    report = assessment.assess_bid(session, ctx, bid_id=scene["bid"].id)
    return session, ctx, scene, report, reader


def _tasks(session, ctx, bid_id):
    rows = session.execute(select(Task).where(Task.org_id == ctx.org_id, Task.bid_id == bid_id))
    return {t.title: t for t in rows.scalars()}


def test_requirements_become_work_packages_not_one_task_per_category(
    app_sessionmaker,
    org,
    monkeypatch,  # noqa: F811
):
    session, ctx, scene, _report, reader = _scene(app_sessionmaker, org, monkeypatch)
    try:
        assert "task_grouping" in reader.seen
        by_title = _tasks(session, ctx, scene["bid"].id)
        assert set(by_title) == {
            "Accreditations and certifications",
            "Certificates and methodology",
        }
        members = set(
            session.execute(
                select(TaskRequirement.requirement_id).where(
                    TaskRequirement.task_id == by_title["Accreditations and certifications"].id
                )
            ).scalars()
        )
        assert members == {scene["2.1"].id}
    finally:
        session.rollback()
        session.close()


def test_a_ready_package_opens_with_a_draft_already_in_it(
    app_sessionmaker,
    org,
    monkeypatch,  # noqa: F811
):
    """Noor opens SEC-01 and the answer is there, cited, waiting for her."""
    session, ctx, scene, report, reader = _scene(app_sessionmaker, org, monkeypatch)
    try:
        task = _tasks(session, ctx, scene["bid"].id)["Accreditations and certifications"]
        assert task.state is TaskState.AI_READY_FOR_REVIEW
        assert report.tasks["drafts"]["drafted"] == 1
        artifact = session.execute(
            select(ResponseArtifact).where(ResponseArtifact.task_id == task.id)
        ).scalar_one()
        assert artifact.current_version_id is not None
        statements = (
            session.execute(
                select(ArtifactStatement).where(
                    ArtifactStatement.artifact_version_id == artifact.current_version_id
                )
            )
            .scalars()
            .all()
        )
        assert statements and statements[0].evidence_ids, "the draft must be cited"
        assert "draft_statement_generation" in (statements[0].generated_by or "")
    finally:
        session.rollback()
        session.close()


def test_the_blocked_package_is_not_drafted(app_sessionmaker, org, monkeypatch):  # noqa: F811
    """A draft is only prepared where the evidence says there is something to say."""
    session, ctx, scene, _report, _reader = _scene(app_sessionmaker, org, monkeypatch)
    try:
        task = _tasks(session, ctx, scene["bid"].id)["Certificates and methodology"]
        assert task.state is not TaskState.AI_READY_FOR_REVIEW
        artifact = session.execute(
            select(ResponseArtifact).where(ResponseArtifact.task_id == task.id)
        ).scalar_one()
        assert not session.execute(
            select(ArtifactStatement).where(
                ArtifactStatement.artifact_version_id == artifact.current_version_id
            )
        ).first()
    finally:
        session.rollback()
        session.close()


def test_the_owner_sees_it_under_ready_for_approval(
    app_sessionmaker,
    org,
    monkeypatch,  # noqa: F811
):
    from app.models import User

    session, ctx, scene, _report, _reader = _scene(app_sessionmaker, org, monkeypatch)
    try:
        noor = User(
            org_id=org,
            email=f"noor-{org.hex[:6]}@example.com",
            display_name="Noor",
            password_hash="x",
            is_active=True,
        )
        session.add(noor)
        session.flush()
        task = _tasks(session, ctx, scene["bid"].id)["Accreditations and certifications"]
        task.owner_user_id = noor.id
        session.flush()

        work = tasks.my_work(session, ExecutionContext(org_id=org, actor_user_id=noor.id))
        assert work["counts"]["ready_for_approval"] == 1
        assert work["counts"]["needs_my_input"] == 0
        assert [t["title"] for t in work["tasks"]] == ["Accreditations and certifications"]
    finally:
        session.rollback()
        session.close()


# --- the rules on their own ----------------------------------------------------


class _Req:
    def __init__(self, code):
        self.code = code


class _Pkg:
    def __init__(self, title, codes, rationale=""):
        self.title, self.requirement_codes, self.rationale = title, codes, rationale


def test_packages_partition_the_requirements():
    a, b, c = _Req("R-001"), _Req("R-002"), _Req("R-003")
    got = packages_from(
        [_Pkg("One", ["R-001", "r-002 "]), _Pkg("Two", ["R-002", "R-999"])], [a, b, c]
    )
    assert [(t, [r.code for r in m]) for t, m, _ in got] == [
        ("One", ["R-001", "R-002"]),
        ("Other requirements", ["R-003"]),  # forgotten, kept -- and "Two" was empty, dropped
    ]


def test_an_unusable_proposal_falls_back():
    assert packages_from([], [_Req("R-001")]) == []
    assert packages_from([_Pkg("X", ["R-999"])], [_Req("R-001")]) == []
    assert packages_from([_Pkg("", ["R-001"])], [_Req("R-001")]) == []


@pytest.mark.parametrize("state", list(TaskState))
def test_every_state_has_a_dashboard_column(state):
    assert bucket_for(state) in {
        "ready_for_approval",
        "needs_my_input",
        "waiting_on_another_department",
        "completed",
    }
