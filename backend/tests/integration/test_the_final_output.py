"""Assembly into the customer's structure, two submissions, two modes, and the last checks.

The approved answers go into the sections the tender asks for; the technical
and commercial proposals come out as separate documents; the review copy shows
every claim's footing and the submission copy shows none of it; and release is
blocked by what is still open -- empty sections, unfinished tasks, unanswered
mandatory requirements, open customer questions, deliverables not ready.
"""

from __future__ import annotations

import io
import uuid

import pytest
from docx import Document as WordDocument
from sqlalchemy import select

from app.gateway.base import LLMResponse
from app.gateway.deepseek import NullLLMProvider
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.models import (
    ClarificationIssue,
    Deliverable,
    Department,
    GeneratedDocument,
    ProposalSection,
)
from app.models.enums import (
    ApprovalStepState,
    ClarificationIssueType,
    ClarificationSeverity,
    ClarificationStatus,
    DeliverableStatus,
    DepartmentCode,
    DocumentStream,
    OutputMode,
)
from app.services import artifacts, bids, release, tasks
from app.services.storage import InMemoryStorage
from tests.integration.test_release_lifecycle import _ctx, _user

pytestmark = pytest.mark.integration


class StructureReader(NullLLMProvider):
    """Answers `proposal_structure` the way a customer's RFP would lay it out."""

    model_id = "scripted-structure"

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        import json
        import re

        blob = "\n".join(m.content for m in messages)
        refs = dict(re.findall(r'"ref":\s*"(A\d+)",\s*"title":\s*"([^"]+)"', blob))
        by_title = {title: ref for ref, title in refs.items()}
        return LLMResponse(
            text=json.dumps(
                {
                    "sections": [
                        {"number_label": "1", "title": "Executive Summary", "artifact_refs": []},
                        {
                            "number_label": "4",
                            "title": "Proposed VAPT Methodology",
                            "artifact_refs": [by_title.get("VAPT methodology", "")],
                        },
                        {
                            "number_label": "C1",
                            "title": "Commercial Offer",
                            "stream": "COMMERCIAL",
                            "artifact_refs": [by_title.get("Commercial BOQ", "")],
                        },
                    ]
                }
            ),
            model_id=self.model_id,
            latency_ms=0,
        )


def _department(session, org_id, code):
    row = session.execute(
        select(Department).where(Department.org_id == org_id, Department.code == code)
    ).scalar_one_or_none()
    if row is None:
        row = Department(org_id=org_id, code=code, name=code.value.title())
        session.add(row)
        session.flush()
    return row


def _approved(session, ctx, *, bid, department, title, text):
    task = tasks.create_task(
        session,
        ctx,
        bid_id=bid.id,
        title=title,
        department_id=department.id,
        owner_user_id=ctx.actor_user_id,
    )
    artifact = artifacts.for_task(session, ctx, task_id=task.id)
    version = artifacts.save_draft(
        session,
        ctx,
        artifact_id=artifact.id,
        statements=[{"statement_class": "HUMAN_PROVIDED_INPUT", "text": text}],
    )
    artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
    approval = artifacts.approval_chain(
        session, ctx, entity_type="ARTIFACT_VERSION", entity_id=version.id
    )[0]
    artifacts.decide_approval(
        session, ctx, approval_id=approval.id, decision=ApprovalStepState.APPROVED
    )
    return task, artifact


def _bid_with_two_answers(session, org_id, users):
    maker = _user(session, org_id, users[org_id]["manager"])
    ctx = _ctx(org_id, maker.id, "BID_MANAGER")
    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Dubai Government Entity",
        tender_name=f"VAPT {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    security = _department(session, org_id, DepartmentCode.SECURITY)
    commercial = _department(session, org_id, DepartmentCode.COMMERCIAL)
    _approved(
        session,
        ctx,
        bid=bid,
        department=security,
        title="VAPT methodology",
        text="We test web applications and APIs against OWASP ASVS.",
    )
    _approved(
        session,
        ctx,
        bid=bid,
        department=commercial,
        title="Commercial BOQ",
        text="The fixed price for the engagement is AED 180,000.",
    )
    return ctx, bid


def _text(storage, version) -> str:
    document = WordDocument(io.BytesIO(storage.get(version.storage_key)))
    return "\n".join(p.text for p in document.paragraphs)


