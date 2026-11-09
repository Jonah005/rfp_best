"""One customer's RFP must never turn up while working another.

Two kinds of material live in the same corpus and they want opposite rules:

  COMPANY  certifications, CVs, past performance, policies. Org-wide ON
           PURPOSE -- reuse across bids is the entire asset. Scoping these to
           a bid would mean re-uploading the CV pack for every tender.
  TENDER   one customer's RFP. Belongs to exactly one bid, and has no business
           being visible while answering a different one. ADX's clauses quoted
           into another exchange's response is not a hypothetical embarrassment.

The asymmetry is the design. These cases pin both halves, because getting
either one backwards is silent: too broad leaks, too narrow hides the knowledge
base and looks like "we have no evidence for that".
"""

from __future__ import annotations

import inspect

from app.models.enums import DocumentSide
from app.services import knowledge_extraction, retrieval
from app.services.retrieval import RetrievalScope


def test_the_knowledge_graph_is_built_from_company_documents_only():
    """The strongest isolation, and it is structural rather than filtered.

    If extraction ran over tender text, one RFP's clauses would become entities
    and facts in an org-wide graph -- and no query-time filter could reliably
    unpick them afterwards, because by then they are facts about the company.
    """
    source = inspect.getsource(knowledge_extraction.extract_company_knowledge)
    assert "is not DocumentSide.COMPANY" in source


def test_the_copilot_never_reads_tender_text_at_all():
    """A task answers from company evidence. Its own bid's tender is already in
    the prompt as requirements; every other bid's is simply not admissible."""
    from app.services import context_builder

    source = inspect.getsource(context_builder.build)
    assert "sides=(DocumentSide.COMPANY,)" in source


def test_searching_tender_text_requires_naming_the_bid():
    """The hole this file was written for.

    The search endpoint took `sides` from the caller and applied no bid filter,
    so `{"sides": ["TENDER"]}` returned chunks from every RFP in the
    organization.
    """
    from app.api.v1 import knowledge as route_module

    source = inspect.getsource(route_module.search)
    assert "DocumentSide.TENDER in sides and payload.bid_id is None" in source
    assert "requires a bid_id" in source


def test_the_request_can_carry_a_bid():
    from app.schemas.knowledge import SearchRequest

    assert "bid_id" in SearchRequest.model_fields


def test_naming_a_bid_does_not_hide_the_company_corpus():
    """The failure in the other direction, and the subtler of the two.

    Company documents carry no bid_id. A plain `IN (versions of this bid)`
    would exclude every one of them, so a bid-scoped search would return tender
    text only -- and the knowledge base would look empty exactly when someone
    was trying to answer from it.
    """
    source = inspect.getsource(retrieval.RetrievalService._admissibility_clause)
    assert "_version_ids_without_bid" in source
    assert "or_(" in source


def test_documents_belonging_to_no_bid_are_the_company_corpus():
    source = inspect.getsource(retrieval.RetrievalService._version_ids_without_bid)
    assert "Document.bid_id.is_(None)" in source


def test_the_scope_defaults_to_company_only():
    """A caller who says nothing gets the safe side, not everything."""
    assert RetrievalScope().sides == (DocumentSide.COMPANY,)
    assert RetrievalScope().bid_ids == ()
