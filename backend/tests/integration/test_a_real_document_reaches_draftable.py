"""From an uploaded company document to a DRAFTABLE requirement, in the suite.

## What was missing

Every assessment test before this one planted `Evidence` rows by hand, already
APPROVED and CURRENT_VERIFIED, and asked the arithmetic what it made of them.
That proves the counting. It says nothing about whether a document a company
actually uploads ever BECOMES that row -- and that is the half that broke on
the real corpus: 502 approved excerpts hanging off withdrawn files, a batch
approval that stranded rows APPROVED-but-PROPOSED, a fixture whose first run
approved nothing and blocked everything.

`scripts/seed_fixture_bid.py` walks the whole path, and it passes, but it is a
script: it needs the live model, somebody has to remember to run it, and a
regression in ingestion or approval would sit unnoticed until they did.

This is the same path, permanently in the suite:

    upload -> parse -> chunk -> embed -> knowledge_extraction
      -> knowledge administrator approves the batch
      -> requirement assessed -> relevance read -> readiness

Everything in that chain is production code. Only the MODEL is scripted.

## What the scripted model does, and why that is honest

The model reads; code counts. The two model calls on this path are replaced by
a reader that answers from the document text, so the test pins the part that
must never drift -- what the code does with a reading -- and not the reading
itself, which is the live model's job and is what the fixture script measures.

The reader is deliberately dumb in a checkable way: it calls an excerpt proof
only if it carries the identifier a human would look for (the scheme reference
on the accreditation, the certificate number on the ISO certificate). It never
sees approval, dates or readiness, which is the same restriction the real
prompt is under.

## The documents

`tests/fixtures/cybranytech/company/` -- a company whose documents ARE
evidence. The accreditation is current, the ISO certificate expired on
14 Mar 2025, and there is no mobile-testing methodology at all.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.gateway.base import LLMResponse
from app.gateway.deepseek import NullLLMProvider
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.ingestion.safety import EicarAwareScanner
from app.models import (
    Document,
    DocumentVersion,
    Evidence,
    Organization,
    Requirement,
    RequirementVersion,
)
from app.models.enums import (
    ApprovalStatus,
    BidReadiness,
    ConfidentialityClass,
    DocumentSide,
    DocumentType,
    EvidenceStatus,
    RequirementApplicability,
    RequirementCategory,
    RequirementLifecycle,
    RiskLevel,
)
from app.services import assessment, bids, ingestion, knowledge
from app.services.storage import InMemoryStorage

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cybranytech"
TODAY = date(2026, 3, 1)

COMPANY = (
    ("01_accreditation.md", DocumentType.ACCREDITATION),
    ("02_iso27001.md", DocumentType.CERTIFICATE),
    ("06_vapt_methodology.md", DocumentType.METHODOLOGY),
)
ISO_EXPIRY = date(2025, 3, 14)

# The tender's own words, clause for clause.
ACCREDITATION = (
    "2.1",
    "The supplier must hold a current penetration-testing accreditation from a "
    "recognised assurance scheme, and must provide the certificate.",
)
ISO = (
    "2.2",
    "The supplier must hold current ISO/IEC 27001 certification covering the "
    "delivery of the proposed services, and must provide the certificate.",
)
MOBILE = (
    "3.3",
    "The supplier must describe its mobile application testing methodology, "
    "including device coverage and the mobile security testing standard applied.",
)

# What a reader would look for in an excerpt before calling it proof.
PROOF_MARKS = {
    "penetration-testing accreditation": "RAC-PT-2024-0817",
    "ISO/IEC 27001": "IS-664120",
}


# --- the scripted reader ----------------------------------------------------


class Reader(NullLLMProvider):
    """Answers the two model calls on this path from the text it is shown.

    `knowledge_extraction`: names one entity per batch and cites every chunk,
    which is what makes the persistence service create an Evidence row per
    chunk. Nothing is invented -- the ordinals come from the payload.

    `evidence_relevance`: for each shortlisted excerpt, `proof_verdict` if it
    carries the identifier the requirement needs, UNRELATED otherwise; and, when
    something was proof, `coverage` as the side-by-side reading of the whole.
    """

    model_id = "scripted-reader"

    def __init__(self, proof_verdict: str = "SUPPORTS", coverage: str = "FULLY_COVERED") -> None:
        super().__init__()
        self.proof_verdict = proof_verdict
        self.coverage = coverage
        self.seen: list[str] = []

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        blob = "\n".join(m.content for m in messages)
        if '"title": "KnowledgeExtractionResult"' in blob:
            self.seen.append("knowledge_extraction")
            return self._reply(self._extract(blob))
        if '"title": "EvidenceRelevanceResult"' in blob:
            self.seen.append("evidence_relevance")
            return self._reply(self._judge(blob))
        self.seen.append("unknown")
        return self._reply({})

    def _reply(self, payload: dict) -> LLMResponse:
        return LLMResponse(text=json.dumps(payload), model_id=self.model_id, latency_ms=0)

    @staticmethod
    def _extract(blob: str) -> dict:
        ordinals = sorted({int(n) for n in re.findall(r'"ordinal":\s*(\d+)', blob)})
        filename = re.search(r'"filename":\s*"([^"]+)"', blob)
        return {
            "entities": [
                {
                    "ref": "e1",
                    "entity_type": "CERTIFICATION",
                    "canonical_name": filename.group(1) if filename else "document",
                    "source_chunk_ordinals": ordinals,
                    "confidence": 0.9,
                }
            ]
        }

    def _judge(self, blob: str) -> dict:
        requirement = blob.split("SHORTLIST", 1)[0]
        mark = next((m for phrase, m in PROOF_MARKS.items() if phrase in requirement), None)
        excerpts = re.split(r"\n\[(\d+)\]\n", blob.split("SHORTLIST", 1)[1])
        verdicts, proven = [], False
        for index, text in zip(excerpts[1::2], excerpts[2::2], strict=False):
            proof = mark is not None and mark in text
            proven |= proof
            verdicts.append(
                {
                    "candidate": int(index),
                    "verdict": self.proof_verdict if proof else "UNRELATED",
                    "reason": "carries the identifier" if proof else "says nothing about it",
                }
            )
        coverage = self.coverage if proven else "NOT_COVERED"
        gap = "" if coverage == "FULLY_COVERED" else "the certificate itself must be attached"
        return {
            "verdicts": verdicts,
            "fit": {"asked": "", "offered": "", "gap": gap, "coverage": coverage},
        }


class Outcome:
    def __init__(self, evidence_status, bid_readiness) -> None:
        self.evidence_status = evidence_status
        self.bid_readiness = bid_readiness

    def __repr__(self) -> str:
        return f"Outcome({self.evidence_status}, {self.bid_readiness})"


# --- the scene --------------------------------------------------------------


@pytest.fixture
def org(owner_engine):
    """A tenant of this test's own: retrieval must see only these documents."""
    org_id = uuid.uuid4()
    with sessionmaker(bind=owner_engine, future=True)() as s:
        s.add(Organization(id=org_id, name="Cybranytech", slug=f"cyb-{org_id.hex[:10]}"))
        s.commit()
    return org_id


