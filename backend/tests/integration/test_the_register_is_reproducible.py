"""The same tender and findings give the same register, however often it is built.

The register's model steps run at temperature zero but are still sampled, and two samples of the
same question can differ. A register that changes between builds from identical inputs changes
what ADX is asked and which questions count as already answered, so identical payloads must be
answered once and the answer reused.
"""

from __future__ import annotations

import json

import pytest

from app.gateway.base import LLMResponse
from app.gateway.deepseek import NullLLMProvider
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.services import clarification_triage as ct
from tests.integration.test_release_lifecycle import _ctx, _user

pytestmark = pytest.mark.integration


class Wobbly(NullLLMProvider):
    """Gives a different verdict every time it is asked the same thing."""

    model_id = "wobbly"

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        self.calls.append(messages)
        verdict = ("KEEP", "BIDDER_DECISION", "INTERNAL_CHECK")[len(self.calls) % 3]
        body = json.dumps({"verdicts": [{"id": 0, "verdict": verdict, "quote": ""}]})
        return LLMResponse(text=body, model_id=self.model_id, attempts=1, latency_ms=0)


def test_an_identical_request_is_answered_once_and_reused(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    provider = Wobbly()
    gateway = ModelGateway(provider, HashingEmbeddingProvider())
    payload = {"questions": [{"id": 0, "text": "How many copies?"}], "tender": "Two copies."}
    with app_sessionmaker() as session:
        session.begin()
        manager = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, manager.id, "BID_MANAGER")
        first = ct._call(session, ctx, "clarification_triage", payload, gateway)
        second = ct._call(session, ctx, "clarification_triage", payload, gateway)
        assert first == second, "the same payload must not give two different verdicts"
        assert len(provider.calls) == 1, "the second request must come from the stored answer"
        session.rollback()


def test_a_question_keeps_its_id_while_its_words_do():
    a = ct.question_id("How many copies of the proposal are required?", "8.1")
    b = ct.question_id("How many copies of the  proposal are required?", "8.1")
    c = ct.question_id("How many copies of the proposal are required?", "8.2")
    assert a == b and a != c and a.startswith("Q-")


def test_the_same_question_under_two_readings_is_asked_once():
    wa = "Web application firewall solution for ADX"
    web = "Website project hardware and software supply"
    base = {"severity": "BLOCKING", "category": "SECURITY_COMPLIANCE"}
    merged = ct.merge_branch_variants(
        [
            {
                **base,
                "branch": wa,
                "question": "What training is required beyond the two seats stated: which "
                "courses, for how many staff, on-site or vendor site, and certification?",
            },
            {
                **base,
                "branch": web,
                "question": "What training is required beyond the two seats stated: which "
                "courses, which vendor certification track, on-site or remote, how many staff?",
            },
            {**base, "branch": web, "question": "Which hosting region must be used?"},
        ]
    )
    assert len(merged) == 2
    assert merged[0]["branch"] == ""
    assert wa in merged[0]["question"] and web in merged[0]["question"]
    assert merged[1]["branch"] == web


TENDER = (
    "The bidder shall submit three (3) copies of the technical proposal and two (2) copies of "
    "the commercial proposal. The quoted total shall be inclusive of all taxes and duties. "
    "All prices shall be quoted in UAE Dirhams (AED). Training for 2 seats is required."
)


def test_facts_the_tender_states_are_answered_by_rule():
    asked = [
        {
            "question": "How many copies of the technical and commercial proposals must be "
            "submitted?",
            "verdict": "BIDDER_DECISION",
            "severity": "MINOR",
        },
        {
            "question": "In which currency must prices be quoted?",
            "verdict": "KEEP",
            "severity": "MINOR",
        },
        {"question": "Which hosting region must be used?", "verdict": "KEEP", "severity": "MINOR"},
    ]
    ct.stated_facts(asked, TENDER)
    assert (
        asked[0]["verdict"] == "ANSWERED_IN_TENDER"
        and "three (3) copies" in asked[0]["answer_quote"]
    )
    assert asked[1]["verdict"] == "ANSWERED_IN_TENDER" and "AED" in asked[1]["answer_quote"]
    assert asked[2]["verdict"] == "KEEP", "a question the tender does not answer stays open"


