"""A table belongs to the heading above it, not to the last heading in the file.

`DocxParser` drained `document.paragraphs` to completion and only then read
`document.tables`. python-docx exposes those as two independent collections, so
by the time the first table was read the heading path had already been wound
forward to the end of the document. Every table in every company document was
therefore filed under whichever heading came last -- while the line above it
claimed "the nearest preceding heading is the best available table name".

The damage was not cosmetic and did not look like a parser bug. In the employee
CV pack it put every person's table under the final section heading, so a
historical CV surfaced as "blank current-CV update form" and invented
demonstration figures surfaced under "bank-letter request template". Evidence
extraction, classification, retrieval and the copilot all read that association
as fact, and none of them could recover it: by then the person and their table
were in different sections.

The tests build real documents in the shape that broke -- alternating headings
and tables -- because that shape is the whole bug and a single-heading fixture
passes against the defect.
"""

from __future__ import annotations

import io

import pytest

docx = pytest.importorskip("docx")

from app.ingestion.parsers import DocxParser  # noqa: E402
from app.models.enums import BlockType  # noqa: E402

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def build(sections: list[tuple[str, list[list[str]] | None]]) -> bytes:
    """A document of (heading, optional table) pairs, in that order."""
    document = docx.Document()
    for heading, rows in sections:
        document.add_heading(heading, level=1)
        if rows:
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    table.cell(r, c).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def parse(data: bytes):
    return DocxParser().parse(data, "pack.docx").blocks


def test_each_table_keeps_the_heading_it_sits_under():
    """The bug, stated directly. Two headings, one table each: the old parser
    filed both under the second heading."""
    data = build(
        [
            ("Employee CV Pack", [["Name", "Role"], ["Wilma", "Senior Software Developer"]]),
            ("Bank Letter Request Template", [["Field", "Value"], ["Amount", "[TO BE FILLED]"]]),
        ]
    )
    tables = {
        block.text: block.table_name
        for block in parse(data)
        if block.block_type in (BlockType.TABLE, BlockType.TABLE_ROW)
    }
    assert tables["Name | Role"] == "Employee CV Pack"
    assert tables["Name: Wilma | Role: Senior Software Developer"] == "Employee CV Pack"
    assert tables["Field | Value"] == "Bank Letter Request Template"


def test_a_person_stays_in_the_same_section_as_their_heading():
    """The heading path is what retrieval and evidence extraction read as
    context. A CV row that carries the wrong section is a CV row attributed to
    the wrong subject."""
    # Two rows per table: the first is the header, so a one-row table produces
    # no TABLE_ROW block at all and the assertion below would pass vacuously.
    data = build(
        [
            ("Employee CV Pack", [["Field", "Value"], ["Name", "Wilma Vineetha Dmello"]]),
            (
                "Commercial and Financial Pack",
                [["Field", "Value"], ["Revenue", "AED 23,000,000 (illustrative)"]],
            ),
        ]
    )
    rows = [b for b in parse(data) if b.block_type is BlockType.TABLE_ROW]
    by_text = {b.text: b.heading_path for b in rows}
    assert by_text["Field: Name | Value: Wilma Vineetha Dmello"] == ["Employee CV Pack"]
    assert by_text["Field: Revenue | Value: AED 23,000,000 (illustrative)"] == [
        "Commercial and Financial Pack"
    ]


def test_blocks_come_out_in_document_order():
    """Ordinals feed chunking and character offsets. Reading every paragraph and
    then every table also put them in the wrong order, so a chunk could span two
    sections that were never adjacent."""
    data = build(
        [
            ("First", [["a"]]),
            ("Second", [["b"]]),
        ]
    )
    texts = [b.text for b in parse(data)]
    assert texts.index("First") < texts.index("a")
    assert texts.index("a") < texts.index("Second")
    assert texts.index("Second") < texts.index("b")


def test_a_table_before_any_heading_still_gets_a_name():
    """A pack that opens with a table has no preceding heading. It must not
    inherit a later one, and must not crash."""
    document = docx.Document()
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "orphan"
    document.add_heading("Later Section", level=1)
    buffer = io.BytesIO()
    document.save(buffer)

    blocks = [b for b in parse(buffer.getvalue()) if b.block_type is BlockType.TABLE]
    assert blocks[0].table_name == "Table 1"
    assert blocks[0].heading_path == []


def test_nested_headings_scope_the_table_correctly():
    """A level-2 heading under a level-1 keeps both in the path, so a table
    under it is attributed to the specific subsection rather than the pack."""
    document = docx.Document()
    document.add_heading("Employee CV Pack", level=1)
    document.add_heading("Wilma Vineetha Dmello", level=2)
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Role"
    table.cell(0, 1).text = "Senior Software Developer"
    buffer = io.BytesIO()
    document.save(buffer)

    tables = [b for b in parse(buffer.getvalue()) if b.block_type is BlockType.TABLE]
    assert tables[0].heading_path == ["Employee CV Pack", "Wilma Vineetha Dmello"]
    assert tables[0].table_name == "Wilma Vineetha Dmello"
