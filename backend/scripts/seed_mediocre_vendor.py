"""The real Jadwa tender, answered by a made-up vendor that is only partly qualified.

## Why

`seed_fixture_bid.py` runs a tender written for this project, with clean
clauses, against a company whose documents answer them. It proves the pipeline
works. It does not show how the pipeline copes with a REAL tender -- Jadwa's
wording is messy, its clauses run together, its typos are real ("confgiuration",
"reconsillation") -- or with a vendor that is ordinary: strong on some clauses,
thin on others, silent on the rest, and occasionally saying outright that it
cannot do what is asked.

## What is and is not real

  * The TENDER is real: the Jadwa RFP, passed in with `--tender`. It is marked
    strictly confidential in its own text (R-064), so it is never copied into
    the repository -- the script reads it from wherever it already sits.
  * The VENDOR is fictional: Wadi Lumen Software Co., in
    `tests/fixtures/wadi_lumen/company/`. It has no connection to the company
    behind the Jadwa bid, and none of its documents may be uploaded to that
    company's organization. It runs in its own organization, `wadi-lumen-fixture`,
    exactly like the Cybranytech fixture.

## What it checks

See `tests/fixtures/wadi_lumen/EXPECTED.md`. Three groups:

  * ANSWERABLE -- the pack clearly covers it. Must not come out BLOCKED.
  * NOT ANSWERABLE -- the pack is silent, or says it cannot. Must never come out
    READY or DRAFTABLE. This is the direction that matters.
  * JUDGEMENT -- the pack half-covers it (150 concurrent users against 300,
    a 30-day warranty against three months). Reported, not asserted: which way
    these fall is exactly what a reviewer should look at.

Costs model calls (tender analysis, extraction, assessment -- a few minutes of
DeepSeek). Re-runnable; `--reset` wipes only this fixture organization.

    python scripts/seed_mediocre_vendor.py --tender "PATH\\TO\\JADWA-RFP.pdf"
    python scripts/seed_mediocre_vendor.py --tender "..." --reset
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Importing it applies the same database URL default and vector-backend
# alignment, BEFORE any app import, and lends us `_wipe_org`.
import seed_fixture_bid as base  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.context import ExecutionContext  # noqa: E402
from app.db.session import apply_tenant_context, get_session_factory  # noqa: E402
from app.ingestion.safety import EicarAwareScanner  # noqa: E402
from app.models.enums import ConfidentialityClass, DocumentSide, DocumentType  # noqa: E402
from app.services import (  # noqa: E402
    assessment,
    bids,
    ingestion,
    knowledge,
    requirements,
    tender_analysis,
)
from app.services.requirement_scope import LIVE_REQUIREMENT  # noqa: E402
from app.services.storage import InMemoryStorage  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "wadi_lumen"
ORG_SLUG = "wadi-lumen-fixture"
ORG_NAME = "Wadi Lumen Software Co. (fictional test vendor)"

COMPANY: tuple[tuple[str, DocumentType], ...] = (
    ("01_company_profile.md", DocumentType.COMPANY_PROFILE),
    ("02_technology_capability.md", DocumentType.CAPABILITY_STATEMENT),
    ("03_project_references.md", DocumentType.PROJECT_REFERENCE),
    ("04_cv_faisal_alharbi.md", DocumentType.CV),
    ("05_cv_lina_qahtani.md", DocumentType.CV),
    ("06_delivery_and_qa_methodology.md", DocumentType.METHODOLOGY),
    ("07_secure_development_policy.md", DocumentType.POLICY),
)

ANSWERABLE = "ANSWERABLE"
NOT_ANSWERABLE = "NOT_ANSWERABLE"
JUDGEMENT = "JUDGEMENT"

# (substring of the requirement text, group, why). Matched case-insensitively on
# the re-extracted text, so they are short and distinctive; a needle that
# matches nothing is reported, not failed -- extraction of a real tender varies.
EXPECTATIONS: tuple[tuple[str, str, str], ...] = (
    ("microsoft c#", ANSWERABLE, "capability statement: C# / ASP.NET"),
    ("microsoft sql server", ANSWERABLE, "capability statement: SQL Server since 2016"),
    ("english and arabic", ANSWERABLE, "bilingual portals, RTL template"),
    ("responsive site", ANSWERABLE, "Bootstrap, menu collapses below 768px"),
    ("cross browser", ANSWERABLE, "tested on Chrome, Edge, Safari, Firefox"),
    ("support ssl", ANSWERABLE, "HTTPS binding and redirect in IIS"),
    # Moved to JUDGEMENT after reading the model's reasoning twice. "Jadwa will
    # bear the cost of SSL certificate, its release on site and site
    # condifugration ... will be responsibility of vendor" parses two ways, and
    # the system raised a customer question about it. BLOCKED-and-ask is a
    # defensible answer; calling it a failure was my reading, not the text's.
    ("cost of ssl certificate", JUDGEMENT, "pack: client buys, vendor configures"),
    ("iis setting", ANSWERABLE, "AlwaysRunning, compression"),
    ("cross site scripting", ANSWERABLE, "secure development policy, rule 2"),
    ("test case", ANSWERABLE, "QA methodology: test cases per requirement"),
    ("assign them roles", ANSWERABLE, "admin area with roles"),
    ("knowledge transfer", ANSWERABLE, "source code handover and walkthrough"),
    ("prior experience", ANSWERABLE, "2019 investor account-opening portal"),
    ("windows 2012", NOT_ANSWERABLE, "the pack says it no longer targets 2012"),
    ("3 months from final release", NOT_ANSWERABLE, "warranty is 30 days"),
    ("arabic sms", NOT_ANSWERABLE, "no SMS work; says it does not build gateways"),
    ("validation sms", NOT_ANSWERABLE, "no SMS work"),
    ("b2b", NOT_ANSWERABLE, "no banking integration"),
    ("dokmee", NOT_ANSWERABLE, "never mentioned"),
    ("google analytics", NOT_ANSWERABLE, "never mentioned"),
    ("signed jadwa nda", NOT_ANSWERABLE, "a submission act, not evidence"),
    ("oracle database", JUDGEMENT, "one read-only Oracle lookup, 2021"),
    ("concurrent 300", JUDGEMENT, "largest load test was 150 users"),
    ("load test results", JUDGEMENT, "a 150-user report exists; 300 does not"),
    ("encrypt the user sensitive data", JUDGEMENT, "only passwords by default"),
    ("user manual", JUDGEMENT, "English guides, on-site training; Arabic extra"),
    ("resumes of all key personnel", JUDGEMENT, "two CVs, no project manager"),
    ("pdf format with english and arabic", JUDGEMENT, "bilingual PDFs in 2022; no signing"),
)

FORBIDDEN = {
    ANSWERABLE: {"BLOCKED"},
    NOT_ANSWERABLE: {"READY", "DRAFTABLE"},
    JUDGEMENT: set(),
}


def _org(session):
    from app.models import Organization

    org = session.execute(
        select(Organization).where(Organization.slug == ORG_SLUG)
    ).scalar_one_or_none()
    if org is None:
        org = Organization(name=ORG_NAME, slug=ORG_SLUG, residency_region="ae-central")
        session.add(org)
        session.flush()
        print(f"organization: created {org.id}")
    else:
        print(f"organization: exists {org.id}")
    return org


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tender", required=True, help="path to the Jadwa RFP PDF")
    parser.add_argument("--reset", action="store_true", help="rebuild this fixture org")
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="re-read the tender even though requirements already exist",
    )
    args = parser.parse_args()

    tender_path = Path(args.tender)
    if not tender_path.is_file():
        print(f"No file at {tender_path}")
        return 2

    from app.gateway.embeddings import HashingEmbeddingProvider
    from app.gateway.gateway import build_gateway
    from app.models import (
        Document,
        Opportunity,
        Requirement,
        RequirementAssessment,
        RequirementVersion,
    )

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
            base._wipe_org(session, org.id)

        from _fixture_cast import ensure as ensure_cast

        ensure_cast(session, ctx, org, domain="wadi-lumen.example")

        print("\ncompany documents (fictional vendor)")
        for name, kind in COMPANY:
            exists = session.execute(
                select(Document).where(
                    Document.org_id == org.id, Document.original_filename == name
                )
            ).scalar_one_or_none()
            if exists is not None:
                print(f"  skip {name} (already ingested)")
                continue
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
            print(f"  ingested {name}  ({kind.value})")

        outcome = knowledge.approve_evidence_batch(
            session,
            ctx,
            evidence_ids=None,
            reason="Fixture: fictional vendor pack reviewed and approved for reuse.",
        )
        print(
            f"\nknowledge administrator: approved {outcome['approved']}, "
            f"held back {len(outcome['skipped'])}"
        )
        for held in outcome["skipped"]:
            print(f"    held back ({held.get('reason')}): {held.get('excerpt', '')[:90]}")

        opportunity = session.execute(
            select(Opportunity).where(
                Opportunity.org_id == org.id,
                Opportunity.tender_reference == "JADWA-AOS-2017",
            )
        ).scalar_one_or_none()
        if opportunity is None:
            opportunity = bids.create_opportunity(
                session,
                ctx,
                customer_name="Jadwa Investment",
                tender_name="Account Opening and Subscription Portal",
                tender_reference="JADWA-AOS-2017",
                currency="SAR",
                region="sa-riyadh",
            )
        bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
        print(f"\nbid {bid.id}")

        if (
            session.execute(
                select(Document).where(
                    Document.org_id == org.id,
                    Document.bid_id == bid.id,
                    Document.original_filename == tender_path.name,
                )
            ).scalar_one_or_none()
            is None
        ):
            version = ingestion.register_upload(
                session,
                ctx,
                data=tender_path.read_bytes(),
                filename=tender_path.name,
                side=DocumentSide.TENDER,
                bid_id=bid.id,
                confidentiality_class=ConfidentialityClass.CONFIDENTIAL,
                document_type=DocumentType.RFP,
                scanner=scanner,
                storage=storage,
            )
            ingestion.parse_and_index(
                session, ctx, version_id=version.id, storage=storage, embedder=embedder
            )
            print(f"  ingested {tender_path.name}")
        session.commit()

        # A re-run re-ASSESSES; it does not re-read the tender unless asked.
        #
        # The second run re-ran tender analysis as well, and that changed two
        # things at once: the relevance prompt (the point of the run) and the
        # open clarifications, because ambiguity analysis is not deterministic
        # and closed 20 questions and raised others. An open question demotes an
        # answerable row, so READY went from 5 to 0 for a reason that had
        # nothing to do with the change being measured. One variable per run.
        session.begin()
        apply_tenant_context(session, ctx)
        already_read = (
            session.execute(
                select(Requirement.id)
                .where(Requirement.org_id == org.id, Requirement.bid_id == bid.id)
                .where(LIVE_REQUIREMENT)
                .limit(1)
            ).first()
            is not None
        )
        session.commit()
        steps = [("opening intake", lambda: bids.ensure_intake_open(session, ctx, bid_id=bid.id))]
        if args.reanalyze or not already_read:
            steps += [
                (
                    "understanding the tender",
                    lambda: tender_analysis.analyze_bid(
                        session, ctx, bid_id=bid.id, gateway=gateway
                    ),
                ),
                (
                    "extracting requirements",
                    lambda: requirements.extract_requirements(
                        session, ctx, bid_id=bid.id, gateway=gateway
                    ),
                ),
            ]
        else:
            print("tender already read; re-assessing only (--reanalyze to read it again)")
        steps.append(("assessing", lambda: assessment.assess_bid(session, ctx, bid_id=bid.id)))
        for label, step in steps:
            session.begin()
            apply_tenant_context(session, ctx)
            print(f"{label} ...")
            result = step()
            if result is not None and label != "opening intake":
                print(f"  {result}")
            session.commit()

        # --- the report ---------------------------------------------------
        session.begin()
        apply_tenant_context(session, ctx)
        rows = session.execute(
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
            .where(LIVE_REQUIREMENT)
            .order_by(Requirement.code)
        ).all()

        def readiness(a) -> str | None:
            value = getattr(a, "bid_readiness", None)
            return getattr(value, "value", value)

        tally: dict[str, int] = {}
        for _r, _v, a in rows:
            tally[readiness(a) or "UNASSESSED"] = tally.get(readiness(a) or "UNASSESSED", 0) + 1
        print(f"\n{len(rows)} live requirement(s)")
        if not rows:
            print(
                "\nNothing was extracted, so nothing below means anything. Check that\n"
                "DEEPSEEK_API_KEY is set and that the tender parsed (see output above)."
            )
            session.commit()
            return 1
        for state, count in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f"  {state:<28} {count}")

        print("\n" + "=" * 100)
        broken = unmatched = 0
        for group in (ANSWERABLE, NOT_ANSWERABLE, JUDGEMENT):
            print(f"\n{group}")
            for needle, g, why in EXPECTATIONS:
                if g != group:
                    continue
                hits = [
                    (r, v, a)
                    for r, v, a in rows
                    if needle in " ".join(((v.requirement_text if v else "") or "").split()).lower()
                ]
                if not hits:
                    unmatched += 1
                    print(f"  ?     no requirement mentions {needle!r}  ({why})")
                    continue
                for r, v, a in hits:
                    state = readiness(a)
                    bad = state in FORBIDDEN[group]
                    broken += bad
                    mark = "FAIL " if bad else ("  -  " if group == JUDGEMENT else "ok   ")
                    print(f"  {mark} {r.code:<7} {state or 'UNASSESSED':<28} {why}")
                    if bad or group == JUDGEMENT:
                        text = " ".join(((v.requirement_text if v else "") or "").split())
                        print(
                            textwrap.fill(
                                text[:160],
                                96,
                                initial_indent="          > ",
                                subsequent_indent="            ",
                            )
                        )
        print("\n" + "=" * 100)
        print(f"\n{broken} expectation(s) broken, {unmatched} needle(s) matched no requirement.")
        print(
            "\nA broken NOT_ANSWERABLE row is the serious kind: the product offering a\n"
            "claim this vendor cannot back. A broken ANSWERABLE row is a missed match.\n"
            "JUDGEMENT rows are for reading, not scoring."
        )
        session.commit()
        return 1 if broken else 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
