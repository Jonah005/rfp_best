"""Parsing and chunking: what must survive from file to retrieval unit."""

from __future__ import annotations

import io

import pytest

from app.ingestion.chunker import chunk_blocks, estimate_tokens
from app.ingestion.parsers import (
    _page_segments,
    _recurring_lines,
    detect_clause_ref,
    looks_like_heading,
    parse_document,
)
from app.models.enums import BlockType

# --- clause detection -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6.1 Testing window", "6.1"),
        ("Section 4.2.3 Scope of work", "4.2.3"),
        ("Clause 12 Payment terms", "12"),
        ("Appendix A.3 Required forms", "A.3"),
        ("2024 was a strong year for us", None),
        ("The bidder shall provide references.", None),
        ("100000 units delivered", None),
    ],
)
def test_clause_reference_detection_is_conservative(text, expected):
    """A false clause_ref is worse than a missing one: precedence keys on it."""
    assert detect_clause_ref(text) == expected


def test_heading_heuristic_rejects_sentences():
    assert looks_like_heading("6.1 Testing Window")
    assert not looks_like_heading("Testing must occur between 01:00 and 05:00 local time.")


# --- markdown / plain text --------------------------------------------------


def test_markdown_headings_nest_and_carry_down_to_paragraphs():
    md = b"# 6 Scope\n\nWork happens.\n\n## 6.1 Window\n\nBetween 01:00 and 05:00.\n"
    blocks = parse_document(md, "rfp.md", "text/markdown").blocks
    paragraph = [b for b in blocks if b.text.startswith("Between")][0]
    assert paragraph.heading_path == ["6 Scope", "6.1 Window"]
    assert paragraph.block_type is BlockType.PARAGRAPH


def test_ordinals_and_char_offsets_are_assigned():
    blocks = parse_document(b"One.\n\nTwo.\n\nThree.\n", "a.txt", "text/plain").blocks
    assert [b.ordinal for b in blocks] == list(range(len(blocks)))
    assert all(b.char_end > b.char_start for b in blocks)


# --- tables -----------------------------------------------------------------


def test_csv_rows_are_labelled_with_their_column_names():
    """A retrieved row must be self-describing, not a bare list of values."""
    data = b"Client,Sector,Country\nMinistry of Interior,Government,UAE\n"
    blocks = parse_document(data, "Referenceable Clients.csv", "text/csv").blocks
    row = [b for b in blocks if b.block_type is BlockType.TABLE_ROW][0]
    assert "Client: Ministry of Interior" in row.text
    assert "Sector: Government" in row.text
    assert row.table_name == "Referenceable Clients"
    assert row.row_index == 1


