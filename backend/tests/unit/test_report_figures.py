import io
from types import SimpleNamespace

import docx

from app.services import diagram_spec as ds
from app.services import report_figures as rf
from app.services.diagram_png import render_png
from app.services.release import _render_docx


def spec(*labels):
    return ds.parse_spec(
        {
            "nodes": [{"id": f"n{i}", "label": label} for i, label in enumerate(labels)],
            "edges": [{"source": "n0", "target": "n1"}] if len(labels) > 1 else [],
        }
    )


def test_a_diagram_sits_after_the_paragraph_that_talks_about_the_same_things():
    paragraphs = [
        {"kind": "BODY", "text": "We run penetration testing before launch."},
        {
            "kind": "BODY",
            "text": "Applicants upload documents which are stored in the document store.",
        },
        {"kind": "BODY", "text": "Training is given to administrators."},
    ]
    subject = rf._words("Document upload flow Upload documents Document store")
    assert rf._place(paragraphs, subject) == 2
    assert rf._place(paragraphs, set()) == 1


def test_a_marker_becomes_a_picture_with_a_numbered_caption():
    png = render_png(spec("Core OMS", "Document store"))

    class Store:
        def exists(self, key):
            return key == "k"

        def get(self, key):
            return png

    section = SimpleNamespace(number_label="1", title="Chapter")
    text = "First.\n\n" + rf.marker("k", "How the parts connect.")
    data = _render_docx("T", [(section, text)], storage=Store())
    document = docx.Document(io.BytesIO(data))
    assert len(document.inline_shapes) == 1
    assert any(p.text == "Figure 1: How the parts connect." for p in document.paragraphs)
    assert any(p.style.name == "Caption" for p in document.paragraphs)
    assert 'descr="Diagram: How the parts connect."' in docx_xml(data)


def docx_xml(data: bytes) -> str:
    import zipfile

    return zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()


def test_a_wide_figure_gets_its_own_landscape_page_and_tables_get_header_rows():
    wide = ds.parse_spec(
        {
            "nodes": [{"id": f"n{i}", "label": f"Step {i}"} for i in range(4)],
            "edges": [{"source": f"n{i}", "target": f"n{i + 1}"} for i in range(3)],
        }
    )
    png = render_png(wide)

    class Store:
        def exists(self, key):
            return True

        def get(self, key):
            return png

    section = SimpleNamespace(number_label="1", title="Chapter")
    text = "| H1 | H2 |\n| a | b |\n\n" + rf.marker("k", "Wide one") + "\n\nAfter."
    data = _render_docx("T", [(section, text)], storage=Store())
    document = docx.Document(io.BytesIO(data))
    orientations = [s.orientation for s in document.sections]
    assert len(document.sections) == 3 and orientations[1] != orientations[0]
    assert orientations[2] == orientations[0]
    assert "tblHeader" in docx_xml(data)


def test_the_customers_assumptions_keep_only_what_affects_them(tmp_path):
    import json
    import uuid

    from app.services import report_consolidate as rc

    class Store:
        def exists(self, key):
            return True

        def get(self, key):
            return json.dumps(
                {
                    "items": [
                        "Jadwa gives access.",
                        "Price excludes VAT.",
                        "We pick a queue.",
                        "x",
                    ],
                    "categories": [
                        "CUSTOMER_DEPENDENCY",
                        "COMMERCIAL",
                        "TECHNICAL_DECISION",
                        "INTERNAL_DRAFTING",
                    ],
                }
            ).encode()

    ctx = SimpleNamespace(org_id=uuid.uuid4())
    customer = rc.register_text(ctx, bid_id=uuid.uuid4(), storage=Store(), submission=True)
    review = rc.register_text(ctx, bid_id=uuid.uuid4(), storage=Store(), submission=False)
    assert "We pick a queue" not in customer and "Price excludes VAT" in customer
    # Internal drafting notes are the team's, in any copy; they are reported in the findings.
    assert "We pick a queue" in review and "Internal drafting note" not in review


def test_customer_copy_shows_customer_statuses_not_internal_ones():
    from app.services.report_compliance import customer_status

    # A promise to provide something later is never "answered": the same words inside and out.
    assert customer_status("Answered; blocked") == "Answered; blocked"
    assert customer_status("Priced separately: option A") == "Priced separately"
    assert customer_status("Conditional") == "Comply subject to stated dependency"
    assert customer_status("Open") == "Open"


def test_a_finding_folds_into_the_deliverable_read_from_the_same_words():
    from app.services.release import _tender_place

    here = {"document_version_id": "d", "char_start": 10, "char_end": 20, "clause_ref": "4"}
    other = {"document_version_id": "d", "char_start": 10, "char_end": 21}
    assert _tender_place(here) == _tender_place({**here, "clause_ref": "x"})
    assert _tender_place(here) != _tender_place(other)
    assert _tender_place({"document_version_id": "d"}) is None
    assert _tender_place(None) is None


