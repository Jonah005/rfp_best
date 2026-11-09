"""Ahmed uploads the mobile methodology from inside SEC-03.

The upload is company knowledge and waits for a knowledge administrator; until
approved it is not a source for the draft. Once approved, the task owner is
told, and the next draft of that task can cite it. A fact a contributor types
in waits the same way.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.models import Department, KnowledgeEntity, Notification, User
from app.models.enums import (
    ApprovalStatus,
    DepartmentCode,
    DocumentType,
    EntityType,
)
from app.services import artifacts, drafting, knowledge, task_evidence, tasks
from app.services.storage import InMemoryStorage
from tests.integration.test_a_real_document_reaches_draftable import (
    FIXTURE,
    Reader,
    _tender_requirements,
    org,  # noqa: F401 - pytest fixture
)

# ruff: noqa: F811 - `org` is a fixture, imported and then requested by name

pytestmark = pytest.mark.integration


class DraftReader(Reader):
    """Records which evidence each draft was offered, and cites the first."""

    def __init__(self):
        super().__init__()
        self.offered: list[list[str]] = []

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        blob = "\n".join(m.content for m in messages)
        if '"title": "DraftStatementResult"' in blob:
            body = blob.split("<untrusted_document_content>\n", 1)[1]
            payload = json.loads(body.split("\n</untrusted_document_content>", 1)[0])
            excerpts = [e["excerpt"] for e in payload["evidence_sources"]]
            self.offered.append(excerpts)
            if payload["evidence_sources"]:
                statement = {
                    "statement_class": "EVIDENCE_BACKED_FACT",
                    "text": "Our methodology covers this.",
                    "evidence_refs": [payload["evidence_sources"][0]["ref"]],
                }
            else:
                statement = {
                    "statement_class": "ASSUMPTION",
                    "text": "Methodology evidence is awaiting approval.",
                }
            return self._reply({"statements": [statement]})
        return super().generate(messages, temperature=temperature, max_tokens=max_tokens)


def _scene(app_sessionmaker, org_id):
    reader = DraftReader()
    gateway = ModelGateway(reader, HashingEmbeddingProvider())
    session = app_sessionmaker()
    session.begin()
    system = ExecutionContext(org_id=org_id, actor_kind="SYSTEM")
    apply_tenant_context(session, system)
    security = Department(org_id=org_id, code=DepartmentCode.SECURITY, name="Security")
    ahmed = User(
        org_id=org_id,
        email=f"ahmed-{org_id.hex[:6]}@example.com",
        display_name="Ahmed",
        password_hash="x",
        is_active=True,
    )
    session.add_all([security, ahmed])
    session.flush()
    scene = _tender_requirements(session, system, InMemoryStorage())
    task = tasks.create_task(
        session,
        system,
        bid_id=scene["bid"].id,
        title="VAPT methodology",
        department_id=security.id,
        requirement_ids=[scene["3.3"].id],
        owner_user_id=ahmed.id,
    )
    ahmed_ctx = ExecutionContext(org_id=org_id, actor_user_id=ahmed.id)
    return session, system, ahmed_ctx, task, gateway, reader, ahmed


def test_an_upload_waits_for_approval_then_feeds_the_draft(app_sessionmaker, org):
    session, system, ahmed_ctx, task, gateway, reader, ahmed = _scene(app_sessionmaker, org)
    try:
        storage = InMemoryStorage()
        version = task_evidence.upload_to_task(
            session,
            ahmed_ctx,
            task_id=task.id,
            data=(FIXTURE / "company" / "06_vapt_methodology.md").read_bytes(),
            filename="mobile_methodology.md",
            document_type=DocumentType.METHODOLOGY,
            storage=storage,
        )
        task_evidence.parse_now(
            session,
            system,
            version_id=version.id,
            storage=storage,
            embedder=HashingEmbeddingProvider(),
            gateway=gateway,
        )

        [row] = task_evidence.status(session, ahmed_ctx, task_id=task.id)
        assert row["excerpts"] > 0
        assert row["approved"] == 0 and row["usable_in_draft"] is False

        artifact = artifacts.for_task(session, ahmed_ctx, task_id=task.id)
        drafting.generate(session, ahmed_ctx, artifact_id=artifact.id, gateway=gateway)
        assert reader.offered[-1] == [], "an unapproved upload must not be a draft source"

        knowledge.approve_evidence_batch(session, system, evidence_ids=None, reason="Reviewed.")
        [row] = task_evidence.status(session, ahmed_ctx, task_id=task.id)
        assert row["usable_in_draft"] is True

        told = (
            session.execute(
                select(Notification).where(
                    Notification.recipient_user_id == ahmed.id,
                    Notification.kind == "task.evidence_approved",
                )
            )
            .scalars()
            .all()
        )
        assert told, "the task owner must be told the upload is usable"

        drafting.generate(session, ahmed_ctx, artifact_id=artifact.id, gateway=gateway)
        assert reader.offered[-1], "the approved upload must now be offered to the draft"
    finally:
        session.rollback()
        session.close()


def test_only_someone_on_the_task_can_add_to_it(app_sessionmaker, org):
    from app.core.errors import PermissionDenied

    session, system, _ahmed_ctx, task, _gateway, _reader, _ahmed = _scene(app_sessionmaker, org)
    try:
        stranger = ExecutionContext(org_id=org, actor_user_id=__import__("uuid").uuid4())
        with pytest.raises(PermissionDenied):
            task_evidence.upload_to_task(
                session,
                stranger,
                task_id=task.id,
                data=b"# x\n\nsome text\n",
                filename="x.md",
                storage=InMemoryStorage(),
            )
    finally:
        session.rollback()
        session.close()


def test_a_typed_fact_from_someone_who_cannot_approve_waits(app_sessionmaker, org):
    """Sarah types "Dubai Entity Z, 30 web apps"; it is not evidence until verified."""
    session, system, _ahmed_ctx, _task, _gateway, _reader, _ahmed = _scene(app_sessionmaker, org)
    try:
        entity, _ = knowledge.resolve_or_create_entity(
            session, system, entity_type=EntityType.PROJECT, name="Dubai Entity Z"
        )
        assert isinstance(entity, KnowledgeEntity)
        fact = knowledge.add_fact(
            session,
            system,
            entity_id=entity.id,
            predicate="web_applications_tested",
            value_number=30,
            approved=False,
        )
        assert fact.approval_status is ApprovalStatus.PENDING_APPROVAL
        vouched = knowledge.add_fact(
            session,
            system,
            entity_id=entity.id,
            predicate="sector",
            value_text="Government",
            approved=True,
        )
        assert vouched.approval_status is ApprovalStatus.APPROVED
    finally:
        session.rollback()
        session.close()