def test_one_matter_is_never_open_and_answered_and_never_asked_twice():
    answered = [
        {
            "question": "For clause 7.3, how many ADX staff must be trained?",
            "answer_quote": "Training for 2 seats is required.",
            "verdict": "ANSWERED_IN_TENDER",
        },
        {
            "question": "How many ADX staff require training?",
            "answer_quote": "Training for 2 seats is required.",
            "verdict": "ANSWERED_IN_TENDER",
        },
        {
            "question": "Must the quoted total be tax inclusive?",
            "answer_quote": "The quoted total shall be inclusive of all taxes and duties.",
            "verdict": "ANSWERED_IN_TENDER",
        },
    ]
    optional = [
        {
            "question": "Should the Grand Total be inclusive of all taxes and duties?",
            "severity": "MINOR",
            "verdict": "KEEP",
        },
        {"question": "Which hosting region must be used?", "severity": "MINOR", "verdict": "KEEP"},
    ]
    blocking, optional, bidder, answered = ct.reconcile_sections([], optional, [], answered)
    assert [q["question"] for q in optional] == ["Which hosting region must be used?"]
    assert sum("train" in q["question"].lower() for q in answered) == 1
    tax = [q for q in answered if "tax" in q["question"].lower()]
    assert len(tax) == 1, "the open tax question joined the answered one instead of duplicating it"


class Sampler(NullLLMProvider):
    """Answers every structured request differently each time it is asked."""

    model_id = "sampler"

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        self.calls.append(messages)
        blob = "\n".join(m.content for m in messages)
        n = len(self.calls)
        if "question_atomize" in blob or "AtomResult" in blob:
            body = {"items": []}
        elif "WorkPackageResult" in blob:
            body = {"packages": [{"name": f"Package {n}", "items": [0]}]}
        elif "EntailmentResult" in blob:
            body = {"verdicts": []}
        else:
            body = {"verdicts": [{"id": 0, "verdict": ("KEEP", "BIDDER_DECISION")[n % 2]}]}
        return LLMResponse(text=json.dumps(body), model_id=self.model_id, attempts=1, latency_ms=0)