def test_an_attached_document_keeps_its_words_and_turns_table_rows_into_a_table():
    from types import SimpleNamespace as N

    from app.models.enums import BlockType
    from app.services.report_attachments import _body

    blocks = [
        N(block_type=BlockType.PARAGRAPH, text="Notice kept as written."),
        N(block_type=BlockType.TABLE, text="Scenario | Sessions"),
        N(block_type=BlockType.TABLE_ROW, text="Scenario: Browse | Sessions: 300"),
        N(block_type=BlockType.LIST_ITEM, text="One point"),
    ]
    text = _body(blocks)
    assert "Notice kept as written." in text
    assert "| Scenario | Sessions |\n| Browse | 300 |" in text
    assert "- One point" in text


def test_a_document_that_says_it_must_be_verified_is_not_attached():
    from app.services.report_attachments import UNVERIFIED

    assert UNVERIFIED.search("EMPLOYMENT AND CONSENT MUST BE VERIFIED. These profiles reflect")
    assert not UNVERIFIED.search("Named staff available for proposal submission.")


def test_fixture_notice_is_recognised():
    from app.services.report_attachments import FIXTURE

    assert FIXTURE.search("FICTIONAL TEST FIXTURE. TTS Technology Solutions LLC does not exist")
    assert not FIXTURE.search("Rania Haddad - Solution Architect, 16 years")


def test_a_statement_that_quotes_fixture_only_facts_is_tainted():
    from app.services.report_attachments import facts_in, tainted

    facts = {"Rania Haddad": "25_CVs", "14,200": "21_Ref", "Al Waha Capital": "21_Ref"}
    assert {"Rania Haddad", "14,200"} <= facts_in("Rania Haddad led it; 14,200 applications.")
    # A long name written short ("Al Waha") is the same fact.
    assert tainted("Al Waha Capital processed 14,200 applications.", facts) == [
        "14,200",
        "Al Waha",
        "Al Waha Capital",
    ]
    # One shared fact is not enough to call a statement derived.
    assert tainted("We know Al Waha Capital.", facts) == []
    assert tainted("The portal supports Arabic.", facts) == []


def test_signers_in_different_documents_conflict_only_with_nothing_in_common():
    from app.services.report_extras import signer_conflict, signers_named

    names = ["Khalid Al Rashid", "Mariam Suleiman"]
    assert signers_named("The proposal is signed by Khalid Al Rashid, MD.", names) == {
        "Khalid Al Rashid"
    }
    assert signers_named("Khalid Al Rashid is our fixed asset lead.", names) == set()
    assert signer_conflict({"T": {"Khalid Al Rashid"}, "C": {"Mariam Suleiman"}})
    assert not signer_conflict(
        {"T": {"Khalid Al Rashid", "Mariam Suleiman"}, "C": {"Mariam Suleiman"}}
    )
    assert not signer_conflict({"T": set(), "C": {"Mariam Suleiman"}})


def test_job_titles_tools_and_section_numbers_do_not_taint():
    from app.services.report_attachments import tainted

    facts = {
        "Security Lead": "25",
        "Apache JMeter": "23",
        "Commercial Director": "29",
        "1.12": "23",
        "Nabil Farouk": "25",
    }
    assert tainted("The Security Lead used Apache JMeter 1.12.", facts) == []
    assert tainted("Our Commercial Director and Security Lead sign.", facts) == []
    assert tainted("Nabil Farouk is our Security Lead.", facts) == ["Nabil Farouk", "Security Lead"]


def test_separately_quoted_items_are_outside_the_fixed_price():
    from app.services.report_extras import build_price_text

    rates = {"Developer": 1000, "Architect": 2000}
    lines = [{"phase": "Build", "role": "Developer", "days": 10}]
    separate = [
        {"ref": "R-104", "item": "Signature system", "role": "Developer", "days": 3},
        {"ref": "R-104", "item": "Signature system", "role": "Architect", "days": 1},
    ]
    text, total = build_price_text(lines, rates, [], [], [], currency="AED", separate=separate)
    assert total == 10000  # the fixed price leaves them out
    assert "Items quoted separately" in text
    assert "| Signature system (R-104) | 4 | 5,000 |" in text
    assert "other than the items quoted separately below" in text
    plain, plain_total = build_price_text(lines, rates, [], [], [], currency="AED")
    assert plain_total == 10000 and "quoted separately" not in plain


def test_separate_lines_must_match_what_the_tender_asked_for():
    from app.schemas.report_extras import FinancialDraft, PriceLine, SeparateLine
    from app.services.report_extras import check_separate

    base = [PriceLine(phase="p", role="Developer", days=1, basis="b")] * 3
    ok = FinancialDraft(
        lines=base,
        separate=[SeparateLine(ref="r-104", item="x", role="developer", days=2, basis="b")],
    )
    assert check_separate(ok, {"Developer": 1}, {"R-104"}) == []
    assert check_separate(ok, {"Developer": 1}, {"R-104", "R-105"}) != []
    assert check_separate(ok, {"Developer": 1}, set()) != []
    assert check_separate(ok, {"Tester": 1}, {"R-104"}) != []
