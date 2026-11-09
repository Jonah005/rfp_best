"""Filing a company document at ingestion, so its excerpts know what they are.

Uploaded without a stated type, a document used to leave every excerpt typed
OTHER -- and `is_self_evidencing` then treated all of them as proof. A blank
form and a signed reference letter were the same thing to the drafting prompt.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.gateway.base import LLMResponse
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.ingestion.safety import EicarAwareScanner
from app.models import DocumentClassification, Evidence
from app.models.enums import ConfidentialityClass, DocumentSide, DocumentType, is_self_evidencing
from app.services import document_classification, ingestion
from app.services.storage import InMemoryStorage
from tests.integration.test_a_real_document_reaches_draftable import (
    Reader,
    org,  # noqa: F401 - pytest fixture
)

# ruff: noqa: F811 - `org` is a fixture, imported and then requested by name

pytestmark = pytest.mark.integration

FORM = b"""# CV Update Form -- Software Tester

Name: ____________________
Certifications held: ____________________
Years of testing experience: ____________________

Return the completed form to HR.
"""


class Filing(Reader):
    """The document reader, plus the filing call, answered from the text."""

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        blob = "\n".join(m.content for m in messages)
        if '"title": "DocumentClassificationResult"' in blob:
            self.seen.append("document_classification")
            blank = "____" in blob
            return LLMResponse(
                text=json.dumps(
                    {
                        "document_type": "STANDARD_FORM" if blank else "REFERENCE_LETTER",
                        "confidence": 0.9,
                        "reason": "Unfilled fields." if blank else "Signed by the customer.",
                    }
                ),
                model_id="scripted-filing",
                latency_ms=0,
            )
        return super().generate(messages, temperature=temperature, max_tokens=max_tokens)


def _upload(session, ctx, gateway, storage, *, data, filename, document_type=None):
    version = ingestion.register_upload(
        session,
        ctx,
        data=data,
        filename=filename,
        side=DocumentSide.COMPANY,
        confidentiality_class=ConfidentialityClass.INTERNAL,
        document_type=document_type,
        scanner=EicarAwareScanner(),
        storage=storage,
    )
    ingestion.parse_and_index(
        session,
        ctx,
        version_id=version.id,
        storage=storage,
        embedder=HashingEmbeddingProvider(),
        knowledge_gateway=gateway,
    )
    return version


def _types(session, ctx, version):
    return {
        e.evidence_type
        for e in session.execute(
            select(Evidence).where(
                Evidence.org_id == ctx.org_id,
                Evidence.source_document_version_id == version.id,
            )
        ).scalars()
    }


def test_an_untyped_upload_is_filed_and_its_excerpts_stamped(app_sessionmaker, org):
    reader = Filing()
    gateway = ModelGateway(reader, HashingEmbeddingProvider())
    session = app_sessionmaker()
    session.begin()
    ctx = ExecutionContext(org_id=org, actor_kind="SYSTEM")
    apply_tenant_context(session, ctx)
    try:
        version = _upload(
            session, ctx, gateway, InMemoryStorage(), data=FORM, filename="cv_form.md"
        )
        assert "document_classification" in reader.seen

        filed = session.execute(
            select(DocumentClassification).where(
                DocumentClassification.document_version_id == version.id
            )
        ).scalar_one()
        assert filed.document_type is DocumentType.STANDARD_FORM
        assert filed.classified_by.startswith("llm:document_classification@")

        types = _types(session, ctx, version)
        assert types == {DocumentType.STANDARD_FORM}
        assert not any(is_self_evidencing(t) for t in types), "a blank form proves nothing"
    finally:
        session.rollback()
        session.close()


def test_a_type_a_person_chose_is_never_overwritten(app_sessionmaker, org):
    reader = Filing()
    gateway = ModelGateway(reader, HashingEmbeddingProvider())
    session = app_sessionmaker()
    session.begin()
    ctx = ExecutionContext(org_id=org, actor_kind="SYSTEM")
    apply_tenant_context(session, ctx)
    try:
        version = _upload(
            session,
            ctx,
            gateway,
            InMemoryStorage(),
            data=FORM,
            filename="cv_form.md",
            document_type=DocumentType.CV,
        )
        filed = session.execute(
            select(DocumentClassification).where(
                DocumentClassification.document_version_id == version.id
            )
        ).scalar_one()
        assert filed.document_type is DocumentType.CV
        assert filed.classified_by.startswith("human:")
        assert _types(session, ctx, version) == {DocumentType.CV}
    finally:
        session.rollback()
        session.close()


def test_the_backfill_types_what_earlier_uploads_left_as_other(app_sessionmaker, org):
    """The live corpus: 610 excerpts typed OTHER because nothing ever filed them."""
    reader = Filing()
    gateway = ModelGateway(reader, HashingEmbeddingProvider())
    session = app_sessionmaker()
    session.begin()
    ctx = ExecutionContext(org_id=org, actor_kind="SYSTEM")
    apply_tenant_context(session, ctx)
    try:
        version = _upload(session, ctx, gateway, InMemoryStorage(), data=FORM, filename="old.md")
        # Put it back the way an upload before this feature left it: nobody
        # ever decided anything, so there is no classification row at all --
        # not a row that says OTHER, which is a decision and is respected.
        session.delete(
            session.execute(
                select(DocumentClassification).where(
                    DocumentClassification.document_version_id == version.id
                )
            ).scalar_one()
        )
        for evidence in session.execute(
            select(Evidence).where(Evidence.source_document_version_id == version.id)
        ).scalars():
            evidence.evidence_type = DocumentType.OTHER
        session.flush()

        report = document_classification.classify_unclassified(session, ctx, gateway=gateway)
        assert report["classified"] >= 1 and report["evidence_restamped"] >= 1
        assert _types(session, ctx, version) == {DocumentType.STANDARD_FORM}
    finally:
        session.rollback()
        session.close()


class Unplaceable(Reader):
    """A filing call that cannot place the document: an answer outside the taxonomy.

    `_type_for` turns anything it does not recognise into OTHER, which is the
    right answer -- an invented type must never enter the enum. What follows
    from it is the subject of the tests below.
    """

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        blob = "\n".join(m.content for m in messages)
        if '"title": "DocumentClassificationResult"' in blob:
            self.seen.append("document_classification")
            return LLMResponse(
                text=json.dumps(
                    {
                        "document_type": "MISCELLANEOUS_PAPERWORK",
                        "confidence": 0.2,
                        "reason": "Could not tell what this is.",
                    }
                ),
                model_id="scripted-filing",
                latency_ms=0,
            )
        return super().generate(messages, temperature=temperature, max_tokens=max_tokens)


def test_a_document_the_model_cannot_place_is_asked_about_once(app_sessionmaker, org):
    """The button has to finish.

    Pending was selected as "has no classification of a type other than OTHER",
    so a document the model could not place was picked up by every pass, re-read
    at cost, filed as OTHER again, and counted as freshly filed. On the live
    corpus four documents did this: File them reported "Filed 4; 54 excerpts now
    carry a type" however many times it was pressed, and neither number was
    true.
    """
    reader = Unplaceable()
    gateway = ModelGateway(reader, HashingEmbeddingProvider())
    session = app_sessionmaker()
    session.begin()
    ctx = ExecutionContext(org_id=org, actor_kind="SYSTEM")
    apply_tenant_context(session, ctx)
    try:
        version = _upload(session, ctx, gateway, InMemoryStorage(), data=FORM, filename="odd.md")
        filed = session.execute(
            select(DocumentClassification).where(
                DocumentClassification.document_version_id == version.id
            )
        ).scalar_one()
        assert filed.document_type is DocumentType.OTHER, "an invented type must not be accepted"

        reader.seen.clear()
        first = document_classification.classify_unclassified(session, ctx, gateway=gateway)
        second = document_classification.classify_unclassified(session, ctx, gateway=gateway)

        # The document was filed at upload by the CURRENT prompt, so neither
        # pass has a question to ask about it.
        assert "document_classification" not in reader.seen
        assert first["attempted"] == 0 and second["attempted"] == 0
        assert first["classified"] == 0 and second["classified"] == 0
    finally:
        session.rollback()
        session.close()


def test_a_pass_that_places_nothing_says_so_instead_of_claiming_work(app_sessionmaker, org):
    """"Filed" means the document came out with a real type, and stamping OTHER
    over OTHER is not 54 excerpts now carrying a type."""
    reader = Unplaceable()
    gateway = ModelGateway(reader, HashingEmbeddingProvider())
    session = app_sessionmaker()
    session.begin()
    ctx = ExecutionContext(org_id=org, actor_kind="SYSTEM")
    apply_tenant_context(session, ctx)
    try:
        version = _upload(session, ctx, gateway, InMemoryStorage(), data=FORM, filename="odd2.md")
        # Put it back the way a corpus uploaded before any filing existed looks:
        # excerpts typed OTHER, and no record that anything was ever decided.
        session.execute(
            select(DocumentClassification).where(
                DocumentClassification.document_version_id == version.id
            )
        ).scalar_one().classified_by = "llm:document_classification@0"
        session.flush()

        report = document_classification.classify_unclassified(session, ctx, gateway=gateway)

        assert report["attempted"] >= 1, "an older prompt's OTHER is a question worth re-asking"
        assert report["classified"] == 0
        assert report["left_untyped"] >= 1
        assert report["evidence_restamped"] == 0
        assert _types(session, ctx, version) == {DocumentType.OTHER}
    finally:
        session.rollback()
        session.close()
