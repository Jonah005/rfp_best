from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import PermissionDenied, StateConflict, ValidationError
from app.db.session import apply_tenant_context
from app.gateway.base import LLMResponse
from app.gateway.deepseek import NullLLMProvider
from app.gateway.gateway import ModelGateway
from app.models import Department, ResponseArtifact, User
from app.models.enums import (
    ApprovalStatus,
    ApprovalStepState,
    ArtifactState,
    ArtifactVersionState,
    StatementClass,
    TaskState,
)
from app.services import artifacts, bids, drafting, tasks

pytestmark = pytest.mark.integration


class DraftProvider(NullLLMProvider):
    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        self.calls.append(messages)
        return LLMResponse(
            text=(
                '{"statements":[{"statement_class":"ASSUMPTION",'
                '"text":"The client will provide secure remote access.",'
                '"evidence_refs":[]}]}'
            ),
            model_id="scripted-draft",
        )


def _context(org_id: uuid.UUID, actor_id: uuid.UUID, *roles: str) -> ExecutionContext:
    return ExecutionContext(
        org_id=org_id,
        actor_user_id=actor_id,
        role_codes=frozenset(roles),
    )


def _make_task(session, org_id: uuid.UUID, actor_id: uuid.UUID):
    ctx = _context(org_id, actor_id, "BID_MANAGER")
    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Artifact Customer",
        tender_name=f"Artifact Tender {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    department = (
        session.execute(select(Department).where(Department.org_id == org_id)).scalars().first()
    )
    assert department is not None
    task = tasks.create_task(
        session,
        ctx,
        bid_id=bid.id,
        title="Write technical response",
        department_id=department.id,
        owner_user_id=actor_id,
    )
    artifact = session.execute(
        select(ResponseArtifact).where(ResponseArtifact.task_id == task.id)
    ).scalar_one()
    return ctx, task, artifact


def _actor(session, org_id: uuid.UUID, email: str) -> User:
    apply_tenant_context(session, ExecutionContext(org_id=org_id))
    return session.execute(
        select(User).where(User.org_id == org_id, User.email == email)
    ).scalar_one()


def test_task_creation_starts_one_continuous_artifact_lifecycle(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _actor(session, org_id, users[org_id]["manager"])
        ctx, task, artifact = _make_task(session, org_id, actor.id)

        assert artifact.state is ArtifactState.DRAFT
        version = artifacts.versions(session, ctx, artifact_id=artifact.id)
        assert len(version) == 1
        assert version[0].state is ArtifactVersionState.DRAFT
        assert artifact.current_version_id == version[0].id

        artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {
                    "statement_class": "HUMAN_PROVIDED_INPUT",
                    "text": "The delivery team will coordinate the response.",
                },
                {
                    "statement_class": "TENDER_DERIVED",
                    "text": "The response covers the requested scope.",
                    "source_locator": {"clause_ref": "4.2", "document_version_id": "demo"},
                },
            ],
        )
        artifacts.submit(
            session,
            ctx,
            artifact_id=artifact.id,
            approval_roles=["BID_MANAGER"],
        )
        approval = artifacts.approval_chain(
            session,
            ctx,
            entity_type="ARTIFACT_VERSION",
            entity_id=artifact.current_version_id,
        )[0]
        artifacts.decide_approval(
            session,
            ctx,
            approval_id=approval.id,
            decision=ApprovalStepState.APPROVED,
            comment="Reviewed against the requirement.",
        )

        first = artifacts.version_by_number(session, ctx, artifact_id=artifact.id, version_no=1)
        first_text = first.rendered_text
        assert first.state is ArtifactVersionState.APPROVED
        assert task.state is TaskState.COMPLETED

        second = artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {
                    "statement_class": "HUMAN_PROVIDED_INPUT",
                    "text": "A later edit belongs to version two.",
                }
            ],
            change_reason="Customer requested a wording change.",
        )
        assert second.version_no == 2
        assert second.state is ArtifactVersionState.DRAFT
        assert first.state is ArtifactVersionState.APPROVED
        assert first.rendered_text == first_text
        session.commit()


