"""The rule that was declared unwaivable and could never fire.

`RESTRICTED_DISCLOSURE` has sat in `release.NON_WAIVABLE` since the ruleset was
written, and until now nothing in the codebase raised it. The effect was a
comment dressed as a control: the code said "this can never be waived" about a
finding that could never occur, and every release passed the check by having
nothing to check. A grep for the code found one hit, its own declaration.

The case is in the fixture tender in as many words -- "Pricing must not appear
anywhere in the technical proposal" -- and the company's rate card is classified
RESTRICTED at ingestion. Nothing stopped a drafter citing it, a reviewer
approving the answer, and the day rates leaving the building inside the
technical submission. That is not a formatting mistake. Day rates in the
technical envelope disqualify a bid outright under most public procurement
rules, and the company would find out from the rejection letter.

## What is checked, and what deliberately is not

The confidentiality CLASS on the cited evidence, not the words in the rendered
answer. Scanning prose for something that looks like a rate is a guess in both
directions: it misses "our blended daily rate is five thousand dirhams" and it
fires on a page number. The class is a decision a person recorded when the
document was ingested, and the citation on the statement says exactly which
classified row the answer leans on.

CONFIDENTIAL is not blocked, and that is the line that makes the rule usable.
It is the ordinary class for internal company material -- policies,
methodologies, CVs -- which is what a proposal is made of, and blocking it would
block every release and teach everybody to waive the check. RESTRICTED and
PRIVILEGED are the two somebody had to choose deliberately.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.ingestion.safety import EicarAwareScanner
from app.models import Department, Evidence
from app.models.enums import (
    ApprovalStatus,
    ApprovalStepState,
    ConfidentialityClass,
    DocumentSide,
    DocumentType,
    FindingWaivability,
    KnowledgeStatus,
    OutputMode,
    PackageState,
)
from app.services import artifacts, bids, ingestion, release
from app.services import tasks as task_service
from app.services.storage import InMemoryStorage
from tests.integration.test_release_lifecycle import _ctx, _user

pytestmark = pytest.mark.integration

RATE_CARD = b"""# Role-based day rates

| Role | Day rate AED |
|---|---|
| Security Specialist | 5,000 |
"""


def _evidence_classified(session, ctx, env_storage, scanner, klass: ConfidentialityClass):
    """Approved, current, reusable evidence -- and classified.

    Every other admissibility gate is deliberately satisfied. The point is that
    a perfectly usable piece of evidence can still be one the company must not
    quote to a customer, which is a different question from whether it is true
    and in date.
    """
    version = ingestion.register_upload(
        session,
        ctx,
        data=RATE_CARD,
        filename=f"rates-{uuid.uuid4().hex[:6]}.md",
        side=DocumentSide.COMPANY,
        bid_id=None,
        confidentiality_class=klass,
        scanner=scanner,
        storage=env_storage,
    )
    item = Evidence(
        org_id=ctx.org_id,
        source_document_version_id=version.id,
        source_locator={"block_ordinal": 1},
        excerpt="Security Specialist | Day rate AED: 5,000",
        evidence_type=DocumentType.RATE_CARD,
        approval_status=ApprovalStatus.APPROVED,
        confidentiality_class=klass,
        status=KnowledgeStatus.CURRENT_VERIFIED,
        approved_for_reuse=True,
    )
    session.add(item)
    session.flush()
    return item


def _packaged_citing(
    session,
    org_id,
    users,
    klass: ConfidentialityClass,
    text: str = "Our Security Specialist rate is AED 5,000 per day.",
):
    """An approved technical answer that cites evidence of the given class."""
    storage = InMemoryStorage()
    maker = _user(session, org_id, users[org_id]["manager"])
    ctx = _ctx(org_id, maker.id, "BID_MANAGER")
    evidence = _evidence_classified(session, ctx, InMemoryStorage(), EicarAwareScanner(), klass)

    opportunity = bids.create_opportunity(
        session,
        ctx,
        customer_name="Emirate Digital Services Authority",
        tender_name=f"Disclosure Tender {uuid.uuid4().hex[:6]}",
    )
    bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
    department = (
        session.execute(select(Department).where(Department.org_id == org_id)).scalars().first()
    )
    task = task_service.create_task(
        session,
        ctx,
        bid_id=bid.id,
        title="Technical response",
        department_id=department.id,
        owner_user_id=maker.id,
    )
    artifact = artifacts.for_task(session, ctx, task_id=task.id)
    version = artifacts.save_draft(
        session,
        ctx,
        artifact_id=artifact.id,
        statements=[
            {
                "statement_class": "EVIDENCE_BACKED_FACT",
                "text": text,
                "evidence_ids": [evidence.id],
            }
        ],
    )
    artifacts.submit(session, ctx, artifact_id=artifact.id, approval_roles=["BID_MANAGER"])
    approval = artifacts.approval_chain(
        session, ctx, entity_type="ARTIFACT_VERSION", entity_id=version.id
    )[0]
    artifacts.decide_approval(
        session, ctx, approval_id=approval.id, decision=ApprovalStepState.APPROVED
    )

    release.derive_structure(session, ctx, bid_id=bid.id)
    release.assemble(
        session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION, storage=storage
    )
    package = release.create_package(session, ctx, bid_id=bid.id, output_mode=OutputMode.SUBMISSION)
    release.validate_package(session, ctx, package_id=package.id, storage=storage)
    return ctx, package


def _disclosure(session, ctx, package):
    findings = release.validation_findings(session, ctx, package_id=package.id)
    return next((row for row in findings if row.check_code == "RESTRICTED_DISCLOSURE"), None)


def test_a_restricted_rate_cannot_leave_inside_the_proposal(app_sessionmaker, seeded_orgs, users):
    """The whole point. Everything else about this answer is in order.

    The evidence is approved, reusable, current and unexpired -- it passes every
    admissibility gate the product has -- and the statement is a properly typed
    EVIDENCE_BACKED_FACT that cites it. The answer is true. It still must not go
    out, because the company classified the rate card RESTRICTED and the tender
    forbids pricing in the technical proposal.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, package = _packaged_citing(session, org_id, users, ConfidentialityClass.RESTRICTED)
        finding = _disclosure(session, ctx, package)
        assert finding is not None, (
            "a RESTRICTED rate card reached an outgoing proposal and nothing objected"
        )
        assert "RESTRICTED" in finding.message
        session.rollback()


