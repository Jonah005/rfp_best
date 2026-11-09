"""Phase G against a real database: authority boundary, precedence, extraction.

The model is replaced with a scripted provider so the assertions are about the
deterministic machinery — what is eligible for extraction, what precedence
decides, what the coverage check catches — rather than about model quality.
"""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.core.errors import IllegalTransition, ValidationError
from app.db.session import apply_tenant_context
from app.gateway.base import LLMResponse
from app.gateway.deepseek import NullLLMProvider
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.ingestion.safety import EicarAwareScanner
from app.models import (
    AuditEvent,
    ClarificationIssue,
    ClauseResolution,
    Document,
    DocumentClassification,
    DocumentRelation,
    Requirement,
    RequirementVersion,
)
from app.models.enums import (
    BidState,
    ClarificationIssueType,
    ClarificationSeverity,
    ClarificationStatus,
    ConfidentialityClass,
    DocumentRelationType,
    DocumentSide,
    DocumentType,
    RequirementApplicability,
    RequirementLifecycle,
)
from app.services import bids, ingestion, precedence, requirements, tender_analysis
from app.services.storage import InMemoryStorage

pytestmark = pytest.mark.integration


# --- a scripted model -------------------------------------------------------


class ScriptedProvider(NullLLMProvider):
    """Answers each call type from a fixture keyed by call type name.

    The gateway appends a schema instruction naming the response model, which is
    what the router below keys on.
    """

    model_id = "scripted-tender"

    def __init__(self, responses: dict[str, object]) -> None:
        super().__init__()
        self.responses = responses
        self.seen: list[str] = []
        self.prompts: list[str] = []

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        blob = "\n".join(m.content for m in messages)
        self.prompts.append(blob)
        for name, model_name in (
            ("precedence_hint_extraction", "PrecedenceHintResult"),
            ("tender_fact_extraction", "TenderFactsResult"),
            ("requirement_decomposition", "RequirementDecompositionResult"),
            ("condition_extraction", "ConditionExtractionResult"),
            ("ambiguity_analysis", "AmbiguityAnalysisResult"),
            ("deliverable_extraction", "DeliverableExtractionResult"),
        ):
            if f'"title": "{model_name}"' in blob or f"'{model_name}'" in blob:
                self.seen.append(name)
                payload = self.responses.get(name, {})
                resolved = payload(blob) if callable(payload) else payload
                return LLMResponse(text=json.dumps(resolved), model_id=self.model_id, latency_ms=0)
        self.seen.append("unknown")
        return LLMResponse(text="{}", model_id=self.model_id, latency_ms=0)


def gateway_for(responses: dict[str, object]) -> ModelGateway:
    return ModelGateway(ScriptedProvider(responses), HashingEmbeddingProvider())


def ordinals_in(prompt: str) -> list[int]:
    """The block ordinals a prompt actually carried."""
    found: list[int] = []
    for chunk in prompt.split('"ordinal":')[1:]:
        digits = chunk.strip().split(",")[0].strip().strip("}").strip()
        if digits.isdigit():
            found.append(int(digits))
    return found


# --- fixtures ---------------------------------------------------------------

RFP_TEXT = b"""# 4.2 Testing scope

The bidder shall perform penetration testing of 25 applications.

# 6.1 Testing window

Testing must occur between 01:00 and 05:00 Gulf Standard Time.

# 9 Subcontracting

If subcontractors are used, the bidder shall provide a subcontractor profile.
"""

AMENDMENT_TEXT = b"""# 4.2 Testing scope

The bidder shall perform penetration testing of 35 applications.
"""

CLARIFICATION_TEXT = b"""# 6.1 Testing window

Testing may occur at any time outside published business hours.
"""

COMPANY_TEXT = b"""# Our standard methodology

We test 12 applications in a typical engagement and always require VPN access.
"""


def _for_clause(resolutions, bare: str):
    """The resolution for the clause a person would call `bare`.

    Resolutions are keyed by the QUALIFIED reference -- "4.2 Testing scope /
    4.2" -- because tenders restart numbering in every section, and a bare "3"
    once gathered three unrelated obligations into one contest. These tests used
    to compare against the bare string and stopped matching anything the day
    that changed, then sat red long enough to be mistaken for a flaky stub.

    So the lookup is by intent -- "the resolution for clause 4.2" -- rather than
    by the format of the key, which these tests do not exist to pin down. It is
    strict about finding exactly one, because matching two would make an
    assertion about whichever came first.
    """
    found = [r for r in resolutions if r.clause_ref == bare or r.clause_ref.endswith(f"/ {bare}")]
    assert found, f"no resolution for clause {bare}; saw {[r.clause_ref for r in resolutions]}"
    assert len(found) == 1, f"clause {bare} is ambiguous here: {[r.clause_ref for r in found]}"
    return found[0]


@pytest.fixture
def env(seeded_orgs):
    return seeded_orgs[0], InMemoryStorage(), EicarAwareScanner(), HashingEmbeddingProvider()