def _upload_company_pack(session, ctx, gateway, storage) -> None:
    """Exactly what a company does: upload, and let ingestion run."""
    scanner, embedder = EicarAwareScanner(), HashingEmbeddingProvider()
    for name, kind in COMPANY:
        version = ingestion.register_upload(
            session,
            ctx,
            data=(FIXTURE / "company" / name).read_bytes(),
            filename=name,
            side=DocumentSide.COMPANY,
            confidentiality_class=ConfidentialityClass.INTERNAL,
            document_type=kind,
            scanner=scanner,
            storage=storage,
        )
        ingestion.parse_and_index(
            session,
            ctx,
            version_id=version.id,
            storage=storage,
            embedder=embedder,
            knowledge_gateway=gateway,
        )
        if name == "02_iso27001.md":
            # The expiry is a governed column; nothing reads a date out of
            # prose. Set the way `seed_fixture_bid.py` sets it.
            session.execute(
                update(Evidence)
                .where(
                    Evidence.org_id == ctx.org_id,
                    Evidence.source_document_version_id == version.id,
                )
                .values(expiry_date=ISO_EXPIRY)
            )
    session.flush()


def _tender_requirements(session, ctx, storage) -> dict[str, Requirement]:
    """A bid, its tender, and three of its clauses as confirmed requirements.

    Requirements are written rather than extracted: extraction is the tender
    half of the product and has its own suite. This test is about whether
    company documents can answer a clause, so the clauses are the tender's own
    words and nothing else.
    """
    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Emirate Digital Services Authority",
        tender_name=f"Penetration Testing {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    bids.ensure_intake_open(session, ctx, bid_id=bid.id)
    tender = ingestion.register_upload(
        session,
        ctx,
        data=(FIXTURE / "tender" / "RFP_Cybersecurity_Assessment.md").read_bytes(),
        filename="RFP_Cybersecurity_Assessment.md",
        side=DocumentSide.TENDER,
        bid_id=bid.id,
        confidentiality_class=ConfidentialityClass.INTERNAL,
        document_type=DocumentType.RFP,
        scanner=EicarAwareScanner(),
        storage=storage,
    )

    made: dict[str, Requirement] = {}
    for clause, text in (ACCREDITATION, ISO, MOBILE):
        requirement = Requirement(
            org_id=ctx.org_id,
            bid_id=bid.id,
            code=f"R-{clause}",
            category=RequirementCategory.SECURITY,
            is_mandatory=True,
            is_scored=False,
            is_conditional=False,
            applicability=RequirementApplicability.ACTIVE,
            lifecycle_state=RequirementLifecycle.CONFIRMED,
            risk=RiskLevel.HIGH,
        )
        session.add(requirement)
        session.flush()
        version = RequirementVersion(
            org_id=ctx.org_id,
            requirement_id=requirement.id,
            version_no=1,
            requirement_text=text,
            source_document_version_id=tender.id,
            source_locator={"clause_ref": clause},
            extracted_by="test",
        )
        session.add(version)
        session.flush()
        requirement.current_version_id = version.id
        made[clause] = requirement
    session.flush()
    return {"bid": bid, **made}