def test_the_disclosure_finding_cannot_be_waived(app_sessionmaker, seeded_orgs, users):
    """Non-waivable in fact, not only in a set literal.

    The declaration in `NON_WAIVABLE` was true and inert for as long as nothing
    raised the code. Now that something does, the property worth asserting is
    that the finding it produces actually arrives marked NON_WAIVABLE and takes
    the package to BLOCKED_HARD -- the state with no exception path -- rather
    than to BLOCKED_PENDING_EXCEPTION, where a tired approver could sign it off
    at five o'clock on a submission day.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, package = _packaged_citing(session, org_id, users, ConfidentialityClass.RESTRICTED)
        finding = _disclosure(session, ctx, package)
        assert finding is not None
        assert finding.waivability is FindingWaivability.NON_WAIVABLE
        assert package.state is PackageState.BLOCKED_HARD
        session.rollback()


def test_privileged_material_is_treated_the_same_way(app_sessionmaker, seeded_orgs, users):
    """Legal advice is the other class somebody chooses on purpose."""
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, package = _packaged_citing(session, org_id, users, ConfidentialityClass.PRIVILEGED)
        finding = _disclosure(session, ctx, package)
        assert finding is not None
        assert "PRIVILEGED" in finding.message
        session.rollback()


@pytest.mark.parametrize(
    "klass",
    [ConfidentialityClass.PUBLIC, ConfidentialityClass.INTERNAL, ConfidentialityClass.CONFIDENTIAL],
)
def test_ordinary_company_material_still_ships(app_sessionmaker, seeded_orgs, users, klass):
    """The half that keeps the rule from being waived out of existence.

    CONFIDENTIAL is the default class for a policy, a methodology, a CV -- the
    substance of a proposal. A rule that blocked those would fire on every
    release, and a control that fires on everything is one people learn to click
    past. It has to be quiet on the ordinary case to be believed on the real
    one.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        # Not a price: since PRICE_IN_TECHNICAL, a price in the technical
        # submission blocks release whatever its class -- see the test below.
        ctx, package = _packaged_citing(
            session, org_id, users, klass, text="Our testers hold current OSCP certification."
        )
        assert _disclosure(session, ctx, package) is None, (
            f"{klass.value} material is what proposals are made of; blocking it would "
            "make the check meaningless"
        )
        assert package.state is not PackageState.BLOCKED_HARD
        session.rollback()


def test_a_price_typed_into_the_technical_proposal_blocks_release(
    app_sessionmaker, seeded_orgs, users
):
    """Technical proposal page 37 contains AED 18,000.

    RESTRICTED_DISCLOSURE catches a classified rate card being cited. This is
    the other way a price leaks: typed in, citing ordinary material. Checked on
    the words, in the technical submission only, and never waivable.
    """
    org_id = seeded_orgs[0]
    with app_sessionmaker() as session:
        session.begin()
        ctx, package = _packaged_citing(
            session,
            org_id,
            users,
            ConfidentialityClass.INTERNAL,
            text="The full assessment will cost AED 18,000.",
        )
        findings = release.validation_findings(session, ctx, package_id=package.id)
        price = [f for f in findings if f.check_code == "PRICE_IN_TECHNICAL"]
        assert price, "a price in the technical proposal must be found"
        assert price[0].waivability is FindingWaivability.NON_WAIVABLE
        assert package.state is PackageState.BLOCKED_HARD
        session.rollback()