def test_the_whole_model_driven_register_stage_builds_the_same_twice(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    provider = Sampler()
    gateway = ModelGateway(provider, HashingEmbeddingProvider())
    questions = lambda: [  # noqa: E731
        {"question": "How many copies must be submitted?", "severity": "MINOR"},
        {"question": "Which hosting region must be used?", "severity": "MINOR"},
    ]
    with app_sessionmaker() as session:
        session.begin()
        manager = _user(session, org_id, users[org_id]["manager"])
        ctx = _ctx(org_id, manager.id, "BID_MANAGER")

        def build():
            qs = ct.triage(session, ctx, questions(), TENDER, gateway=gateway)
            ct.stated_facts(qs, TENDER)
            packages = ct.work_packages(session, ctx, ["a task", "another task"], gateway=gateway)
            return json.dumps(
                {
                    "q": [
                        (q["question"], q.get("verdict"), ct.question_id(q["question"])) for q in qs
                    ],
                    "p": packages,
                },
                sort_keys=True,
            )

        first = build()
        calls_after_first = len(provider.calls)
        second = build()
        assert first == second, "identical inputs must give an identical register stage"
        assert len(provider.calls) == calls_after_first, "nothing may be sampled a second time"
        session.rollback()


COPIES_TENDER = (
    "Clause 5.2(b): the bidder shall submit 3 technical copies and 2 commercial copies, each in "
    "a separate sealed envelope. Clause 5.2(d): the CDs are enclosed as indicated above."
)
REAL_COPY_QUESTION = (
    "Clause 5.2(b) specifies 3 technical copies and 2 commercial copies, while clause 5.2(d) "
    "refers generally to 'the number of copies as indicated above' — please confirm the exact "
    "copy count."
)


def test_the_real_compound_copy_question_is_answered_by_the_tender():
    asked = [{"question": REAL_COPY_QUESTION, "verdict": "BIDDER_DECISION", "severity": "MINOR"}]
    ct.stated_facts(asked, COPIES_TENDER)
    assert asked[0]["verdict"] == "ANSWERED_IN_TENDER"
    assert "3 technical copies" in asked[0]["answer_quote"]


def test_a_question_asking_which_taxes_apply_is_not_answered_by_price_treatment():
    price = "The Total fee is inclusive of all taxes, duties, charges and levies, as applicable."
    rated = "VAT is charged at 5% on all fees."
    questions = [
        {
            "question": "What are the applicable taxes?",
            "verdict": "ANSWERED_IN_TENDER",
            "answer_quote": price,
        },
        {
            "question": "Which taxes apply to the fee?",
            "verdict": "ANSWERED_IN_TENDER",
            "answer_quote": rated,
        },
        {
            "question": "Must the quoted total be inclusive of taxes?",
            "verdict": "ANSWERED_IN_TENDER",
            "answer_quote": price,
        },
    ]
    ct.demote_unspecific_answers(questions)
    assert questions[0]["verdict"] == "KEEP" and questions[0]["partial_answer"] == price
    assert questions[1]["verdict"] == "ANSWERED_IN_TENDER", "a named tax and rate is an answer"
    assert questions[2]["verdict"] == "ANSWERED_IN_TENDER", "how the price is stated is answered"


def test_two_questions_answered_by_one_sentence_are_one_question():
    quote = "The Total fee is inclusive of all taxes, duties, charges and levies, as applicable."
    answered = [
        {
            "question": "Must the quoted total be inclusive of VAT and other levies?",
            "answer_quote": quote,
            "verdict": "ANSWERED_IN_TENDER",
        },
        {
            "question": "Should the Grand Total in Table B2-1 be stated inclusive of all taxes, "
            "duties, charges and levies?",
            "answer_quote": quote,
            "verdict": "ANSWERED_IN_TENDER",
        },
        {
            "question": "Is the bid bond refundable?",
            "answer_quote": "Bonds are refundable.",
            "verdict": "ANSWERED_IN_TENDER",
        },
    ]
    *_, answered = ct.reconcile_sections([], [], [], answered)
    assert len(answered) == 2


FULL_TENDER = (
    "# Request for Proposal\n\n"
    "Clause 5.2(b): the bidder shall submit 3 technical copies and 2 commercial copies.\n\n"
    "Clause 6.1: The Total fee is inclusive of all taxes, duties, charges and levies.\n\n"
    "Clause 6.2: All prices shall be quoted in UAE Dirhams (AED).\n\n"
    "Clause 7.3: Training for 2 seats is required.\n\n"
    "Clause 8.1: Support arrangements will be agreed after award.\n"
)


class FullSampler(NullLLMProvider):
    """Answers every register stage, differently every single time it is asked."""

    model_id = "full-sampler"

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        self.calls.append(messages)
        blob = "\n".join(m.content for m in messages)
        n = len(self.calls)
        if "ScopeResult" in blob:
            body = {"interpretations": []}
        elif "DesignGapResult" in blob:
            body = {
                "gaps": [
                    {
                        "question": f"Which hosting region applies (variant {n})?",
                        "category": "SOLUTION_DESIGN",
                        "blocks": "DESIGN",
                        "impact": "The design cannot be sized.",
                        "topic_terms": ["zzqx"],
                        "basis_quote": None,
                    }
                ]
            }
        elif "AtomResult" in blob:
            body = {"items": []}
        elif "EntailmentResult" in blob:
            body = {"verdicts": []}
        elif "WorkPackageResult" in blob:
            body = {"packages": [{"name": f"Package number {n}", "items": [0]}]}
        else:
            body = {"verdicts": [{"id": 0, "verdict": ("KEEP", "BIDDER_DECISION")[n % 2]}]}
        return LLMResponse(text=json.dumps(body), model_id=self.model_id, attempts=1, latency_ms=0)


def test_the_complete_register_is_the_same_twice(app_sessionmaker, seeded_orgs):
    from app.core.context import ExecutionContext
    from app.db.session import apply_tenant_context
    from app.ingestion.safety import EicarAwareScanner
    from app.models.enums import ConfidentialityClass, DocumentSide
    from app.services import bids, clarification_register, ingestion
    from app.services.storage import InMemoryStorage

    org_id = seeded_orgs[0]
    provider = FullSampler()
    gateway = ModelGateway(provider, HashingEmbeddingProvider())
    with app_sessionmaker() as session:
        session.begin()
        ctx = ExecutionContext(org_id=org_id)
        apply_tenant_context(session, ctx)
        opportunity = bids.create_opportunity(
            session, ctx, customer_name="Register Co", tender_name="Reproducible register"
        )
        bid = bids.promote_to_bid(session, ctx, opportunity_id=opportunity.id)
        bids.ensure_intake_open(session, ctx, bid_id=bid.id)
        storage = InMemoryStorage()
        version = ingestion.register_upload(
            session,
            ctx,
            data=FULL_TENDER.encode(),
            filename="tender.md",
            side=DocumentSide.TENDER,
            bid_id=bid.id,
            confidentiality_class=ConfidentialityClass.CONFIDENTIAL,
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

        def serialised() -> str:
            data = clarification_register.build(session, ctx, bid.id, gateway=gateway)
            return json.dumps(data, sort_keys=True, default=str)

        first = serialised()
        used = len(provider.calls)
        second = serialised()
        assert first == second, "the same tender and findings must give the identical register"
        assert len(provider.calls) == used, "no stage may be sampled a second time"
        assert used >= 3, "the register's model stages really ran"
        assert "hosting region" in first and '"question_id"' in first
        session.rollback()