def _run(
    app_sessionmaker,
    org_id,
    monkeypatch,
    *,
    proof_verdict: str = "SUPPORTS",
    coverage: str = "FULLY_COVERED",
    approve: bool = True,
):
    """The whole path. Returns the reader and each clause's current assessment."""
    reader = Reader(proof_verdict, coverage)
    gateway = ModelGateway(reader, HashingEmbeddingProvider())
    # The relevance verifier builds its own gateway; route it to the reader.
    monkeypatch.setattr(assessment, "build_gateway", lambda: gateway)
    storage = InMemoryStorage()

    with app_sessionmaker() as session:
        session.begin()
        ctx = ExecutionContext(org_id=org_id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        _upload_company_pack(session, ctx, gateway, storage)
        if approve:
            knowledge.approve_evidence_batch(
                session,
                ctx,
                evidence_ids=None,
                reason="Company pack reviewed and approved for bid reuse.",
            )
        scene = _tender_requirements(session, ctx, storage)
        assessment.assess_bid(session, ctx, bid_id=scene["bid"].id, today=TODAY)

        results = {}
        for clause in ("2.1", "2.2", "3.3"):
            current = assessment.current_assessment(session, ctx, requirement_id=scene[clause].id)
            cited = assessment.evidence_for(session, ctx, assessment_id=current.id)
            sources = set(
                session.execute(
                    select(Document.original_filename)
                    .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                    .where(
                        DocumentVersion.id.in_(
                            [evidence.source_document_version_id for _, evidence in cited]
                        )
                    )
                ).scalars()
            )
            # Read before the rollback, which expires every loaded row.
            results[clause] = (
                Outcome(current.evidence_status, current.bid_readiness),
                sources,
            )
        session.rollback()
    return reader, results


# --- the path ---------------------------------------------------------------


def test_an_uploaded_accreditation_answers_the_clause_that_asks_for_it(
    app_sessionmaker, org, monkeypatch
):
    """The whole point: a document goes in, a requirement comes out answerable.

    And it is answerable because of THAT document, not because something
    attached itself -- the citation must lead back to the accreditation file.
    """
    reader, results = _run(app_sessionmaker, org, monkeypatch)
    accreditation, sources = results["2.1"]

    assert "knowledge_extraction" in reader.seen, "ingestion never asked what the file says"
    assert "evidence_relevance" in reader.seen, "assessment never read the shortlist"
    assert accreditation.evidence_status is EvidenceStatus.CURRENT_VERIFIED
    assert accreditation.bid_readiness is BidReadiness.READY
    assert "01_accreditation.md" in sources, (
        f"answerable, but cites {sorted(sources)} rather than the accreditation"
    )


def test_proof_with_a_named_gap_is_draftable(app_sessionmaker, org, monkeypatch):
    """DRAFTABLE is reached, and it means what it says.

    The accreditation IS proof, and the reader still names something the
    answer has to close -- here, that the certificate itself must go in. That
    is the state a writer can start from and a reviewer must finish: there is
    something real to write from, and "ready" would claim nothing is left.
    This is the mechanism the fixture script reached DRAFTABLE through.
    """
    _reader, results = _run(app_sessionmaker, org, monkeypatch, coverage="PARTIALLY_COVERED")
    accreditation, sources = results["2.1"]

    assert accreditation.evidence_status is EvidenceStatus.CURRENT_VERIFIED
    assert accreditation.bid_readiness is BidReadiness.DRAFTABLE
    assert "01_accreditation.md" in sources


def test_material_that_is_only_related_is_not_draftable(app_sessionmaker, org, monkeypatch):
    """The trap DRAFTABLE used to fall into, on a real upload.

    Same document, read as PARTIAL: related, not proof on its own. On the live
    bid 40 of 41 DRAFTABLE rows were exactly this, and invited a drafter to
    write a positive answer backed by material the system had itself said does
    not answer the requirement. The excerpts stay attached; the row does not
    pretend they are enough.

    Written first as "PARTIAL means DRAFTABLE", which is the old rule -- and
    the product refused it. The test was wrong, not the code.
    """
    _reader, results = _run(app_sessionmaker, org, monkeypatch, proof_verdict="PARTIAL")
    accreditation, _ = results["2.1"]

    assert accreditation.evidence_status is EvidenceStatus.PARTIAL
    assert accreditation.bid_readiness not in (BidReadiness.READY, BidReadiness.DRAFTABLE)


def test_an_expired_certificate_is_found_and_still_not_current(app_sessionmaker, org, monkeypatch):
    """Both axes, on a real upload.

    The certificate is found, read and judged to be proof -- and its date says
    it lapsed, so it asks for renewal rather than pretending to be current or
    vanishing as though it were never there.

    Verified by removal, and the result is worth knowing: disabling the date
    check alone changes nothing, and neither does also disabling the EXPIRED
    status check -- approval files a lapsed row as HISTORICAL_VERIFIED, which
    also demotes to NEEDS_REFRESH. Only with all three removed does this fail.
    Three guards on one fact, each sufficient alone.
    """
    _reader, results = _run(app_sessionmaker, org, monkeypatch)
    iso, _ = results["2.2"]

    assert iso.bid_readiness is BidReadiness.NEEDS_REFRESH
    assert iso.evidence_status is not EvidenceStatus.CURRENT_VERIFIED


def test_a_clause_the_pack_cannot_answer_stays_blocked(app_sessionmaker, org, monkeypatch):
    """The false positive the product exists to prevent.

    There is a penetration-testing METHODOLOGY in the pack, and it is exactly
    the sort of neighbour retrieval ranks highly for a mobile-methodology
    clause. It is not a mobile methodology, and the row must not go answerable.
    """
    _reader, results = _run(app_sessionmaker, org, monkeypatch)
    mobile, _ = results["3.3"]

    assert mobile.bid_readiness is BidReadiness.BLOCKED


def test_without_the_administrators_approval_nothing_is_answerable(
    app_sessionmaker, org, monkeypatch
):
    """The approval step is load-bearing, and this is the proof.

    Same documents, same reader, same clauses -- the only thing missing is the
    knowledge administrator. Uploaded material is true and still unusable until
    a person vouches for it, and the first run of the fixture script found out
    the hard way that skipping this step blocks everything.
    """
    _reader, results = _run(app_sessionmaker, org, monkeypatch, approve=False)

    for clause in ("2.1", "2.2", "3.3"):
        current, _ = results[clause]
        assert current.bid_readiness is BidReadiness.BLOCKED, clause
        assert current.bid_readiness not in (BidReadiness.READY, BidReadiness.DRAFTABLE)


def test_nothing_the_pack_contains_was_left_unapproved_by_accident(
    app_sessionmaker, org, monkeypatch
):
    """The batch approval reached every excerpt except the lapsed ones.

    Guards the failure that stranded a corpus once: rows APPROVED but still
    PROPOSED, inadmissible, and invisible to a second attempt.
    """
    reader = Reader()
    gateway = ModelGateway(reader, HashingEmbeddingProvider())
    with app_sessionmaker() as session:
        session.begin()
        ctx = ExecutionContext(org_id=org, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)
        _upload_company_pack(session, ctx, gateway, InMemoryStorage())
        knowledge.approve_evidence_batch(session, ctx, evidence_ids=None, reason="Reviewed.")

        rows = session.execute(
            select(Evidence.approval_status, Evidence.expiry_date).where(Evidence.org_id == org)
        ).all()
        assert rows, "ingestion produced no evidence at all"
        unexpired = [status for status, expiry in rows if expiry is None or expiry >= TODAY]
        assert unexpired
        assert all(status is ApprovalStatus.APPROVED for status in unexpired)
        session.rollback()
