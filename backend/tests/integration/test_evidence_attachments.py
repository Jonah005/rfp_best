"""Attachments are separate, versioned files; evidence needs a person's decision."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.core.errors import StateConflict, ValidationError
from app.models import (
    Document,
    DocumentBlock,
    DocumentClassification,
    DocumentVersion,
    GeneratedDocument,
)
from app.models.enums import (
    BlockType,
    ConfidentialityClass,
    DocumentEvidenceStatus,
    DocumentSide,
    DocumentType,
    DocumentVersionState,
    OutputMode,
    UseScope,
)
from app.services import evidence_approval, release, report_attachments
from app.services.storage import InMemoryStorage
from tests.integration.test_release_lifecycle import _approved_artifact, _user

pytestmark = pytest.mark.integration

FIXTURE_NOTICE = "FICTIONAL TEST FIXTURE. The company does not exist."


def _company_document(session, ctx, *, title: str, lines: list[str], doc_type=DocumentType.CV):
    document = Document(
        org_id=ctx.org_id,
        side=DocumentSide.COMPANY,
        title=title,
        original_filename=f"{title}.docx",
        confidentiality_class=ConfidentialityClass.CONFIDENTIAL,
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        org_id=ctx.org_id,
        document_id=document.id,
        version_no=1,
        storage_key=f"k/{uuid.uuid4()}",
        sha256="0" * 64,
        byte_size=1,
        mime_type="text/plain",
        received_at=datetime.now(timezone.utc),
        state=DocumentVersionState.CLASSIFIED,
    )
    session.add(version)
    session.flush()
    document.current_version_id = version.id
    position = 0
    for n, text in enumerate(lines):
        session.add(
            DocumentBlock(
                org_id=ctx.org_id,
                document_version_id=version.id,
                block_type=BlockType.PARAGRAPH,
                ordinal=n,
                text=text,
                char_start=position,
                char_end=position + len(text),
            )
        )
        position += len(text) + 1
    session.add(
        DocumentClassification(
            org_id=ctx.org_id,
            document_version_id=version.id,
            document_type=doc_type,
            confidence=1.0,
            classified_by="test",
        )
    )
    session.flush()
    return document


def _setup(session, org_id, users):
    maker = _user(session, org_id, users[org_id]["manager"])
    ctx, bid, _artifact = _approved_artifact(session, org_id, maker)
    release.derive_structure(session, ctx, bid_id=bid.id)
    return ctx, bid


def test_a_fixture_cannot_be_approved_whoever_asks(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid = _setup(session, org_id, users)
        fixture = _company_document(
            session, ctx, title="CV pack", lines=["Key Personnel", FIXTURE_NOTICE]
        )
        with pytest.raises(StateConflict, match="test fixture"):
            evidence_approval.decide(
                session,
                ctx,
                document_id=fixture.id,
                status=DocumentEvidenceStatus.APPROVED,
                use_scope=UseScope.EXTERNAL_BID,
                provenance="HR records",
            )
        session.rollback()


def test_approval_needs_a_provenance_and_clearing_needs_approval(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid = _setup(session, org_id, users)
        real = _company_document(session, ctx, title="CV pack", lines=["Key Personnel", "A. B."])
        with pytest.raises(ValidationError, match="came from"):
            evidence_approval.decide(
                session,
                ctx,
                document_id=real.id,
                status=DocumentEvidenceStatus.APPROVED,
                use_scope=UseScope.EXTERNAL_BID,
                provenance=None,
            )
        with pytest.raises(ValidationError, match="approved"):
            evidence_approval.decide(
                session,
                ctx,
                document_id=real.id,
                status=DocumentEvidenceStatus.UNREVIEWED,
                use_scope=UseScope.EXTERNAL_BID,
                provenance="HR",
            )
        decided = evidence_approval.decide(
            session,
            ctx,
            document_id=real.id,
            status=DocumentEvidenceStatus.APPROVED,
            use_scope=UseScope.EXTERNAL_BID,
            provenance="HR records, signed by the Resource Manager",
        )
        assert decided.evidence_decided_by == ctx.actor_user_id
        assert decided.evidence_decided_at is not None
        session.rollback()


def test_attachments_are_separate_versioned_files_and_block_a_submission_until_cleared(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _setup(session, org_id, users)
        real = _company_document(
            session, ctx, title="CV pack", lines=["Key Personnel CVs", "Rania - architect."]
        )
        _company_document(
            session,
            ctx,
            title="Terms",
            lines=["Standard Terms", FIXTURE_NOTICE],
            doc_type=DocumentType.LEGAL_DOC,
        )

        first = release.assemble(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW, storage=storage
        )
        names = {session.get(GeneratedDocument, v.generated_document_id).name for v in first}
        assert "Attachment: Key Personnel CVs" in names
        assert "Attachment: Standard Terms" in names
        cv_one = next(
            v
            for v in first
            if session.get(GeneratedDocument, v.generated_document_id).name
            == "Attachment: Key Personnel CVs"
        )

        # Nothing changed, so the attachment keeps its version.
        again = release.assemble(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW, storage=storage
        )
        cv_two = next(v for v in again if v.generated_document_id == cv_one.generated_document_id)
        assert cv_two.id == cv_one.id

        # A decision changes what the attachment says about itself: new version.
        evidence_approval.decide(
            session,
            ctx,
            document_id=real.id,
            status=DocumentEvidenceStatus.APPROVED,
            use_scope=UseScope.EXTERNAL_BID,
            provenance="HR records",
        )
        third = release.assemble(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW, storage=storage
        )
        cv_three = next(v for v in third if v.generated_document_id == cv_one.generated_document_id)
        assert cv_three.version_no == cv_one.version_no + 1
        assert cv_three.generation_trace["evidence_status"] == "APPROVED"

        # The package carries them and the validator names each problem.
        package = release.create_package(session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW)
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        codes = [
            f.check_code for f in release.validation_findings(session, ctx, package_id=package.id)
        ]
        assert "FIXTURE_EVIDENCE" in codes
        assert "EVIDENCE_NOT_APPROVED" not in codes  # the real one is cleared

        # A submission copy is refused while a fixture is attached.
        with pytest.raises(StateConflict, match="test fixtures"):
            release.assemble(
                session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION, storage=storage
            )
        assert report_attachments.fixture_titles(session, ctx) == ["Standard Terms"]
        session.rollback()


def test_an_unapproved_real_document_is_listed_as_uncleared(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid = _setup(session, org_id, users)
        _company_document(session, ctx, title="CV pack", lines=["Key Personnel", "A. B."])
        assert report_attachments.uncleared_titles(session, ctx) == ["Key Personnel"]
        assert report_attachments.fixture_titles(session, ctx) == []
        session.rollback()


def test_answers_quoting_fixture_facts_are_noted_in_review_and_block_a_submission(
    app_sessionmaker, seeded_orgs, users
):
    from sqlalchemy import select

    from app.models import ArtifactStatement

    org_id = seeded_orgs[0]
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        maker = _user(session, org_id, users[org_id]["manager"])
        ctx, bid, artifact = _approved_artifact(session, org_id, maker)
        release.derive_structure(session, ctx, bid_id=bid.id)
        # A fixture that is not an attachable kind, so only the claims check can object.
        _company_document(
            session,
            ctx,
            title="Company profile",
            lines=["Profile", "Rania Haddad leads Al Waha Capital delivery.", FIXTURE_NOTICE],
            doc_type=DocumentType.COMPANY_PROFILE,
        )
        statement = (
            session.execute(
                select(ArtifactStatement).where(
                    ArtifactStatement.artifact_version_id == artifact.approved_version_id
                )
            )
            .scalars()
            .first()
        )
        statement.text = "Rania Haddad led the Al Waha Capital programme."
        session.flush()

        facts = report_attachments.fixture_only_facts(session, ctx, bid.id)
        assert "Rania Haddad" in facts
        held = release._tainted_answers(session, ctx, bid.id, facts)
        assert list(held) == [artifact.title]

        release.assemble(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW, storage=storage
        )
        with pytest.raises(StateConflict, match="fixture documents"):
            release.assemble(
                session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION, storage=storage
            )
        session.rollback()


def test_generated_reference_tables_are_separate_files(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _setup(session, org_id, users)
        rows = release.assemble(
            session,
            ctx,
            bid_id=bid.id,
            output_mode=OutputMode.REVIEW,
            storage=storage,
            join=True,
        )
        names = {session.get(GeneratedDocument, v.generated_document_id).name for v in rows}
        assert "Technical Proposal" in names
        generated = [v for v in rows if (v.generation_trace or {}).get("generated")]
        for version in generated:
            assert version.generation_trace["stream"] == "SUPPORTING"
        package = release.create_package(session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW)
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        codes = [
            f.check_code for f in release.validation_findings(session, ctx, package_id=package.id)
        ]
        assert "EVIDENCE_NOT_APPROVED" not in codes
        session.rollback()
