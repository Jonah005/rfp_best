"""A customer question holds up the requirements it is about, and no others.

Measured on the Wadi Lumen fixture against the real Jadwa tender. Questions
were linked to every requirement drawn from the paragraph they cited, and an
open question demotes an answerable row to NEEDS_CLIENT_CLARIFICATION. So
"which third-party controls are acceptable?" held back "develop in C#", "which
Al-Elm services?" held back "support English and Arabic", and 28 of 85 rows
waited on the customer -- most of them over a question about a neighbouring
clause. A rerun that regrouped the questions took READY from 5 to 0 with no
change to the evidence.

`ambiguity_analysis@v3` is shown the requirements under each block and names
the ones an issue concerns. `concerned_requirements` keeps only codes it was
actually shown, and falls back to the whole block when it names none -- a
question on too many rows is visible and arguable; one on no row is lost.

The model here is scripted to answer from the prompt it is sent, so these tests
also prove the requirements actually reach the prompt: the reader can only name
a code it can see.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
from sqlalchemy import select

from app.core.context import ExecutionContext
from app.db.session import apply_tenant_context
from app.models import ClarificationIssue, ClarificationRequirement, Requirement
from app.models.enums import DocumentType
from app.services import requirements, tender_analysis
from app.services.tender_analysis import concerned_requirements
from tests.integration.test_tender_intelligence import (
    _new_bid,
    _upload,
    env,  # noqa: F401 - pytest fixture
    gateway_for,
    ordinals_in,
)

pytestmark = pytest.mark.integration

TENDER = b"""# Technical requirements

The solution must be developed in Microsoft C# with jQuery. Any third party controls used should be licensed under the vendor and the customer is to have rights to use them.
"""  # noqa: E501 - one paragraph on purpose: two requirements, one block

LANGUAGE = "developed in Microsoft C#"
LICENCE = "third party controls"


def _two_from_one_block(prompt: str) -> dict:
    """Both requirements from the paragraph, as Jadwa's R-053..R-057 were."""
    block = max(ordinals_in(prompt))
    return {
        "requirements": [
            {
                "requirement_text": f"The solution must be {LANGUAGE} with jQuery.",
                "normalized_requirement": "language-csharp",
                "category": "TECHNICAL",
                "is_mandatory": True,
                "is_scored": False,
                "risk": "MEDIUM",
                "source_block_ordinal": block,
                "confidence": 0.9,
            },
            {
                "requirement_text": f"Any {LICENCE} used must be licensed under the vendor.",
                "normalized_requirement": "third-party-licence",
                "category": "LEGAL",
                "is_mandatory": True,
                "is_scored": False,
                "risk": "MEDIUM",
                "source_block_ordinal": block,
                "confidence": 0.9,
            },
        ],
        "no_requirement_block_ordinals": [o for o in ordinals_in(prompt) if o != block],
    }


def _shown(prompt: str) -> dict[str, str]:
    """code -> text, for every requirement the prompt carried."""
    found = {}
    for code, text in re.findall(r'"code":\s*"([^"]+)",\s*"text":\s*"([^"]*)"', prompt):
        found[code] = text
    return found


def _licence_question(concerns_from):
    """A reader that asks about the licence and says which rows it concerns."""

    def reply(prompt: str) -> dict:
        assert '"document_outline"' in prompt, "the outline must still reach every batch"
        shown = _shown(prompt)
        block = max(ordinals_in(prompt))
        return {
            "issues": [
                {
                    "issue_type": "UNDEFINED_FIELD",
                    "description": "Which third-party controls are acceptable is not stated.",
                    "severity": "MINOR",
                    "recommended_question": "Which third-party controls are acceptable?",
                    "source_block_ordinal": block,
                    "concerns": concerns_from(shown),
                    "confidence": 0.9,
                }
            ]
        }

    return reply


