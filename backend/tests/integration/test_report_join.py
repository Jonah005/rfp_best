"""Joining approved answers into report prose, and refusing a rewrite that adds a fact."""

from __future__ import annotations

import json
import re

import pytest

from app.gateway.base import LLMResponse
from app.gateway.deepseek import NullLLMProvider
from app.gateway.embeddings import HashingEmbeddingProvider
from app.gateway.gateway import ModelGateway
from app.models.enums import DocumentStream, OutputMode
from app.services import release, report_edit
from app.services.storage import InMemoryStorage
from tests.integration.test_the_final_output import (
    StructureReader,
    _bid_with_two_answers,
    _streams,
    _text,
)

pytestmark = pytest.mark.integration


def _payload(messages):
    blob = "\n".join(m.content for m in messages)
    body = re.search(
        r"<untrusted_document_content>\n(.*?)\n</untrusted_document_content>", blob, re.S
    )
    return json.loads(body.group(1)) if body else {}


class Editor(NullLLMProvider):
    """Rewrites faithfully, or invents a figure when told to."""

    model_id = "scripted-editor"

    def __init__(self, invent=False):
        super().__init__("{}")
        self.invent = invent

    def generate(self, messages, *, temperature=None, max_tokens=None, timeout=None):
        system = messages[0].content
        if "editor of a bid proposal" not in system:
            return StructureReader().generate(messages)
        data = _payload(messages)
        statements = data.get("statements", [])
        refs = [s["ref"] for s in statements]
        text = " ".join(s["text"] for s in statements)
        if self.invent:
            text += " We have done this 99 times."
        paragraphs = [{"text": text, "kind": "BODY", "source_refs": refs}]
        if data.get("chapter") != "Executive Summary":
            paragraphs.insert(
                0, {"text": "This topic is set out below.", "kind": "CONNECTIVE", "source_refs": []}
            )
        return LLMResponse(
            text=json.dumps({"paragraphs": paragraphs}), model_id=self.model_id, latency_ms=0
        )


def test_answers_are_joined_and_the_summary_is_written(app_sessionmaker, seeded_orgs, users):
    org_id = seeded_orgs[0]
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _bid_with_two_answers(session, org_id, users)
        gateway = ModelGateway(Editor(), HashingEmbeddingProvider())
        release.derive_structure(session, ctx, bid_id=bid.id, gateway=gateway)
        done = {"remaining": 1}
        for _ in range(6):
            done = report_edit.edit_bid(
                session, ctx, bid_id=bid.id, limit=1, gateway=gateway, storage=storage
            )
            if done["remaining"] == 0 and done["summary"] == "done":
                break
        assert done["failed"] == 0 and done["summary"] == "done"
        versions = _streams(
            session,
            release.assemble(
                session,
                ctx,
                bid_id=bid.id,
                output_mode=OutputMode.REVIEW,
                storage=storage,
                join=True,
            ),
        )
        technical = _text(storage, versions[DocumentStream.TECHNICAL])
        assert "This topic is set out below." in technical
        assert "OWASP ASVS" in technical
        assert release.NO_CONTENT not in technical, "the Executive Summary is now written"
        session.rollback()


def test_a_rewrite_that_adds_a_fact_falls_back_to_the_approved_wording(
    app_sessionmaker, seeded_orgs, users
):
    org_id = seeded_orgs[0]
    storage = InMemoryStorage()
    with app_sessionmaker() as session:
        session.begin()
        ctx, bid = _bid_with_two_answers(session, org_id, users)
        gateway = ModelGateway(Editor(invent=True), HashingEmbeddingProvider())
        release.derive_structure(session, ctx, bid_id=bid.id, gateway=gateway)
        out = report_edit.edit_bid(
            session, ctx, bid_id=bid.id, limit=5, gateway=gateway, storage=storage
        )
        assert out["failed"] >= 1 and out["edited"] == 0
        versions = _streams(
            session,
            release.assemble(
                session,
                ctx,
                bid_id=bid.id,
                output_mode=OutputMode.REVIEW,
                storage=storage,
                join=True,
            ),
        )
        technical = _text(storage, versions[DocumentStream.TECHNICAL])
        assert "99 times" not in technical
        assert "OWASP ASVS" in technical, "the approved wording is printed instead"
        session.rollback()
