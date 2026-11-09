"""Run one bid end to end against evidence that actually exists.

## Why this exists

The Jadwa bid reports 0 DRAFTABLE, and that is the CORRECT answer for it: its
company pack is a register of evidence to be collected, not evidence. Measured:
42 requirements retrieve nothing, 30 have material the relevance judge rightly
rejects, 0 have complete proof of anything.

The consequence is the thing worth fixing. Stages 4 to 12 of the product --
task assignment, task-scoped chat, cross-department requests, approval into
response artifacts, assembly, cross-section validation, release blocking,
outcome learning -- are modelled, coded, and have never executed, because no
input has ever reached them. Four test files out of 772 touch that half.

This seeds a company whose documents ARE evidence, and a tender written so that
every requirement has a known correct answer beforehand. See
`tests/fixtures/cybranytech/EXPECTED.md` for the table and the reasoning; the
expectations below are the same table in executable form, and they are asserted
rather than printed, because a fixture whose result is only ever eyeballed
becomes a second thing to audit.

## Its own organization, always

Everything runs in an org with its own slug. The Jadwa data is in another org
and RLS keeps them apart, but the reason is not only safety -- a fixture that
shares a tenant with real work cannot be re-run freely, and a fixture that
cannot be re-run freely stops being used. `--reset` drops and rebuilds this
org's bid without touching anything else.

    python scripts/seed_fixture_bid.py            # build, run, report
    python scripts/seed_fixture_bid.py --reset    # rebuild from scratch
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault(
    "BIDREADY_DATABASE_URL",
    os.environ.get("BIDREADY_LOCAL_DATABASE_URL")
    or "postgresql+psycopg://bidready_app:bidready-local@localhost:5432/bidready",
)

# BEFORE any app import. `settings.vector_backend` picks the embedding column
# type and is resolved once at import time; run_local.py sets it and a
# standalone script does not, so the ORM builds pgvector columns against a
# real[] schema and the first write dies on "malformed array literal". Detected
# from the live schema rather than hardcoded -- see scripts/_vector_backend.py.
from _vector_backend import align_vector_backend  # noqa: E402

align_vector_backend(verbose=True)

from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.ingestion.safety import EicarAwareScanner  # noqa: E402
from app.models.enums import (  # noqa: E402
    ConfidentialityClass,
    DocumentSide,
    DocumentType,
)
from app.services import (  # noqa: E402
    assessment,
    bids,
    ingestion,
    knowledge,
    requirements,
    tender_analysis,
)
from app.services.storage import InMemoryStorage  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cybranytech"
ORG_SLUG = "cybranytech-fixture"

# Company documents, with the type a human filing them would state.
#
# Stated at upload rather than left for a classifier, exactly as
# `ingestion.register_version` intends: "a stated type is a human filing
# decision and outranks anything a classifier would infer". The fixture is
# testing the draftable path, not the unbuilt classifier, and leaving these
# blank would make every row OTHER and confound the two.
COMPANY: tuple[tuple[str, DocumentType, ConfidentialityClass], ...] = (
    ("01_accreditation.md", DocumentType.ACCREDITATION, ConfidentialityClass.INTERNAL),
    ("02_iso27001.md", DocumentType.CERTIFICATE, ConfidentialityClass.INTERNAL),
    ("03_cv_ahmed_hassan.md", DocumentType.CV, ConfidentialityClass.INTERNAL),
    ("04_cv_zain_malik.md", DocumentType.CV, ConfidentialityClass.INTERNAL),
    ("05_project_reference.md", DocumentType.PROJECT_REFERENCE, ConfidentialityClass.INTERNAL),
    ("06_vapt_methodology.md", DocumentType.METHODOLOGY, ConfidentialityClass.INTERNAL),
    # RESTRICTED on purpose: a tester must not see rates. This is what should
    # push clause 4.1 through a cross-department request instead of into a
    # drafted answer.
    ("07_rate_card.md", DocumentType.RATE_CARD, ConfidentialityClass.RESTRICTED),
)

# The certificate is expired in the document text. The date is set explicitly
# here as well, because `expiry_date` is a governed column and nothing in the
# pipeline currently reads a date out of prose -- assuming it does is how a
# fixture comes to prove something it never tested.
EXPIRY = {"02_iso27001.md": date(2025, 3, 14)}

# Substrings that identify a requirement, and the readiness it must reach.
# Matched on requirement text because codes are assigned by extraction order
# and would make this file re-ordering-sensitive for no benefit.
#
# Each expectation is a SET of acceptable outcomes, not one value, and that is
# a deliberate narrowing of what this fixture claims rather than a softening of
# it.
#
# Two rows swapped places between runs: the API-methodology row went DRAFTABLE
# then READY, and the rate-card row went READY then DRAFTABLE. Both sit on the
# same seam -- PARTIAL becomes DRAFTABLE, CURRENT_VERIFIED becomes READY -- and
# which side a row lands on depends on the relevance judge calling coverage
# partial or full. That is a real judgement about meaning and it varies. Pinning
# it makes the fixture fail on noise, and a red line that cries wolf is one
# people learn to skip, which costs more than it catches.
#
# What must NOT vary is the distinction the product exists to make:
#
#     answerable      DRAFTABLE, READY, NEEDS_CLIENT_CLARIFICATION
#     needs renewal   NEEDS_REFRESH        -- proof exists and has lapsed
#     not answerable  BLOCKED              -- nothing here proves it
#
# So the sets never straddle those groups. A row expected to be answerable can
# never be accepted as BLOCKED, and -- the direction that actually matters --
# a row expected BLOCKED can never be accepted as answerable. That is the false
# positive that puts an unbackable claim in front of a customer, and it is
# asserted exactly.
#
# NEEDS_CLIENT_CLARIFICATION belongs in the answerable group, and the reason is
# structural rather than convenient. An open question DEMOTES a row, and the
# precedence in `assessment.py` is explicit that it "yields to the states that
# mean WE STILL OWE PROOF: BLOCKED, and NEEDS_REFRESH". So a row can only reach
# NCC if its evidence verdict was already answerable -- a row with nothing
# behind it goes BLOCKED and stays there. Accepting NCC therefore cannot hide a
# missing proof, which is the only thing this fixture is protecting.
#
# It has to be accepted, too: the ambiguity detector found 8, 10 and 13
# questions across three runs of the same tender, so which rows carry an open
# question is not stable, and pinning it would make the fixture flap on a
# detector that is working correctly.
ANSWERABLE = ("DRAFTABLE", "READY", "NEEDS_CLIENT_CLARIFICATION")

EXPECTATIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("penetration-testing accreditation", ANSWERABLE, "current accreditation, in the pack"),
    ("Offensive Security Certified Professional", ANSWERABLE, "two OSCP CVs"),
    ("web application testing methodology", ANSWERABLE, "approved methodology"),
    ("programming interface testing", ANSWERABLE, "approved methodology"),
    ("ISO/IEC 27001", ("NEEDS_REFRESH",), "evidence complete, certificate EXPIRED"),
    # Was the one KNOWN RED, and is now the case the counting rule exists for.
    #
    # One project exists, three are required, and the run reported DRAFTABLE --
    # "the gaps must be filled during drafting", which is false: nobody drafts
    # their way to two more delivered projects. It was left red rather than
    # widened to accept DRAFTABLE, because it crosses the one boundary these
    # sets must never straddle and it is the false-positive direction, and
    # because a red line switched off the first time it fires is not a red line.
    #
    # Closed by splitting the reading from the arithmetic:
    # `requirement_quantity` reads that the clause demands three, and
    # `assessment.quantity_shortfall` counts one distinct source and blocks. The
    # relevance prompt was NOT taught to call one project "not partial proof" --
    # that was tried, and it made the verdicts flap, because the one reference
    # genuinely IS partial proof.
    ("three comparable projects", ("BLOCKED",), "quantity shortfall: 1 project, 3 required"),
    ("mobile application testing methodology", ("BLOCKED",), "deliberately absent"),
    ("project schedule", ("BLOCKED",), "bid-specific, must become a commitment"),
    ("nominate a Project Manager", ("BLOCKED",), "nobody to nominate in the pack"),
    # Assessed as SYSTEM, which applies no permission filter, so the RESTRICTED
    # rate card is legitimately visible and answerable is right for the caller
    # doing the asking. The permission path is NOT covered here -- testing it
    # needs a scoped user, which is gate two item 4.
    ("daily rate", ANSWERABLE, "visible to SYSTEM; the permission path is untested here"),
)


def _org(session):
    from app.models import Organization

    org = session.execute(
        select(Organization).where(Organization.slug == ORG_SLUG)
    ).scalar_one_or_none()
    if org is None:
        org = Organization(name="Cybranytech FZ-LLC", slug=ORG_SLUG, residency_region="ae-central")
        session.add(org)
        session.flush()
        print(f"organization: created {org.id}")
    else:
        print(f"organization: exists {org.id}")
    return org


# One query, asked of pg_catalog by OID.
#
# The first version used `information_schema.columns` with
# `has_table_privilege(current_user, quote_ident(table_name), 'DELETE')` and
# failed with `relation "schemata" does not exist`: a bare name is resolved
# through `search_path`, and the planner is free to evaluate the privilege
# check before the `table_schema = 'public'` filter, so it ran against rows it
# was never meant to see. Passing `c.oid` removes name resolution from the
# question altogether -- the table is already identified.
_TENANT_TABLES = """
SELECT c.relname, has_table_privilege(c.oid, 'DELETE') AS deletable
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = 'public'
  AND c.relkind = 'r'
  AND a.attname = 'org_id'
  AND NOT a.attisdropped