def _scene(session, ctx, fixture_env, concerns_from):
    bid = _new_bid(session, ctx, name="Scoped questions")
    _upload(
        session,
        ctx,
        fixture_env,
        data=TENDER,
        filename=f"rfp-{uuid.uuid4().hex[:6]}.md",
        bid_id=bid.id,
        doc_type=DocumentType.RFP,
    )
    requirements.extract_requirements(
        session,
        ctx,
        bid_id=bid.id,
        gateway=gateway_for({"requirement_decomposition": _two_from_one_block}),
    )
    tender_analysis.analyze_ambiguity(
        session,
        ctx,
        bid_id=bid.id,
        gateway=gateway_for({"ambiguity_analysis": _licence_question(concerns_from)}),
    )
    rows = (
        session.execute(
            select(Requirement).where(
                Requirement.org_id == ctx.org_id, Requirement.bid_id == bid.id
            )
        )
        .scalars()
        .all()
    )
    by_topic = {("licence" if LICENCE in _text(session, r) else "language"): r for r in rows}
    assert set(by_topic) == {"licence", "language"}, "decomposition did not produce both"
    issue = (
        session.execute(
            select(ClarificationIssue).where(
                ClarificationIssue.org_id == ctx.org_id,
                ClarificationIssue.bid_id == bid.id,
                ClarificationIssue.recommended_question.is_not(None),
            )
        )
        .scalars()
        .one()
    )
    linked = set(
        session.execute(
            select(ClarificationRequirement.requirement_id).where(
                ClarificationRequirement.clarification_id == issue.id
            )
        ).scalars()
    )
    return by_topic, linked


def _text(session, requirement) -> str:
    from app.models import RequirementVersion

    version = session.get(RequirementVersion, requirement.current_version_id)
    return (version.requirement_text if version else "") or ""


def _code_for(shown: dict[str, str], needle: str) -> list[str]:
    return [code for code, text in shown.items() if needle in text]


def test_a_licence_question_does_not_hold_up_the_language_requirement(app_sessionmaker, env):  # noqa: F811
    """The whole point, and the Jadwa R-056 case in miniature.

    Two requirements from one paragraph. The question is about the licence; the
    C# requirement must not wait on the customer for it -- and the backfill that
    runs at the end of the same analysis must not quietly widen it back.
    """
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        by_topic, linked = _scene(s, ctx, env, lambda shown: _code_for(shown, LICENCE))

        assert by_topic["licence"].id in linked
        assert by_topic["language"].id not in linked, (
            "a question about third-party licences is holding up the C# requirement"
        )
        s.rollback()


def test_a_question_that_names_nothing_still_reaches_its_paragraph(app_sessionmaker, env):  # noqa: F811
    """The fallback. A question on no row is the 918-unreachable failure."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        by_topic, linked = _scene(s, ctx, env, lambda shown: [])

        assert linked == {by_topic["licence"].id, by_topic["language"].id}
        s.rollback()


def test_a_code_the_model_was_not_shown_moves_nothing(app_sessionmaker, env):  # noqa: F811
    """The model names; the code bounds. It can narrow a link, never redirect it."""
    org, *_ = env
    with app_sessionmaker() as s:
        s.begin()
        ctx = ExecutionContext(org_id=org)
        apply_tenant_context(s, ctx)
        by_topic, linked = _scene(s, ctx, env, lambda shown: ["R-999"])

        assert linked == {by_topic["licence"].id, by_topic["language"].id}
        s.rollback()


# --- the bounding rule on its own --------------------------------------------


A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
CANDIDATES = [(A, "R-053"), (B, "R-056"), (C, "R-057")]


@pytest.mark.parametrize(
    ("concerns", "expected", "narrowed"),
    [
        (["R-053"], [A], True),
        (["r-053 ", "R-057"], [A, C], True),  # case and spacing are not meaning
        (["R-053", "R-999"], [A], True),  # an unshown code is dropped, the rest kept
        ([], [A, B, C], False),
        (["R-999"], [A, B, C], False),
        (["", "  "], [A, B, C], False),
    ],
)
def test_concerned_requirements(concerns, expected, narrowed):
    assert concerned_requirements(CANDIDATES, concerns) == (expected, narrowed)


def test_no_candidates_links_nothing():
    assert concerned_requirements([], ["R-001"]) == ([], False)


def test_the_payload_carries_requirements_only_when_asked():
    """Other callers of `_payload` must send exactly what they sent before, or
    every cached answer for them is silently invalidated."""
    from types import SimpleNamespace

    block = SimpleNamespace(
        ordinal=3,
        block_type=SimpleNamespace(value="PARAGRAPH"),
        clause_ref="1",
        heading_path=[],
        table_name=None,
        page_from=1,
        text="x",
    )
    plain = tender_analysis._payload([block], [])
    assert '"requirements"' not in plain
    scoped = tender_analysis._payload(
        [block], [], requirements_by_ordinal={3: [{"code": "R-001", "text": "t"}]}
    )
    body = json.loads(scoped.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert body["blocks"][0]["requirements"] == [{"code": "R-001", "text": "t"}]