def _upload(
    session,
    ctx,
    env,
    *,
    data,
    filename,
    bid_id,
    doc_type=None,
    issue_date=None,
    version_label=None,
    side=DocumentSide.TENDER,
):
    _org, storage, scanner, embedder = env
    version = ingestion.register_upload(
        session,
        ctx,
        data=data,
        filename=filename,
        side=side,
        bid_id=bid_id if side is DocumentSide.TENDER else None,
        confidentiality_class=ConfidentialityClass.CONFIDENTIAL,
        scanner=scanner,
        storage=storage,
    )
    if issue_date is not None:
        version.issue_date = issue_date
    if version_label is not None:
        version.version_label = version_label
    session.flush()
    ingestion.parse_and_index(
        session,
        ctx,
        version_id=version.id,
        storage=storage,
        embedder=embedder,
        # COMPANY uploads auto-extract knowledge; these tests are about the
        # tender side, so the company path gets a model that proposes nothing.
        knowledge_gateway=gateway_for(
            {"knowledge_extraction": {"entities": [], "facts": [], "relations": []}}
        ),
    )
    if doc_type is not None:
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
    return version


def _new_bid(session, ctx, name="ADNOC VAPT"):
    opportunity = bids.create_opportunity(
        session, ctx, customer_name="ADNOC", tender_name=f"{name} {uuid.uuid4().hex[:4]}"
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    bids.ensure_intake_open(session, ctx, bid_id=bid.id)
    return bid


# --- the authority boundary -------------------------------------------------


def test_a_company_document_cannot_be_attached_to_a_bid(app_sessionmaker, env):
    org, storage, scanner, _ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        with pytest.raises(ValidationError, match="organization-wide"):
            ingestion.register_upload(
                s,
                ctx,
                data=COMPANY_TEXT,
                filename="method.md",
                side=DocumentSide.COMPANY,
                bid_id=bid.id,
                confidentiality_class=ConfidentialityClass.INTERNAL,
                scanner=scanner,
                storage=storage,
            )
        s.rollback()


def test_company_content_is_never_eligible_for_requirement_extraction(app_sessionmaker, env):
    """Risk 1 from the original evaluation, prevented structurally.

    A company methodology saying "we test 12 applications" sits in the same
    organization as the tender. It must contribute nothing: the eligible-block
    query is tender-side only.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        # A company document in the same tenant, deliberately contradicting it.
        _upload(
            s,
            ctx,
            env,
            data=COMPANY_TEXT,
            filename="method.md",
            bid_id=None,
            side=DocumentSide.COMPANY,
        )

        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        eligible = requirements.eligible_blocks(s, ctx, bid_id=bid.id)

        company_versions = {
            d.current_version_id
            for d in s.execute(
                select(Document).where(
                    Document.org_id == org, Document.side == DocumentSide.COMPANY
                )
            )
            .scalars()
            .all()
        }
        assert eligible, "the tender should have produced eligible clauses"
        assert all(block.document_version_id not in company_versions for block, _ in eligible), (
            "a company document became eligible for requirement extraction"
        )
        s.commit()


def test_tender_prompts_never_carry_company_text(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        _upload(
            s,
            ctx,
            env,
            data=COMPANY_TEXT,
            filename="method.md",
            bid_id=None,
            side=DocumentSide.COMPANY,
        )

        gateway = gateway_for(
            {"precedence_hint_extraction": {"hints": [], "affected_clause_refs": []}}
        )
        tender_analysis.extract_precedence_hints(s, ctx, bid_id=bid.id, gateway=gateway)
        joined = "\n".join(gateway.llm.prompts)
        assert "standard methodology" not in joined
        assert "VPN access" not in joined
        s.commit()


# --- precedence over real documents ----------------------------------------


def test_a_clarification_and_an_amendment_each_win_their_own_clause(app_sessionmaker, env):
    """The case the hardcoded ordering gets wrong, end to end."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        rfp = _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        amendment = _upload(
            s,
            ctx,
            env,
            data=AMENDMENT_TEXT,
            filename="amendment.md",
            bid_id=bid.id,
            doc_type=DocumentType.AMENDMENT,
            issue_date=date(2026, 8, 11),
        )
        clarification = _upload(
            s,
            ctx,
            env,
            data=CLARIFICATION_TEXT,
            filename="clarification.md",
            bid_id=bid.id,
            doc_type=DocumentType.CLARIFICATION,
            issue_date=date(2026, 8, 19),
        )

        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        resolutions = precedence.current_resolutions(s, ctx, bid_id=bid.id)
        assert _for_clause(resolutions, "4.2").winning_document_version_id == amendment.id, (
            "the amendment governs the scope clause"
        )
        assert _for_clause(resolutions, "6.1").winning_document_version_id == clarification.id, (
            "the later clarification governs the testing-window clause"
        )
        assert _for_clause(resolutions, "9").winning_document_version_id == rfp.id, (
            "an untouched clause stays with the original"
        )
        s.commit()