"""

# Foreign keys between those tables, by OID for the same reason.
_FOREIGN_KEYS = """
SELECT child.relname, parent.relname
FROM pg_constraint k
JOIN pg_class child ON child.oid = k.conrelid
JOIN pg_class parent ON parent.oid = k.confrelid
JOIN pg_namespace n ON n.oid = child.relnamespace
WHERE k.contype = 'f' AND k.conrelid <> k.confrelid AND n.nspname = 'public'
"""


def _tenant_tables(session) -> tuple[list[str], list[str]]:
    """Every org-scoped table, split into what this role may delete and what it
    may not.

    `audit_event` is append-only: the application role holds INSERT and SELECT
    and no DELETE, which is what "nothing writes without an audit row" is worth.
    A reset must not be able to erase the record of what happened, and a fixture
    is not a reason to weaken that -- so the audit trail is left behind on
    purpose, and reported rather than dropped silently.
    """
    from sqlalchemy import text

    deletable: list[str] = []
    protected: list[str] = []
    for name, may_delete in session.execute(text(_TENANT_TABLES)):
        (deletable if may_delete else protected).append(name)
    return deletable, sorted(protected)


def _delete_order(session, tables: list[str]) -> list[str]:
    """`tables`, children before parents, from the DATABASE's own graph.

    The obvious version read `Base.metadata.sorted_tables` and was wrong in
    exactly the place it needed to be right. `document.bid_id` is declared on
    the model as a plain column; its foreign key (`fk_document_bid_org`) exists
    only in migration 0004, added with raw SQL. The ORM therefore sorts
    `document` BEFORE `bid`, and deleting in reverse takes out the bid first --
    reproducing the error this function exists to avoid, while looking
    principled. Verified before shipping, which is the only reason it was
    caught.

    So the graph comes from `pg_constraint`, which cannot disagree with the
    constraints actually being enforced.
    """
    from sqlalchemy import text

    known = set(tables)
    parents: dict[str, set[str]] = {t: set() for t in tables}
    for child, parent in session.execute(text(_FOREIGN_KEYS)):
        if child in known and parent in known:
            parents[child].add(parent)

    ordered: list[str] = []
    remaining = dict(parents)
    while remaining:
        ready = sorted(t for t, deps in remaining.items() if not (deps & remaining.keys()))
        if not ready:
            # A cycle, which this schema does not have today. Appending the rest
            # keeps the wipe running rather than hanging; a real cycle would
            # then fail loudly on the DELETE, which is the right way to find out.
            ordered.extend(sorted(remaining))
            break
        ordered.extend(ready)
        for t in ready:
            remaining.pop(t)
    return list(reversed(ordered))


def _wipe_org(session, org_id) -> None:
    """Delete every row this organization owns, children first.

    Two narrower versions were tried and both failed, each on a different
    constraint:

        fk_document_bid_org           deleting a bid while its tender document
                                      still pointed at it
        fk_clause_resolution_..._org  deleting that document while a clause
                                      resolution still pointed at its version

    71 tenant tables with dependencies several levels deep is whack-a-mole with
    a long tail, and every miss costs a round trip. `_delete_order` computes the
    order once, from the constraints the database is actually enforcing.

    Deliberately a full wipe rather than "just the bid". A partial reset is why
    the corpus came back unapproved after a crashed run: half-old, half-new
    state nobody can reason about. Seven model calls and about a minute buys a
    run that starts from a known point every time.

    `organization` and `permission` are the only tables without `org_id`, and
    neither is touched, so the org id stays stable across resets.
    """
    from sqlalchemy import text

    deletable, protected = _tenant_tables(session)
    removed = 0
    for name in _delete_order(session, deletable):
        result = session.execute(
            text(f'DELETE FROM "{name}" WHERE org_id = :org'), {"org": org_id}
        )
        removed += result.rowcount or 0
    session.flush()
    print(f"reset: {removed} row(s) removed; the organization itself was kept")
    if protected:
        print(
            "  append-only, left intact: "
            + ", ".join(protected)
            + "\n  (the role holds no DELETE there, which is the point of an audit trail)"
        )


def _ingest(session, ctx, storage, scanner, embedder, gateway) -> None:
    from app.models import Document, Evidence

    scanner = scanner or EicarAwareScanner()
    for name, kind, klass in COMPANY:
        already = session.execute(
            select(Document).where(
                Document.org_id == ctx.org_id, Document.original_filename == name
            )
        ).scalar_one_or_none()
        if already is not None:
            print(f"  skip {name} (already ingested)")
            continue
        version = ingestion.register_upload(
            session,
            ctx,
            data=(FIXTURE / "company" / name).read_bytes(),
            filename=name,
            side=DocumentSide.COMPANY,
            confidentiality_class=klass,
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
        expiry = EXPIRY.get(name)
        if expiry is not None:
            # Columns, not entities: loading Evidence pulls `excerpt_vector`
            # and pgvector's decoder raises on the value psycopg returns.
            from sqlalchemy import update

            session.execute(
                update(Evidence)
                .where(
                    Evidence.org_id == ctx.org_id,
                    Evidence.source_document_version_id == version.id,
                )
                .values(expiry_date=expiry)
            )
        print(f"  ingested {name}  ({kind.value}, {klass.value})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="rebuild the fixture bid")
    args = parser.parse_args()

    from app.gateway.embeddings import HashingEmbeddingProvider
    from app.gateway.gateway import build_gateway
    from app.models import Opportunity

    storage, scanner = InMemoryStorage(), EicarAwareScanner()
    embedder = HashingEmbeddingProvider()
    gateway = build_gateway()

    session = get_session_factory()()
    try:
        session.begin()
        org = _org(session)
        ctx = ExecutionContext(org_id=org.id, actor_kind="SYSTEM")
        apply_tenant_context(session, ctx)

        if args.reset:
            _wipe_org(session, org.id)
        # Departments and people BEFORE anything else.
        #
        # `generate_tasks` skips any requirement whose category maps to a
        # department the organization does not have, so an org with no
        # departments produces no tasks -- and drafting, artifacts, requests and
        # assembly all hang off a task. The first walk of gate two died on
        # "tasks exist for the bid" for exactly this reason, with assessment
        # reporting 17 requirements and 0 tasks and nothing saying why.
        #
        # It belongs here rather than in the walk script because it is part of
        # being a company that uses this product: departments are configured
        # before a tender arrives, not after its requirements are assessed.
        from _fixture_cast import ensure as ensure_cast

        ensure_cast(session, ctx, org)

        print("\ncompany documents")
        _ingest(session, ctx, storage, scanner, embedder, gateway)

        # The Knowledge Administrator's approval, which the first version of
        # this fixture skipped -- and the run said so plainly:
        #
        #     assessment_started  approved=0  embedded=0  total=18
        #
        # `_admissible()` requires APPROVED, and `knowledge_extraction` records
        # every excerpt PROPOSED. So all 18 rows existed, none were candidates,
        # every requirement saw an empty pool and landed on BLOCKED. Not a
        # defect in the draftable path: the governance gate doing its job, on a
        # fixture that had not walked through it.
        #
        # It belongs here because it is stage 1 of the product's own
        # walkthrough -- "the knowledge administrator later verifies it" --
        # and a company that has never approved its corpus genuinely cannot
        # bid from it. Modelling that step is part of modelling the company.
        #
        # `approve_evidence_batch` is the real API, not a direct UPDATE: it
        # audits each row individually, refuses anything
        # `placeholders.disqualifying_match` rejects, and skips expired rows
        # rather than failing the batch. A fixture that bypassed it would prove
        # the pipeline works for evidence no reviewer would have accepted.
        outcome = knowledge.approve_evidence_batch(
            session,
            ctx,
            evidence_ids=None,
            reason="Fixture: company corpus reviewed and approved for bid reuse.",
        )
        print(
            f"\nknowledge administrator: approved {outcome['approved']} excerpt(s)"
            f", skipped {len(outcome['skipped'])}"
        )
        for held in outcome["skipped"]:
            print(f"    held back ({held.get('reason')}): {held.get('excerpt', '')[:90]}")
        session.flush()

        opportunity = session.execute(
            select(Opportunity).where(
                Opportunity.org_id == org.id, Opportunity.tender_reference == "EDSA/RFP/2026/114"
            )
        ).scalar_one_or_none()
        if opportunity is None:
            opportunity = bids.create_opportunity(
                session,
                ctx,
                customer_name="Emirate Digital Services Authority",
                tender_name="Cybersecurity Assessment and Penetration Testing Services",
                tender_reference="EDSA/RFP/2026/114",
                deadline=datetime(2026, 3, 17, 10, 0, tzinfo=timezone.utc),
                currency="AED",
                region="ae-dubai",
            )
        bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
        print(f"\nbid {bid.id}")

        from app.models import Document

        tender_name = "RFP_Cybersecurity_Assessment.md"
        if (
            session.execute(
                select(Document).where(
                    Document.org_id == org.id,
                    Document.bid_id == bid.id,
                    Document.original_filename == tender_name,
                )
            ).scalar_one_or_none()
            is None
        ):
            version = ingestion.register_upload(
                session,
                ctx,
                data=(FIXTURE / "tender" / tender_name).read_bytes(),
                filename=tender_name,
                side=DocumentSide.TENDER,
                bid_id=bid.id,
                confidentiality_class=ConfidentialityClass.INTERNAL,
                document_type=DocumentType.RFP,
                scanner=scanner,
                storage=storage,
            )
            ingestion.parse_and_index(
                session, ctx, version_id=version.id, storage=storage, embedder=embedder
            )
            print(f"  ingested {tender_name}")
        session.commit()

        session.begin()
        apply_tenant_context(session, ctx)
        bids.ensure_intake_open(session, ctx, bid_id=bid.id)
        session.commit()

        session.begin()
        apply_tenant_context(session, ctx)
        print("\nunderstanding the tender ...")
        analysis = tender_analysis.analyze_bid(session, ctx, bid_id=bid.id, gateway=gateway)
        print(f"  {analysis}")
        session.commit()

        session.begin()
        apply_tenant_context(session, ctx)
        print("extracting requirements ...")
        report = requirements.extract_requirements(session, ctx, bid_id=bid.id, gateway=gateway)
        print(f"  {report}")
        session.commit()

        session.begin()
        apply_tenant_context(session, ctx)
        print("assessing ...")
        result = assessment.assess_bid(session, ctx, bid_id=bid.id)
        print(f"  {result}")
        session.commit()

        # --- the report --------------------------------------------------
        session.begin()
        apply_tenant_context(session, ctx)
        from app.models import Requirement, RequirementAssessment, RequirementVersion

        rows = list(
            session.execute(
                select(Requirement, RequirementVersion, RequirementAssessment)
                .outerjoin(
                    RequirementVersion,
                    (RequirementVersion.id == Requirement.current_version_id)
                    & (RequirementVersion.org_id == Requirement.org_id),
                )
                .outerjoin(
                    RequirementAssessment,
                    (RequirementAssessment.requirement_id == Requirement.id)
                    & (RequirementAssessment.org_id == Requirement.org_id)
                    & (RequirementAssessment.is_current.is_(True)),
                )
                .where(Requirement.org_id == org.id, Requirement.bid_id == bid.id)
                .order_by(Requirement.code)
            ).all()
        )
        print(f"\n{len(rows)} requirement(s)\n")
        print("=" * 100)
        passed = failed = unmatched = 0
        for needle, expected, why in EXPECTATIONS:
            hit = next(
                (
                    (r, v, a)
                    for r, v, a in rows
                    if needle.lower() in ((v.requirement_text if v else "") or "").lower()
                ),
                None,
            )
            if hit is None:
                print(f"  ?  no requirement matched {needle!r}")
                print(f"        expected {expected} -- {why}")
                unmatched += 1
                continue
            r, v, a = hit
            actual = getattr(
                getattr(a, "bid_readiness", None), "value", getattr(a, "bid_readiness", None)
            )
            ok = actual in expected
            passed += ok
            failed += not ok
            wanted = expected[0] if len(expected) == 1 else " or ".join(expected)
            print(f"  {'PASS' if ok else 'FAIL'}  {r.code}  expected {wanted}, got {actual}")
            print(f"        {why}")
            print(
                textwrap.fill(
                    " ".join(((v.requirement_text if v else "") or "").split())[:150],
                    96,
                    initial_indent="        > ",
                    subsequent_indent="          ",
                )
            )
        print("=" * 100)
        print(f"\n{passed} passed, {failed} failed, {unmatched} not extracted at all.")
        if unmatched:
            print(
                "\nA requirement that was never extracted is an extraction defect, not a\n"
                "readiness one -- it fails earlier in the pipeline than this fixture measures."
            )
        print(
            "\nGate one: at least one DRAFTABLE, and the ISO row NEEDS_REFRESH rather than\n"
            "DRAFTABLE. The first proves the draftable path exists; the second proves an\n"
            "expiry still demotes it -- the two axes staying orthogonal.\n"
            "See tests/fixtures/cybranytech/EXPECTED.md for gate two."
        )
        session.commit()
        return 0 if failed == 0 and unmatched == 0 else 1
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
