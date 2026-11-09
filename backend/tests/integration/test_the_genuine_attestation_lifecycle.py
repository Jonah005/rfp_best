"""Only a person's recorded attestation makes a document genuine, and only for the file attested.

The lifecycle: attest, include, edit the file, revoke, and the ways it must refuse. Prose in
a provenance ("Genuine: ...") never counts; the structured fields on the document do.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.errors import NotFound, StateConflict, ValidationError
from app.models import DocumentBlock, DocumentClassification, DocumentVersion
from app.models.enums import (
    BlockType,
    DocumentEvidenceStatus,
    DocumentType,
    DocumentVersionState,
    OutputMode,
    UseScope,
)
from app.services import bids, evidence_approval, release, report_attachments
from app.services.storage import InMemoryStorage
from tests.integration.test_evidence_attachments import _company_document
from tests.integration.test_release_lifecycle import _ctx, _user

pytestmark = pytest.mark.integration


def _setup(session, org_id, users):
    """A bid with a derived structure and nothing else: these tests are about evidence,
    so they do not depend on drafting a task and approving an answer first."""
    maker = _user(session, org_id, users[org_id]["manager"])
    ctx = _ctx(org_id, maker.id, "BID_MANAGER")
    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Lifecycle Customer",
        tender_name=f"Lifecycle {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    release.derive_structure(session, ctx, bid_id=bid.id)
    return ctx, bid


REAL = ["Key Personnel CVs", "Rania - architect, ten years in payment systems."]


def _attest(session, ctx, document, **over):
    args = dict(
        document_id=document.id,
        status=DocumentEvidenceStatus.APPROVED,
        use_scope=UseScope.EXTERNAL_BID,
        provenance="HR records, signed by the Resource Manager",
        reality="GENUINE",
        attestation_method="Signed original seen by the Company Secretary",
    )
    args.update(over)
    return evidence_approval.decide(session, ctx, **args)


def _attachment(session, ctx, bid_id, title="Key Personnel CVs"):
    return next(a for a in report_attachments.selected(session, ctx, bid_id) if a.title == title)


def _second_version(session, ctx, document, *, sha: str, lines: list[str]) -> DocumentVersion:
    """A later upload of the same document: a new file, hence a new hash."""
    version = DocumentVersion(
        org_id=ctx.org_id,
        document_id=document.id,
        version_no=2,
        storage_key=f"k/{uuid.uuid4()}",
        sha256=sha,
        byte_size=1,
        mime_type="text/plain",
        received_at=datetime.now(timezone.utc),
        state=DocumentVersionState.CLASSIFIED,
    )
    session.add(version)
    session.flush()
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
    # A later upload is classified like the first, or it would not be an attachable kind.
    first = session.get(DocumentVersion, document.current_version_id)
    kind = (
        session.execute(
            select(DocumentClassification).where(
                DocumentClassification.document_version_id == first.id
            )
        )
        .scalars()
        .first()
    )
    session.add(
        DocumentClassification(
            org_id=ctx.org_id,
            document_version_id=version.id,
            document_type=kind.document_type,
            confidence=1.0,
            classified_by="test",
        )
    )
    document.current_version_id = version.id
    session.flush()
    return version


def test_a_genuine_attestation_makes_the_document_eligible_and_records_who(
    app_sessionmaker, seeded_orgs, users
):
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _setup(session, seeded_orgs[0], users)
        document = _company_document(session, ctx, title="CV pack", lines=REAL)
        assert _attachment(session, ctx, bid.id).submission_status == "BLOCKED_NOT_APPROVED"

        _attest(session, ctx, document)
        assert document.evidence_reality == "GENUINE"
        assert document.attested_by == ctx.actor_user_id
        assert document.attested_at is not None
        assert document.attestation_method.startswith("Signed original")
        assert document.attested_version_hash == "0" * 64

        attachment = _attachment(session, ctx, bid.id)
        assert attachment.reality == "ATTESTED"
        assert attachment.cleared
        assert attachment.submission_status == "ELIGIBLE"
        assert not attachment.fixture

        row = next(
            r
            for r in report_attachments.evidence_manifest(session, ctx, bid.id)
            if r["title"] == "Key Personnel CVs"
        )
        assert row["submission_eligible"] is True
        assert row["attestation_valid"] is True
        assert row["attested_by"] == str(ctx.actor_user_id)
        assert row["attested_version_hash"] == "0" * 64
        assert row["attestation_method"].startswith("Signed original")
        session.rollback()


def test_a_genuine_document_is_not_reported_as_a_problem_in_a_package(
    app_sessionmaker, seeded_orgs, users
):
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _setup(session, seeded_orgs[0], users)
        document = _company_document(session, ctx, title="CV pack", lines=REAL)
        _attest(session, ctx, document)
        release.assemble(
            session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW, storage=storage
        )
        package = release.create_package(session, ctx, bid_id=bid.id, output_mode=OutputMode.REVIEW)
        release.validate_package(session, ctx, package_id=package.id, storage=storage)
        findings = release.validation_findings(session, ctx, package_id=package.id)
        codes = {f.check_code for f in findings}
        assert "FIXTURE_EVIDENCE" not in codes
        assert "PROVENANCE_UNVERIFIED" not in codes

        # A submission copy is not refused because of this attachment.
        try:
            release.assemble(
                session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION, storage=storage
            )
        except StateConflict as refusal:
            assert "attachments" not in str(refusal)
            assert "Key Personnel" not in str(refusal)
        session.rollback()


def test_editing_the_file_after_attestation_drops_it_back_to_unverified(
    app_sessionmaker, seeded_orgs, users
):
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _setup(session, seeded_orgs[0], users)
        document = _company_document(session, ctx, title="CV pack", lines=REAL)
        _attest(session, ctx, document)
        assert _attachment(session, ctx, bid.id).submission_status == "ELIGIBLE"

        _second_version(
            session, ctx, document, sha="1" * 64, lines=REAL + ["Added after attestation."]
        )
        attachment = _attachment(session, ctx, bid.id)
        assert attachment.reality == "UNVERIFIED"
        assert not attachment.cleared
        assert attachment.submission_status == "BLOCKED_PROVENANCE_UNVERIFIED"
        row = next(
            r
            for r in report_attachments.evidence_manifest(session, ctx, bid.id)
            if r["title"] == "Key Personnel CVs"
        )
        assert row["attestation_valid"] is False
        assert row["submission_eligible"] is False

        # Attesting the new file restores it.
        _attest(session, ctx, document)
        assert document.attested_version_hash == "1" * 64
        assert _attachment(session, ctx, bid.id).submission_status == "ELIGIBLE"
        session.rollback()


def test_a_missing_or_wrong_hash_is_not_an_attestation(app_sessionmaker, seeded_orgs, users):
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _setup(session, seeded_orgs[0], users)
        document = _company_document(session, ctx, title="CV pack", lines=REAL)
        _attest(session, ctx, document)

        document.attested_version_hash = None
        session.flush()
        assert _attachment(session, ctx, bid.id).reality == "UNVERIFIED"

        document.attested_version_hash = "f" * 64
        session.flush()
        assert _attachment(session, ctx, bid.id).reality == "UNVERIFIED"

        document.attested_version_hash = "0" * 64
        document.attested_by = None
        session.flush()
        assert _attachment(session, ctx, bid.id).reality == "UNVERIFIED"

        document.attested_by = ctx.actor_user_id
        document.attestation_method = None
        session.flush()
        assert _attachment(session, ctx, bid.id).reality == "UNVERIFIED"
        session.rollback()


def test_words_in_the_provenance_never_make_a_document_genuine(
    app_sessionmaker, seeded_orgs, users
):
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _setup(session, seeded_orgs[0], users)
        document = _company_document(session, ctx, title="CV pack", lines=REAL)
        evidence_approval.decide(
            session,
            ctx,
            document_id=document.id,
            status=DocumentEvidenceStatus.APPROVED,
            use_scope=UseScope.EXTERNAL_BID,
            provenance="Genuine: signed original held by the Company Secretary",
        )
        attachment = _attachment(session, ctx, bid.id)
        assert attachment.reality == "UNVERIFIED"
        assert attachment.submission_status == "BLOCKED_PROVENANCE_UNVERIFIED"
        session.rollback()


def test_synthetic_content_cannot_be_attested_as_genuine(app_sessionmaker, seeded_orgs, users):
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid = _setup(session, seeded_orgs[0], users)
        invented = _company_document(
            session,
            ctx,
            title="Letter",
            lines=["Reference Letter", "Names, dates and figures are invented."],
        )
        with pytest.raises(StateConflict, match="test data"):
            _attest(session, ctx, invented)
        with pytest.raises(StateConflict, match="test data"):
            _attest(session, ctx, invented, provenance="Dev test data entered by a developer")
        assert invented.evidence_reality == "UNVERIFIED"

        fixture = _company_document(
            session, ctx, title="Terms", lines=["Standard Terms", "FICTIONAL TEST FIXTURE."]
        )
        with pytest.raises(StateConflict, match="test fixture"):
            _attest(session, ctx, fixture)
        assert fixture.attested_by is None
        session.rollback()


def test_provenance_that_says_test_data_marks_the_document_synthetic(
    app_sessionmaker, seeded_orgs, users
):
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _setup(session, seeded_orgs[0], users)
        document = _company_document(session, ctx, title="CV pack", lines=REAL)
        evidence_approval.decide(
            session,
            ctx,
            document_id=document.id,
            status=DocumentEvidenceStatus.APPROVED,
            use_scope=UseScope.EXTERNAL_BID,
            provenance="Dev test data entered by a developer",
        )
        assert document.evidence_reality == "SYNTHETIC"
        attachment = _attachment(session, ctx, bid.id)
        assert attachment.fixture
        assert attachment.submission_status == "BLOCKED_FIXTURE"
        session.rollback()


def test_genuine_needs_approval_a_method_and_a_stored_file(app_sessionmaker, seeded_orgs, users):
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid = _setup(session, seeded_orgs[0], users)
        document = _company_document(session, ctx, title="CV pack", lines=REAL)
        with pytest.raises(ValidationError, match="approved"):
            _attest(
                session,
                ctx,
                document,
                status=DocumentEvidenceStatus.UNREVIEWED,
                use_scope=UseScope.INTERNAL_ONLY,
            )
        with pytest.raises(ValidationError, match="how you checked"):
            _attest(session, ctx, document, attestation_method="  ")
        with pytest.raises(ValidationError, match="GENUINE"):
            _attest(session, ctx, document, reality="REAL")
        assert document.attested_by is None
        session.rollback()


def test_revoking_or_rejecting_removes_the_attestation(app_sessionmaker, seeded_orgs, users):
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _setup(session, seeded_orgs[0], users)
        document = _company_document(session, ctx, title="CV pack", lines=REAL)
        _attest(session, ctx, document)
        assert _attachment(session, ctx, bid.id).submission_status == "ELIGIBLE"

        # Revoke: the same approval, without the attestation.
        _attest(session, ctx, document, reality="UNVERIFIED", attestation_method=None)
        assert document.attested_by is None
        assert document.attested_at is None
        assert document.attestation_method is None
        assert document.attested_version_hash is None
        assert (
            _attachment(session, ctx, bid.id).submission_status == "BLOCKED_PROVENANCE_UNVERIFIED"
        )

        # Attest again, then reject: nothing of the attestation survives and it is not attached.
        _attest(session, ctx, document)
        evidence_approval.decide(
            session,
            ctx,
            document_id=document.id,
            status=DocumentEvidenceStatus.REJECTED,
            use_scope=UseScope.EXTERNAL_BID,
            provenance="Superseded",
        )
        assert document.evidence_use_scope is UseScope.INTERNAL_ONLY
        assert document.attested_by is None
        assert document.attested_version_hash is None
        assert all(
            a.title != "Key Personnel CVs"
            for a in report_attachments.selected(session, ctx, bid.id)
        )
        session.rollback()


def test_another_organisation_cannot_attest_this_document(app_sessionmaker, seeded_orgs, users):
    with app_sessionmaker() as session:
        session.begin()
        ctx, _bid = _setup(session, seeded_orgs[0], users)
        document = _company_document(session, ctx, title="CV pack", lines=REAL)
        other_ctx, _other_bid = _setup(session, seeded_orgs[1], users)
        with pytest.raises(NotFound):
            _attest(session, other_ctx, document)
        assert document.attested_by is None
        session.rollback()


def test_one_unattested_attachment_refuses_the_whole_submission_copy(
    app_sessionmaker, seeded_orgs, users
):
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _setup(session, seeded_orgs[0], users)
        genuine = _company_document(session, ctx, title="CV pack", lines=REAL)
        _attest(session, ctx, genuine)
        other = _company_document(
            session,
            ctx,
            title="Terms",
            lines=["Standard Terms", "Warranty is thirty days from acceptance."],
            doc_type=DocumentType.LEGAL_DOC,
        )
        evidence_approval.decide(
            session,
            ctx,
            document_id=other.id,
            status=DocumentEvidenceStatus.APPROVED,
            use_scope=UseScope.EXTERNAL_BID,
            provenance="Resource Manager",
        )
        with pytest.raises(StateConflict, match="not been approved and cleared"):
            release.assemble(
                session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION, storage=storage
            )
        session.rollback()