def test_xlsx_sheets_become_named_tables():
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Referenceable Clients"
    sheet.append(["Client", "Sector", "Year"])
    sheet.append(["Ministry of Interior", "Government", 2024])
    buffer = io.BytesIO()
    workbook.save(buffer)

    blocks = parse_document(
        buffer.getvalue(),
        "refs.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).blocks
    rows = [b for b in blocks if b.block_type is BlockType.TABLE_ROW]
    assert rows and rows[0].table_name == "Referenceable Clients"
    assert "Client: Ministry of Interior" in rows[0].text


def test_docx_headings_and_tables_are_preserved():
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("Company Certifications", level=1)
    document.add_paragraph("We hold the following accreditations.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Certification"
    table.cell(0, 1).text = "Expiry"
    table.cell(1, 0).text = "CREST"
    table.cell(1, 1).text = "2026-10-14"
    buffer = io.BytesIO()
    document.save(buffer)

    blocks = parse_document(
        buffer.getvalue(),
        "certs.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ).blocks
    assert any(b.block_type is BlockType.HEADING for b in blocks)
    row = [b for b in blocks if b.block_type is BlockType.TABLE_ROW][0]
    assert "Certification: CREST" in row.text


def test_unsupported_mime_has_no_parser():
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError, match="No parser"):
        parse_document(b"x", "f.bin", "application/octet-stream")


# --- chunking ---------------------------------------------------------------


def test_each_table_row_becomes_its_own_chunk_carrying_the_header():
    data = b"Client,Sector\nMinistry,Government\nAcme Bank,Finance\n"
    blocks = parse_document(data, "Refs.csv", "text/csv").blocks
    chunks = chunk_blocks(blocks)
    row_chunks = [c for c in chunks if "Client: " in c.text]
    assert len(row_chunks) == 2
    for chunk in row_chunks:
        assert "Table: Refs" in chunk.text
        assert "Columns: Client | Sector" in chunk.text


def test_chunks_never_span_two_clauses():
    """A chunk covering 6.1 and 6.2 could cite neither."""
    md = (
        b"## 6.1 Window\n\nBetween 01:00 and 05:00.\n\n"
        b"## 6.2 Retesting\n\nOne retest is included.\n"
    )
    chunks = chunk_blocks(parse_document(md, "r.md", "text/markdown").blocks)
    for chunk in chunks:
        refs = {r for r in ("6.1", "6.2") if r in chunk.text}
        assert len(refs) <= 1, f"chunk spans clauses {refs}: {chunk.text!r}"


def test_chunks_retain_provenance():
    md = b"# 4 Team\n\n" + (b"The team is experienced. " * 20) + b"\n"
    chunks = chunk_blocks(parse_document(md, "r.md", "text/markdown").blocks)
    assert chunks
    for chunk in chunks:
        assert chunk.heading_path, "a chunk without a heading path cannot be cited"
        assert chunk.block_ordinals


def test_oversized_blocks_are_split_within_budget():
    huge = ("word " * 4000).encode()
    chunks = chunk_blocks(parse_document(huge, "big.txt", "text/plain").blocks, max_tokens=200)
    assert len(chunks) > 1
    assert all(c.token_count <= 400 for c in chunks)


def test_token_estimate_is_monotonic():
    assert estimate_tokens("a") < estimate_tokens("a" * 100) < estimate_tokens("a" * 1000)


# --- page segmentation, from a real tender that produced zero clauses -------
#
# A live 32-page RFP parsed into 50 blocks, none of which carried a clause_ref.
# The whole understanding pass depends on those refs: precedence keys on
# clause_ref, requirement extraction only reads blocks that won a clause
# resolution, so the run reported 59 deliverables and 26 ambiguities alongside
# 0 requirements and 0 model calls — and looked like a model failure rather
# than a parsing one.
#
# Two causes, both reproduced below: pdfplumber returned page text with no
# blank lines (so the whole page became one block), and every page opened with
# a running header (so even a page starting at a clause number did not match,
# because clause detection anchors at the start of the block).



def test_a_page_with_no_blank_lines_still_splits_at_clause_numbers():
    page = (
        "5.1 Notice to Bid\n"
        "Firms must notify ADX of their intention to respond.\n"
        "5.2 Bid Response\n"
        "All bid responses shall be carefully composed.\n"
    )
    segments = _page_segments(page, drop=set())
    assert len(segments) == 2
    assert detect_clause_ref(segments[0]) == "5.1"
    assert detect_clause_ref(segments[1]) == "5.2"


def test_a_running_header_does_not_hide_the_clause_number_behind_it():
    """The header is why a document full of numbered clauses parsed as none."""
    header = "ADX WAF RFP 2015"
    pages = [f"{header}\n5.{n} Some clause\nBody text here.\n" for n in range(1, 9)]
    running = _recurring_lines(pages, page_count=len(pages))
    assert header in running

    segments = _page_segments(pages[0], drop=running)
    assert segments, "the page must still yield content once the header is dropped"
    assert detect_clause_ref(segments[0]) == "5.1"
    assert not segments[0].startswith(header)


def test_body_prose_repeated_across_pages_is_not_mistaken_for_furniture():
    """Only short, repeated lines are page furniture."""
    sentence = (
        "The supplier shall maintain professional indemnity insurance for the "
        "duration of the agreement and provide evidence on request."
    )
    pages = [f"{sentence}\nPage {n}\n" for n in range(1, 9)]
    running = _recurring_lines(pages, page_count=len(pages))
    assert sentence not in running


def test_a_table_of_contents_line_is_not_a_clause():
    """A contents entry would compete with the real clause during precedence."""
    assert detect_clause_ref("5.1 Notice to Bid .......................... 11") is None
    assert detect_clause_ref("5.1 Notice to Bid") == "5.1"


def test_a_short_document_keeps_every_line():
    """Under three pages there is no way to tell a header from content."""
    pages = ["Title\nBody.\n", "Title\nMore body.\n"]
    assert _recurring_lines(pages, page_count=2) == set()