def test_explicit_supersession_overrides_dates(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        rfp = _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 9, 1),
        )
        amendment = _upload(
            s,
            ctx,
            env,
            data=AMENDMENT_TEXT,
            filename="amendment.md",
            bid_id=bid.id,
            doc_type=DocumentType.AMENDMENT,
            issue_date=date(2026, 8, 11),
        )
        s.add(
            DocumentRelation(
                org_id=org,
                from_version_id=amendment.id,
                to_version_id=rfp.id,
                relation_type=DocumentRelationType.REPLACES_CLAUSE,
                clause_refs=["4.2"],
                detected_by="test",
                confidence=1.0,
            )
        )
        s.flush()

        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        resolution = _for_clause(precedence.current_resolutions(s, ctx, bid_id=bid.id), "4.2")
        assert resolution.winning_document_version_id == amendment.id
        assert resolution.method.value == "EXPLICIT_SUPERSESSION"
        s.commit()


def test_an_unresolvable_clause_raises_a_blocking_clarification(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        # Two amendments, same type, no dates, no labels: nothing discriminates.
        _upload(
            s,
            ctx,
            env,
            data=AMENDMENT_TEXT,
            filename="a1.md",
            bid_id=bid.id,
            doc_type=DocumentType.AMENDMENT,
        )
        _upload(
            s,
            ctx,
            env,
            data=AMENDMENT_TEXT,
            filename="a2.md",
            bid_id=bid.id,
            doc_type=DocumentType.AMENDMENT,
        )

        report = precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        assert report.unresolved >= 1

        resolution = _for_clause(precedence.current_resolutions(s, ctx, bid_id=bid.id), "4.2")
        assert resolution.winning_document_version_id is None

        issue = (
            s.execute(
                select(ClarificationIssue).where(
                    ClarificationIssue.bid_id == bid.id,
                    ClarificationIssue.issue_type == ClarificationIssueType.PRECEDENCE_UNRESOLVED,
                )
            )
            .scalars()
            .first()
        )
        assert issue is not None
        assert issue.blocks_generation
        assert issue.source_a and issue.source_b, "both sides must be cited"
        s.commit()


def test_a_human_override_supersedes_and_is_audited(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        rfp = _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        _upload(
            s,
            ctx,
            env,
            data=AMENDMENT_TEXT,
            filename="amendment.md",
            bid_id=bid.id,
            doc_type=DocumentType.AMENDMENT,
            issue_date=date(2026, 8, 11),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)

        overridden = precedence.override(
            s,
            ctx,
            bid_id=bid.id,
            clause_ref="4.2",
            winning_document_version_id=rfp.id,
            reason="Client confirmed by email that the original scope stands.",
        )
        assert overridden.method.value == "MANUAL"
        assert overridden.is_current

        current = [
            r
            for r in s.execute(
                select(ClauseResolution).where(
                    ClauseResolution.bid_id == bid.id,
                    ClauseResolution.clause_ref == overridden.clause_ref,
                )
            )
            .scalars()
            .all()
        ]
        assert sum(1 for r in current if r.is_current) == 1, "only one current per clause"
        assert len(current) >= 2, "the prior decision must be retained"

        event = (
            s.execute(select(AuditEvent).where(AuditEvent.action == "precedence.overridden"))
            .scalars()
            .first()
        )
        assert event is not None and event.reason
        s.commit()


def test_an_override_must_name_a_version_that_contains_the_clause(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        clarification = _upload(
            s,
            ctx,
            env,
            data=CLARIFICATION_TEXT,
            filename="clar.md",
            bid_id=bid.id,
            doc_type=DocumentType.CLARIFICATION,
            issue_date=date(2026, 8, 19),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        with pytest.raises(ValidationError, match="does not contain clause"):
            precedence.override(
                s,
                ctx,
                bid_id=bid.id,
                clause_ref="9",
                winning_document_version_id=clarification.id,
                reason="wrong document",
            )
        s.rollback()


# --- requirement extraction -------------------------------------------------


def _decomposition(prompt: str) -> dict:
    """Turn every block in the prompt into one requirement.

    Reads the ordinals the gateway actually sent, so the fixture can never cite
    a block outside its own batch.
    """
    return {
        "requirements": [
            {
                "requirement_text": f"Requirement derived from block {o}.",
                "normalized_requirement": f"req-{o}",
                "category": "TECHNICAL",
                "is_mandatory": True,
                "is_scored": False,
                "risk": "MEDIUM",
                "source_block_ordinal": o,
                "confidence": 0.9,
            }
            for o in ordinals_in(prompt)
        ],
        "no_requirement_block_ordinals": [],
    }


def test_an_earlier_prompts_reading_is_retired_on_a_run_that_reads_nothing(
    app_sessionmaker, env
):
    """R-044, reproduced: the one path where the retirement could never run.

    A prompt bump re-decomposes every block on ONE run. From the next run
    onwards `extract_requirements` returns early -- everything is already
    decomposed by the current version -- and that early return sat before the
    reconciliation passes. So the live bid kept one v10 requirement beside the
    v11 reading of the same clause through three successive analyses, and no
    amount of re-running could clear it.

    Here the register is put in exactly that state: a requirement stamped with
    an older version of the same prompt, on a clause the current version has
    also read. The second call does no reading at all -- and must still retire
    it.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        gateway = gateway_for({"requirement_decomposition": _decomposition})
        first = requirements.extract_requirements(s, ctx, bid_id=bid.id, gateway=gateway)
        assert first.requirements_created > 0

        # A clause that has a reading by the CURRENT prompt, plus a second
        # requirement on it left by an earlier one. Cloning the live row is how
        # the two come to share a clause ref without sharing wording.
        live = [
            (r, v)
            for r, v in s.execute(
                select(Requirement, RequirementVersion)
                .join(RequirementVersion, RequirementVersion.id == Requirement.current_version_id)
                .where(Requirement.bid_id == bid.id, RequirementVersion.clause_ref.is_not(None))
            ).all()
        ]
        assert live, "the fixture must produce at least one clause-referenced requirement"
        current, current_version = live[0]

        stale = Requirement(
            org_id=org,
            bid_id=bid.id,
            code="R-STALE",
            category=current.category,
            obligation_owner=current.obligation_owner,
            is_mandatory=True,
            is_scored=False,
            applicability=RequirementApplicability.ACTIVE,
            risk=current.risk,
            lifecycle_state=RequirementLifecycle.DRAFT_EXTRACTED,
        )
        s.add(stale)
        s.flush()
        stale_version = RequirementVersion(
            org_id=org,
            requirement_id=stale.id,
            version_no=1,
            requirement_text="An earlier prompt's shorter reading of the same clause.",
            normalized_requirement="stale",
            source_document_version_id=current_version.source_document_version_id,
            source_locator=current_version.source_locator,
            clause_ref=current_version.clause_ref,
            extracted_by="llm:requirement_decomposition@v0",
        )
        s.add(stale_version)
        s.flush()
        stale.current_version_id = stale_version.id
        s.flush()

        # Nothing left to read: every block was decomposed by the first call.
        second = requirements.extract_requirements(s, ctx, bid_id=bid.id, gateway=gateway)
        assert second.requirements_created == 0
        assert second.blocks_already_decomposed > 0
        assert second.stale_readings_retired == 1

        s.refresh(stale)
        assert stale.lifecycle_state is RequirementLifecycle.WITHDRAWN
        assert stale.superseded_by_requirement_id == current.id
        # And a third run finds nothing more to do.
        assert requirements.extract_requirements(
            s, ctx, bid_id=bid.id, gateway=gateway
        ).stale_readings_retired == 0


def test_requirements_are_extracted_only_from_winning_clauses(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        amendment = _upload(
            s,
            ctx,
            env,
            data=AMENDMENT_TEXT,
            filename="amendment.md",
            bid_id=bid.id,
            doc_type=DocumentType.AMENDMENT,
            issue_date=date(2026, 8, 11),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)

        report = requirements.extract_requirements(
            s,
            ctx,
            bid_id=bid.id,
            gateway=gateway_for({"requirement_decomposition": _decomposition}),
        )
        assert report.requirements_created > 0

        versions = (
            s.execute(select(RequirementVersion).where(RequirementVersion.org_id == org))
            .scalars()
            .all()
        )
        # Qualified reference, same as everywhere else -- see `_for_clause`.
        scope_versions = [
            v for v in versions if v.clause_ref == "4.2" or (v.clause_ref or "").endswith("/ 4.2")
        ]
        assert scope_versions, "clause 4.2 should have produced a requirement"
        assert all(v.source_document_version_id == amendment.id for v in scope_versions), (
            "a superseded clause produced a requirement"
        )
        s.commit()


def test_the_coverage_check_catches_a_block_the_model_ignored(app_sessionmaker, env):
    """Silence about a clause must become a counted gap, not a quiet omission."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)

        def only_first(prompt: str) -> dict:
            ordinals = ordinals_in(prompt)
            return {
                "requirements": [
                    {
                        "requirement_text": "Only one obligation reported.",
                        "normalized_requirement": "only-one",
                        "category": "TECHNICAL",
                        "is_mandatory": True,
                        "risk": "MEDIUM",
                        "source_block_ordinal": ordinals[0],
                        "confidence": 0.9,
                    }
                ],
                "no_requirement_block_ordinals": [],
            }

        report = requirements.extract_requirements(
            s,
            ctx,
            bid_id=bid.id,
            gateway=gateway_for({"requirement_decomposition": only_first}),
        )
        assert report.coverage_gaps, "unreported blocks must be flagged"
        assert report.coverage_ratio < 1.0

        gaps = (
            s.execute(
                select(ClarificationIssue).where(
                    ClarificationIssue.bid_id == bid.id,
                    ClarificationIssue.issue_type == ClarificationIssueType.COVERAGE_GAP,
                )
            )
            .scalars()
            .all()
        )
        assert gaps, "each uncovered clause must raise a clarification"
        s.commit()


def test_an_explicit_no_requirement_decision_counts_as_covered(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)

        def nothing_here(prompt: str) -> dict:
            return {
                "requirements": [],
                "no_requirement_block_ordinals": ordinals_in(prompt),
            }

        report = requirements.extract_requirements(
            s,
            ctx,
            bid_id=bid.id,
            gateway=gateway_for({"requirement_decomposition": nothing_here}),
        )
        assert report.coverage_gaps == []
        assert report.coverage_ratio == 1.0
        assert report.requirements_created == 0
        s.commit()


def test_out_of_batch_ordinals_are_rejected_and_nothing_is_written(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)

        rogue = {
            "requirements": [
                {
                    "requirement_text": "Invented from a block never supplied.",
                    "category": "TECHNICAL",
                    "is_mandatory": True,
                    "risk": "MEDIUM",
                    "source_block_ordinal": 9999,
                    "confidence": 0.99,
                }
            ],
            "no_requirement_block_ordinals": [],
        }
        with pytest.raises(ValidationError, match="outside its input batch"):
            requirements.extract_requirements(
                s,
                ctx,
                bid_id=bid.id,
                gateway=gateway_for({"requirement_decomposition": rogue}),
            )
        s.rollback()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org))
        assert (
            s.execute(select(Requirement).where(Requirement.bid_id == bid.id)).scalars().all() == []
        )
        s.commit()


def test_an_amendment_adds_a_version_and_keeps_the_requirement_identity(app_sessionmaker, env):
    """Wording changes; identity, code and downstream references do not."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)

        def scope_requirement(text: str):
            def _factory(prompt: str) -> dict:
                ordinals = ordinals_in(prompt)
                return {
                    "requirements": [
                        {
                            "requirement_text": text,
                            "normalized_requirement": "penetration-testing-scope",
                            "category": "TECHNICAL",
                            "is_mandatory": True,
                            "risk": "HIGH",
                            "source_block_ordinal": ordinals[0],
                            "confidence": 0.95,
                        }
                    ],
                    "no_requirement_block_ordinals": ordinals[1:],
                }

            return _factory

        requirements.extract_requirements(
            s,
            ctx,
            bid_id=bid.id,
            gateway=gateway_for(
                {"requirement_decomposition": scope_requirement("Test 25 applications.")}
            ),
        )
        requirements.confirm_requirements(s, ctx, bid_id=bid.id)
        original = (
            s.execute(select(Requirement).where(Requirement.bid_id == bid.id)).scalars().one()
        )
        original_id, original_code = original.id, original.code

        # The amendment lands and rewrites that clause.
        _upload(
            s,
            ctx,
            env,
            data=AMENDMENT_TEXT,
            filename="amendment.md",
            bid_id=bid.id,
            doc_type=DocumentType.AMENDMENT,
            issue_date=date(2026, 8, 11),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)
        requirements.extract_requirements(
            s,
            ctx,
            bid_id=bid.id,
            gateway=gateway_for(
                {"requirement_decomposition": scope_requirement("Test 35 applications.")}
            ),
        )

        s.refresh(original)
        assert original.id == original_id
        assert original.code == original_code
        assert original.lifecycle_state is RequirementLifecycle.CONFIRMED, (
            "a wording amendment must not supersede the requirement identity"
        )
        assert original.superseded_by_requirement_id is None

        versions = (
            s.execute(
                select(RequirementVersion)
                .where(RequirementVersion.requirement_id == original_id)
                .order_by(RequirementVersion.version_no)
            )
            .scalars()
            .all()
        )
        assert [v.version_no for v in versions] == [1, 2]
        assert versions[0].superseded_at is not None
        assert versions[1].superseded_at is None
        assert "35" in versions[1].requirement_text
        assert original.current_version_id == versions[1].id
        s.commit()


# --- conditional requirements ----------------------------------------------


def test_a_false_condition_marks_a_requirement_not_applicable(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)

        def conditional(prompt: str) -> dict:
            ordinals = ordinals_in(prompt)
            return {
                "requirements": [
                    {
                        "requirement_text": "Provide a subcontractor profile.",
                        "normalized_requirement": "subcontractor-profile",
                        "category": "LEGAL",
                        "is_mandatory": True,
                        "risk": "MEDIUM",
                        "is_conditional": True,
                        "condition_text": "If subcontractors are used",
                        "source_block_ordinal": ordinals[0],
                        "confidence": 0.9,
                    }
                ],
                "no_requirement_block_ordinals": ordinals[1:],
            }

        gateway = gateway_for(
            {
                "requirement_decomposition": conditional,
                "condition_extraction": {
                    "expression": {
                        "variable": "subcontractor_used",
                        "operator": "EQUALS",
                        "value": True,
                        "description": "Subcontractors are used on this bid",
                    },
                    "unparseable_reason": None,
                },
            }
        )
        requirements.extract_requirements(s, ctx, bid_id=bid.id, gateway=gateway)

        pending = s.execute(select(Requirement).where(Requirement.bid_id == bid.id)).scalars().one()
        assert pending.applicability is RequirementApplicability.CONDITIONAL_PENDING

        counts = requirements.evaluate_conditions(
            s, ctx, bid_id=bid.id, variables={"subcontractor_used": False}, gateway=gateway
        )
        assert counts["not_applicable"] == 1
        s.refresh(pending)
        assert pending.applicability is RequirementApplicability.NOT_APPLICABLE
        assert pending.applicability_reason

        # And it is excluded from the active set rather than counted as a gap.
        active = requirements.list_requirements(
            s, ctx, bid_id=bid.id, applicability=RequirementApplicability.ACTIVE
        )
        assert pending.id not in {r.id for r in active}
        s.commit()


def test_an_unknown_variable_leaves_the_condition_pending(app_sessionmaker, env):
    """Never guessed into ACTIVE (inventing work) or NOT_APPLICABLE (hiding it)."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        precedence.resolve_bid_precedence(s, ctx, bid_id=bid.id)

        def conditional(prompt: str) -> dict:
            ordinals = ordinals_in(prompt)
            return {
                "requirements": [
                    {
                        "requirement_text": "Provide a subcontractor profile.",
                        "normalized_requirement": "subcontractor-profile",
                        "category": "LEGAL",
                        "is_mandatory": True,
                        "risk": "MEDIUM",
                        "is_conditional": True,
                        "condition_text": "If subcontractors are used",
                        "source_block_ordinal": ordinals[0],
                        "confidence": 0.9,
                    }
                ],
                "no_requirement_block_ordinals": ordinals[1:],
            }

        gateway = gateway_for(
            {
                "requirement_decomposition": conditional,
                "condition_extraction": {
                    "expression": {
                        "variable": "subcontractor_used",
                        "operator": "EQUALS",
                        "value": True,
                        "description": "Subcontractors are used",
                    },
                    "unparseable_reason": None,
                },
            }
        )
        requirements.extract_requirements(s, ctx, bid_id=bid.id, gateway=gateway)
        counts = requirements.evaluate_conditions(
            s, ctx, bid_id=bid.id, variables={}, gateway=gateway
        )
        assert counts["still_pending"] == 1
        assert counts["activated"] == 0 and counts["not_applicable"] == 0
        s.commit()


# --- ambiguity, scope confidence, and the whole pass ------------------------


def _full_gateway(prompt_aware_decomposition=None, deliverables=None):
    return gateway_for(
        {
            "knowledge_extraction": {"entities": [], "facts": [], "relations": []},
            "precedence_hint_extraction": {"hints": [], "affected_clause_refs": []},
            "tender_fact_extraction": lambda prompt: {
                "customer_name": "ADNOC",
                "tender_title": "Penetration Testing Services 2026",
                "tender_reference": "TND-2026-114",
                "submission_deadline": "2026-10-12T14:00:00+04:00",
                "contacts": [],
                "evaluation_criteria": [
                    {
                        "name": "Technical",
                        "weight": 70,
                        "weight_unit": "PERCENT",
                        "source_block_ordinal": ordinals_in(prompt)[0],
                    }
                ],
                "submission_rules": [
                    {
                        "rule_type": "RECIPIENT",
                        "detail": "Technical and financial go to separate recipients.",
                        "source_block_ordinal": ordinals_in(prompt)[0],
                    }
                ],
                "currency": "AED",
                "confidence": 0.9,
            },
            "requirement_decomposition": prompt_aware_decomposition or _decomposition,
            "deliverable_extraction": deliverables
            or (
                lambda prompt: {
                    "deliverables": [
                        {
                            "name": "Technical Proposal",
                            "deliverable_type": "PROPOSAL",
                            "required_format": "DOCX",
                            "is_mandatory": True,
                            "source_block_ordinal": ordinals_in(prompt)[0],
                            "confidence": 0.9,
                        }
                    ]
                }
            ),
            "ambiguity_analysis": {"issues": []},
        }
    )


def test_the_same_deliverable_named_twice_does_not_kill_the_analysis(app_sessionmaker, env):
    """A real DeepSeek run returned "Technical Proposal" from two clauses.

    The dedup check queried the database for an existing fingerprint, but the
    session runs with autoflush off, so rows added earlier in the same run were
    not visible to it. Both copies were added and the batched INSERT died on
    uq_deliverable_fingerprint — taking down the whole analysis and losing all
    sixty deliverables the model had correctly extracted, with the caller
    getting a 500.

    The duplicate here differs in case and surrounding whitespace, because
    that is what the fingerprint is supposed to normalize away.
    """
    org, *_ = env

    def _twice(prompt):
        """The same deliverable, differing only in case and whitespace."""
        entry = {
            "deliverable_type": "PROPOSAL",
            "required_format": "DOCX",
            "is_mandatory": True,
            "source_block_ordinal": ordinals_in(prompt)[0],
            "confidence": 0.9,
        }
        return {
            "deliverables": [
                {"name": "Technical Proposal", **entry},
                {"name": "  technical proposal  ", **entry},
            ]
        }

    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )

        result = tender_analysis.analyze_bid(
            s, ctx, bid_id=bid.id, gateway=_full_gateway(deliverables=_twice)
        )
        # One row, and — the point — no IntegrityError taking the run with it.
        assert result.deliverables == 1


def test_a_row_left_over_from_the_previous_key_scheme_does_not_kill_the_analysis(
    app_sessionmaker, env
):
    """The same failure, one scheme change later, and it got past unit tests.

    The deliverable key was changed to fold plurals and articles. Stored rows
    kept the OLD key, so the next run matched nothing and wrote a second copy
    of every deliverable. The fix -- recognise the old key and re-stamp it --
    then hit the case where BOTH keys were present, because last run had
    already written the twin: two rows, one key, and analyze_bid returned 500
    from uq_deliverable_fingerprint before ambiguity or anything after it ran.

    Here the bid already holds both: one row under the old key, one under the
    new. The run must survive, keep the new-key row, and fold the other into
    it rather than re-stamping it.
    """
    from app.models import Deliverable
    from app.models.enums import DeliverableStatus
    from app.services.tender_analysis import _deliverable_key, _fingerprint

    org, *_ = env
    name = "Signed proposal by authorized representative"

    def _one(prompt):
        return {
            "deliverables": [
                {
                    "name": name,
                    "deliverable_type": "PROPOSAL",
                    "required_format": "DOCX",
                    "is_mandatory": True,
                    "source_block_ordinal": ordinals_in(prompt)[0],
                    "confidence": 0.9,
                }
            ]
        }

    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        for key, stored in (
            (_fingerprint("deliverable", name.strip().casefold(), None), name),
            (_fingerprint("deliverable", _deliverable_key(name), None), name + "s"),
        ):
            s.add(
                Deliverable(
                    org_id=org,
                    bid_id=bid.id,
                    name=stored,
                    deliverable_type="PROPOSAL",
                    is_mandatory=True,
                    status=DeliverableStatus.IDENTIFIED,
                    extracted_by="llm:deliverable_extraction@v1",
                    fingerprint=key,
                )
            )
        s.flush()

        # The point: this completes at all.
        tender_analysis.analyze_bid(s, ctx, bid_id=bid.id, gateway=_full_gateway(deliverables=_one))

        rows = list(
            s.execute(select(Deliverable).where(Deliverable.bid_id == bid.id)).scalars()
        )
        assert len(rows) == 2, "no third copy"
        assert len({r.fingerprint for r in rows}) == 2, "the old-key row kept its own key"
        folded = [r for r in rows if r.duplicate_of_deliverable_id is not None]
        assert len(folded) == 1, "one of the twins points at the other"
        assert folded[0].equivalence_reason


def test_a_contradiction_cites_both_sources(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )

        def contradiction(prompt: str) -> dict:
            ordinals = ordinals_in(prompt)
            if len(ordinals) < 2:
                return {"issues": []}
            return {
                "issues": [
                    {
                        "issue_type": "CONTRADICTORY_QUANTITY",
                        "description": "Clause 4.2 says 25 applications; the amendment says 35.",
                        "severity": "MAJOR",
                        "recommended_question": "How many applications are in scope?",
                        "source_block_ordinal": ordinals[0],
                        "conflicting_block_ordinal": ordinals[1],
                        "confidence": 0.9,
                    }
                ]
            }

        raised = tender_analysis.analyze_ambiguity(
            s,
            ctx,
            bid_id=bid.id,
            gateway=gateway_for({"ambiguity_analysis": contradiction}),
        )
        assert raised == 1
        issue = (
            s.execute(
                select(ClarificationIssue).where(
                    ClarificationIssue.bid_id == bid.id,
                    ClarificationIssue.issue_type == ClarificationIssueType.CONTRADICTORY_QUANTITY,
                )
            )
            .scalars()
            .one()
        )
        assert issue.source_a and issue.source_b
        assert issue.source_a != issue.source_b
        assert issue.recommended_question
        s.commit()


def test_re_running_analysis_does_not_duplicate_findings(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )

        def blank(prompt: str) -> dict:
            return {
                "issues": [
                    {
                        "issue_type": "BLANK",
                        "description": "A required field is blank.",
                        "severity": "MINOR",
                        "source_block_ordinal": ordinals_in(prompt)[0],
                        "confidence": 0.8,
                    }
                ]
            }

        for _ in range(3):
            tender_analysis.analyze_ambiguity(
                s, ctx, bid_id=bid.id, gateway=gateway_for({"ambiguity_analysis": blank})
            )
        issues = (
            s.execute(select(ClarificationIssue).where(ClarificationIssue.bid_id == bid.id))
            .scalars()
            .all()
        )
        assert len(issues) == 1, "fingerprinting must collapse repeat detections"
        s.commit()


def test_an_issue_a_human_sent_to_the_client_is_not_reopened(app_sessionmaker, env):
    from app.services import clarifications

    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        issue = clarifications.raise_issue(
            s,
            ctx,
            bid_id=bid.id,
            issue_type=ClarificationIssueType.BLANK,
            description="original wording",
            severity=ClarificationSeverity.MINOR,
            fingerprint="stable-fingerprint",
            detected_by="test",
        )
        clarifications.update_status(
            s, ctx, issue_id=issue.id, status=ClarificationStatus.SENT_TO_CLIENT
        )
        clarifications.raise_issue(
            s,
            ctx,
            bid_id=bid.id,
            issue_type=ClarificationIssueType.BLANK,
            description="rewritten by a later run",
            severity=ClarificationSeverity.BLOCKING,
            fingerprint="stable-fingerprint",
            detected_by="test",
        )
        s.refresh(issue)
        assert issue.status is ClarificationStatus.SENT_TO_CLIENT
        assert issue.description == "original wording"
        s.commit()


def test_the_full_pass_produces_a_scope_confidence_and_audits_itself(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )

        result = tender_analysis.analyze_bid(s, ctx, bid_id=bid.id, gateway=_full_gateway())
        assert result.requirements["requirements_created"] > 0
        assert result.deliverables == 1
        assert result.scope_confidence in {"HIGH", "MEDIUM", "LOW"}

        record = tender_analysis.get_scope_confidence(s, ctx, bid_id=bid.id)
        assert record is not None
        assert record.signals["requirements_found"] > 0
        assert "evidence" not in json.dumps(record.signals).lower()

        actions = {
            e.action
            for e in s.execute(select(AuditEvent).where(AuditEvent.bid_id == bid.id))
            .scalars()
            .all()
        }
        for expected in (
            "precedence.resolved",
            "tender.facts_extracted",
            "requirements.extracted",
            "tender.scope_confidence_computed",
            "tender.analysis_completed",
        ):
            assert expected in actions, f"{expected} was not audited"
        s.commit()


def test_scope_confidence_ignores_company_knowledge(app_sessionmaker, env):
    """Acceptance criterion 15: adding company evidence must not move the gate."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        tender_analysis.analyze_bid(s, ctx, bid_id=bid.id, gateway=_full_gateway())
        before = tender_analysis.get_scope_confidence(s, ctx, bid_id=bid.id)
        before_level, before_signals = before.level, dict(before.signals)

        # A pile of company knowledge arrives. The tender is no clearer for it.
        _upload(
            s,
            ctx,
            env,
            data=COMPANY_TEXT,
            filename="method.md",
            bid_id=None,
            side=DocumentSide.COMPANY,
        )
        after = tender_analysis.compute_scope_confidence(s, ctx, bid_id=bid.id)

        assert after.level is before_level
        assert after.signals == before_signals
        s.commit()


def test_a_bid_cannot_be_analyzed_from_an_inapplicable_state(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        opportunity = bids.create_opportunity(
            s, ctx, customer_name="X", tender_name=f"Y {uuid.uuid4().hex[:4]}"
        )
        bid = bids.promote_to_bid(s, ctx, opportunity_id=opportunity.id)
        assert bid.state is BidState.DRAFT
        with pytest.raises(ValidationError, match="not in a state"):
            tender_analysis.analyze_bid(s, ctx, bid_id=bid.id, gateway=_full_gateway())
        s.rollback()


def test_bid_state_transitions_are_guarded_and_audited(app_sessionmaker, env):
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        with pytest.raises(IllegalTransition):
            bids.transition(s, ctx, bid_id=bid.id, to_state=BidState.RELEASED)
        with pytest.raises(ValidationError, match="requires a reason"):
            bids.transition(s, ctx, bid_id=bid.id, to_state=BidState.NO_BID)

        bids.transition(
            s, ctx, bid_id=bid.id, to_state=BidState.NO_BID, reason="Cannot meet the deadline."
        )
        s.refresh(bid)
        assert bid.state is BidState.NO_BID and bid.no_bid_reason
        event = (
            s.execute(select(AuditEvent).where(AuditEvent.action == "bid.state_changed"))
            .scalars()
            .first()
        )
        assert event is not None and event.before_state and event.after_state
        s.commit()


def test_tender_intelligence_does_not_leak_across_tenants(app_sessionmaker, env, seeded_orgs):
    org_a, org_b = seeded_orgs
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org_a)
        apply_tenant_context(s, ctx)
        bid = _new_bid(s, ctx)
        _upload(
            s,
            ctx,
            env,
            data=RFP_TEXT,
            filename="rfp.md",
            bid_id=bid.id,
            doc_type=DocumentType.RFP,
            issue_date=date(2026, 7, 14),
        )
        tender_analysis.analyze_bid(s, ctx, bid_id=bid.id, gateway=_full_gateway())
        s.commit()

    with app_sessionmaker() as s:
        s.begin()
        apply_tenant_context(s, ExecutionContext(org_id=org_b))
        for model in (Requirement, ClauseResolution, ClarificationIssue):
            rows = s.execute(select(model)).scalars().all()
            assert all(r.org_id == org_b for r in rows), f"{model.__name__} leaked"
        s.commit()