def test_commitments_need_the_named_approver_before_artifact_approval(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _actor(session, org_id, users[org_id]["manager"])
        ctx, _, artifact = _make_task(session, org_id, actor.id)
        version = artifacts.save_draft(
            session,
            ctx,
            artifact_id=artifact.id,
            statements=[
                {
                    "statement_class": "PROPOSED_COMMITMENT",
                    "text": "We will provide weekly executive reporting.",
                    "required_approver_role": "BID_MANAGER",
                    "commitment_owner_user_id": actor.id,
                }
            ],
        )
        artifacts.submit(
            session,
            ctx,
            artifact_id=artifact.id,
            approval_roles=["BID_MANAGER"],
        )
        approval = artifacts.approval_chain(
            session,
            ctx,
            entity_type="ARTIFACT_VERSION",
            entity_id=version.id,
        )[0]
        with pytest.raises(StateConflict, match="commitment"):
            artifacts.decide_approval(
                session,
                ctx,
                approval_id=approval.id,
                decision=ApprovalStepState.APPROVED,
            )

        commitment = artifacts.list_commitments(session, ctx, bid_id=artifact.bid_id)[0]
        outsider_ctx = _context(org_id, actor.id, "REVIEWER")
        with pytest.raises(PermissionDenied):
            artifacts.decide_commitment(
                session,
                outsider_ctx,
                commitment_id=commitment.id,
                decision=ApprovalStatus.APPROVED,
            )
        artifacts.decide_commitment(
            session,
            ctx,
            commitment_id=commitment.id,
            decision=ApprovalStatus.APPROVED,
        )
        artifacts.decide_approval(
            session,
            ctx,
            approval_id=approval.id,
            decision=ApprovalStepState.APPROVED,
        )
        assert artifact.state is ArtifactState.APPROVED
        session.commit()


def test_a_task_with_nothing_left_to_answer_is_not_drafted(app_sessionmaker, seeded_orgs, users):
    """With no live requirement the model used to write "this cannot be drafted",
    and that sentence was saved and submitted as the company's response."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        actor = _actor(session, org_id, users[org_id]["manager"])
        ctx, task, artifact = _make_task(session, org_id, actor.id)
        from app.models import Requirement
        from app.models.enums import RequirementLifecycle

        for requirement in session.execute(
            select(Requirement).where(Requirement.bid_id == task.bid_id)
        ).scalars():
            requirement.lifecycle_state = RequirementLifecycle.WITHDRAWN
        session.flush()
        provider = DraftProvider()
        with pytest.raises(ValidationError, match="no live tender requirements"):
            drafting.generate(session, ctx, artifact_id=artifact.id, gateway=ModelGateway(provider))
        assert provider.calls == []
        session.commit()


def test_structured_model_drafting_writes_only_through_typed_artifact_governance(
    app_sessionmaker, seeded_orgs, users, monkeypatch
):
    org_id = seeded_orgs[0]
    # The task here has no linked clause; give the drafter one tender source so the
    # test stays about how a model's proposals are written, not about sourcing.
    monkeypatch.setattr(
        drafting,
        "gather_sources",
        lambda session, ctx, *, task_id: (
            {"T1": {"code": "R-1", "source_locator": {"clause_ref": "1"}, "text": "A clause."}},
            {},
            [],
        ),
    )
    with app_sessionmaker() as session:
        session.begin()
        actor = _actor(session, org_id, users[org_id]["manager"])
        ctx, _, artifact = _make_task(session, org_id, actor.id)
        provider = DraftProvider()
        version = drafting.generate(
            session,
            ctx,
            artifact_id=artifact.id,
            instruction="Draft the response and surface assumptions.",
            gateway=ModelGateway(provider),
        )
        rows = artifacts.statements_for_version(session, ctx, version_id=version.id)
        assert len(rows) == 1
        assert rows[0].statement_class.value == "ASSUMPTION"
        assert rows[0].assumption_id is not None
        # The invariant is that the stamp names the reading that produced the
        # row -- not that the reading is any particular version. Pinning the
        # literal "v1" meant a test about provenance broke on every prompt
        # change, and it sat red through the v2 and v3 bumps saying nothing
        # about either. Derived from the registry it still fails if the stamp
        # stops matching the call actually made, which is the part worth
        # protecting.
        from app.gateway.registry import get as get_call_type

        call = get_call_type("draft_statement_generation")
        assert rows[0].generated_by == f"llm:{call.name}@{call.prompt_version}"
        assert len(provider.calls) == 1
        session.commit()


def test_a_fact_rewritten_as_a_commitment_is_routed_to_the_bid_manager():
    from app.schemas.drafting import DraftStatementProposal
    from app.services.drafting import map_proposals

    # Built without validation on purpose: the schema refuses a fact with no
    # citation, but a fact whose citation cannot prove it reaches the same branch.
    fact = DraftStatementProposal.model_construct(
        statement_class=StatementClass.EVIDENCE_BACKED_FACT,
        text="We have delivered similar portals before.",
        paragraph=None,
        answers=[],
        evidence_refs=[],
        tender_source_ref=None,
        required_approver_role=None,
    )
    ctx = _context(uuid.uuid4(), uuid.uuid4(), "BID_MANAGER")
    (row,) = map_proposals(ctx, [fact], {}, {})
    assert row["statement_class"] == "PROPOSED_COMMITMENT"
    assert row["required_approver_role"] == "BID_MANAGER"


def test_untagged_sentences_inherit_the_clause_the_tender_statement_names():
    """The first draft's sentences answer the clause they follow, so the write-up
    does not write that answer a second time."""
    from app.schemas.drafting import DraftStatementProposal
    from app.services.drafting import map_proposals

    def proposal(klass, text, **kw):
        base = dict(
            statement_class=klass,
            text=text,
            paragraph=None,
            answers=[],
            evidence_refs=[],
            tender_source_ref=None,
            required_approver_role=None,
        )
        base.update(kw)
        return DraftStatementProposal.model_construct(**base)

    sources = {
        "T1": {"code": "R-1", "source_locator": {"clause_ref": "1"}},
        "T2": {"code": "R-2", "source_locator": {"clause_ref": "2"}},
    }
    ctx = _context(uuid.uuid4(), uuid.uuid4(), "BID_MANAGER")
    rows = map_proposals(
        ctx,
        [
            proposal(StatementClass.TENDER_DERIVED, "R-1 requires a.", tender_source_ref="T1"),
            proposal(
                StatementClass.PROPOSED_COMMITMENT, "We will do a.", required_approver_role="LEGAL"
            ),
            proposal(StatementClass.TENDER_DERIVED, "R-2 requires b.", tender_source_ref="T2"),
            proposal(StatementClass.ASSUMPTION, "We assume b."),
            proposal(
                StatementClass.PROPOSED_COMMITMENT,
                "Tagged elsewhere.",
                answers=["R-1"],
                required_approver_role="LEGAL",
            ),
        ],
        sources,
        {},
    )
    assert [r.get("requirement_codes") for r in rows] == [
        ["R-1"],
        ["R-1"],
        ["R-2"],
        ["R-2"],
        ["R-1"],
    ]