def _streams(session, versions):
    return {session.get(GeneratedDocument, v.generated_document_id).stream: v for v in versions}


def test_the_customers_structure_and_two_separate_submissions(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _bid_with_two_answers(session, org_id, users)
        gateway = ModelGateway(StructureReader(), HashingEmbeddingProvider())
        structure = release.derive_structure(session, ctx, bid_id=bid.id, gateway=gateway)
        assert structure.derivation_method.value == "TENDER_SPECIFIED"
        titles = [
            (s.number_label, s.title)
            for s in release.sections(session, ctx, structure_id=structure.id)
        ]
        assert titles == [
            ("1", "Executive Summary"),
            ("4", "Proposed VAPT Methodology"),
            ("C1", "Commercial Offer"),
        ]

        review = _streams(
            session,
            release.assemble(
                session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW, storage=storage
            ),
        )
        assert set(review) == {DocumentStream.TECHNICAL, DocumentStream.COMMERCIAL}
        technical = _text(storage, review[DocumentStream.TECHNICAL])
        commercial = _text(storage, review[DocumentStream.COMMERCIAL])
        assert "OWASP ASVS" in technical and "AED 180,000" not in technical
        assert "AED 180,000" in commercial and "OWASP" not in commercial
        assert release.NO_CONTENT in technical, "the empty Executive Summary must show as a gap"
        assert "[Provided by a contributor]" in technical, "review copy shows each claim's footing"

        clean = _streams(
            session,
            release.assemble(
                session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION, storage=storage
            ),
        )
        submission = _text(storage, clean[DocumentStream.TECHNICAL])
        assert "OWASP ASVS" in submission
        assert "[" not in submission, "the customer's copy carries no internal markers"
        session.rollback()


def test_without_a_reading_of_the_tender_answers_still_split_by_submission(
    app_sessionmaker, seeded_orgs, users
):
    """The fallback: one section per answer, commercial answers in the commercial proposal."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _bid_with_two_answers(session, org_id, users)
        structure = release.derive_structure(
            session,
            ctx,
            bid_id=bid.id,
            gateway=ModelGateway(NullLLMProvider("{}"), HashingEmbeddingProvider()),
        )
        placed = {
            s.title: session.get(GeneratedDocument, s.target_document_id).stream
            for s in session.execute(
                select(ProposalSection).where(ProposalSection.structure_id == structure.id)
            ).scalars()
        }
        assert placed == {
            "VAPT methodology": DocumentStream.TECHNICAL,
            "Commercial BOQ": DocumentStream.COMMERCIAL,
        }
        session.rollback()


def test_release_is_blocked_by_what_is_still_open(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _bid_with_two_answers(session, org_id, users)
        security = _department(session, org_id, DepartmentCode.SECURITY)
        tasks.create_task(
            session, ctx, bid_id=bid.id, title="Project plan", department_id=security.id
        )
        session.add(
            Deliverable(
                org_id=org_id,
                bid_id=bid.id,
                name="Signed NDA",
                is_mandatory=True,
                status=DeliverableStatus.IDENTIFIED,
                extracted_by="test",
                fingerprint="nda",
            )
        )
        session.add(
            ClarificationIssue(
                org_id=org_id,
                bid_id=bid.id,
                issue_type=ClarificationIssueType.UNDEFINED_FIELD,
                description="Retesting window undefined.",
                severity=ClarificationSeverity.MAJOR,
                status=ClarificationStatus.OPEN,
                recommended_question="How long is the retesting window?",
                detected_by="test",
                fingerprint=f"retest-{uuid.uuid4().hex[:8]}",
            )
        )
        session.flush()
        gateway = ModelGateway(StructureReader(), HashingEmbeddingProvider())
        release.derive_structure(session, ctx, bid_id=bid.id, gateway=gateway)
        release.assemble(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW, storage=storage
        )
        package = release.create_package(session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW)
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        codes = {
            f.check_code for f in release.validation_findings(session, ctx, package_id=package.id)
        }

        assert "SECTION_WITHOUT_CONTENT" in codes  # the Executive Summary
        assert "UNRESOLVED_TASK" in codes  # the project plan nobody approved
        assert "OPEN_TENDER_GAP" in codes
        assert "MANDATORY_DELIVERABLE_NOT_READY" in codes
        assert "PRICE_IN_TECHNICAL" not in codes, "the price is in the commercial submission"
        session.rollback()
