"""Structure-preserving document parsers.

The point of this module is what it does NOT throw away. A parser that returns
a flat string makes retrieval guess; one that keeps headings, tables, rows and
page numbers lets retrieval rank by them and lets every claim cite an exact
location. Losing that at parse time is unrecoverable downstream.

Parsers never execute embedded content. They read structure and text only.
"""

from __future__ import annotations

import csv
import io
import re
from abc import ABC, abstractmethod
from collections import Counter

from pydantic import BaseModel, Field

from app.core.errors import ValidationError
from app.ingestion import ocr
from app.models.enums import BlockType

# Matches leading clause identifiers: "6", "6.1", "6.1.2", "A.3", optionally
# preceded by a label. Deliberately conservative -- a false clause_ref is worse
# than a missing one, because precedence resolution keys on it.
_CLAUSE_RE = re.compile(
    r"^\s*(?:(?:section|clause|article|item|appendix|annex)\s+)?"
    r"([A-Z]?\d+(?:\.\d+){0,4}|[A-Z]\.\d+(?:\.\d+){0,3})"
    r"[\.\)]?\s+(?=\S)",
    re.IGNORECASE,
)
_HEADING_HINT = re.compile(r"^\s*[A-Z0-9]")


class ParsedBlock(BaseModel):
    """A parser's output unit. Becomes a DocumentBlock row unchanged."""

    block_type: BlockType
    ordinal: int = 0
    text: str
    heading_path: list[str] = Field(default_factory=list)
    clause_ref: str | None = None
    table_name: str | None = None
    row_index: int | None = None
    col_index: int | None = None
    page_from: int | None = None
    page_to: int | None = None
    char_start: int = 0
    char_end: int = 0
    language: str = "en"


class ParseResult(BaseModel):
    blocks: list[ParsedBlock]
    page_count: int | None = None
    #: 1-based pages whose text came from OCR rather than from a text layer.
    #: Empty for every format but PDF, and for PDFs that extracted normally.
    ocr_pages: list[int] = Field(default_factory=list)


# A run of dot leaders is a table-of-contents entry, not a clause. Treating
# "5.1 Notice to Bid ......... 11" as clause 5.1 creates a second, competing
# block for a clause that already exists in the body, and precedence then has
# to choose between the real text and a line from the contents page.
_TOC_LEADER_RE = re.compile(r"\.{4,}")


def detect_clause_ref(text: str) -> str | None:
    if _TOC_LEADER_RE.search(text):
        return None
    match = _CLAUSE_RE.match(text)
    if not match:
        return None
    ref = match.group(1)
    # A bare year or a large integer is not a clause reference.
    if "." not in ref and ref.isdigit() and (len(ref) > 2 or int(ref) > 99):
        return None
    return ref


def looks_like_heading(text: str, *, max_words: int = 14) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped.split()) > max_words:
        return False
    if stripped.endswith((".", ";", ",")):
        return False
    # A heading is not a sentence. Without this, a segment that glued a clause
    # and the next section's title together -- "2. Menu Website should adopt a
    # drop down based menu. Integration Requirements" -- was filed as a heading,
    # and every clause after it inherited that whole blob as its section.
    if ". " in stripped:
        return False
    return bool(_HEADING_HINT.match(stripped))


def looks_like_section_title(line: str) -> bool:
    """A bare section title: "Integration Requirements", "Technical Requirements".

    Numbered clauses already start a new segment. An unnumbered section title
    did not, so when a PDF extracted with no blank line before it, the title was
    swallowed by the previous clause's segment and never became a heading. Every
    clause under it then kept the PREVIOUS section's name, which is how
    Integration 4 (SMS) and Business 4 (Menu) both came to be filed as
    "Business Requirements / 4" -- one reference for two unrelated obligations.
    """
    stripped = line.strip()
    words = stripped.split()
    if not (1 <= len(words) <= 6):
        return False
    if stripped.endswith((".", ";", ",", ":")):
        return False
    if detect_clause_ref(stripped):
        return False
    if not stripped[:1].isupper():
        return False
    # Title Case or ALL CAPS, the two ways a section title is written. A
    # sentence fragment that happens to be short is not one.
    significant = [w for w in words if len(w) > 3]
    return bool(significant) and all(w[:1].isupper() or w.isupper() for w in significant)


class DocumentParser(ABC):
    mime_types: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, data: bytes, filename: str) -> ParseResult: ...

    @staticmethod
    def _finalize(blocks: list[ParsedBlock]) -> list[ParsedBlock]:
        """Assign ordinals and running character offsets."""
        cursor = 0
        for index, block in enumerate(blocks):
            block.ordinal = index
            block.char_start = cursor
            block.char_end = cursor + len(block.text)
            cursor = block.char_end + 1
        return blocks


class PlainTextParser(DocumentParser):
    mime_types = ("text/plain", "text/markdown")

    def parse(self, data: bytes, filename: str) -> ParseResult:
        text = data.decode("utf-8", errors="replace")
        blocks: list[ParsedBlock] = []
        heading_path: list[str] = []

        for raw in re.split(r"\n\s*\n", text):
            chunk = raw.strip()
            if not chunk:
                continue
            md_heading = re.match(r"^(#{1,6})\s+(.*)$", chunk)
            if md_heading:
                level, title = len(md_heading.group(1)), md_heading.group(2).strip()
                heading_path = heading_path[: level - 1] + [title]
                blocks.append(
                    ParsedBlock(
                        block_type=BlockType.HEADING,
                        text=title,
                        heading_path=list(heading_path),
                        clause_ref=detect_clause_ref(title),
                    )
                )
                continue
            if looks_like_heading(chunk) and len(chunk) < 120:
                heading_path = heading_path[:-1] + [chunk] if heading_path else [chunk]
                blocks.append(
                    ParsedBlock(
                        block_type=BlockType.HEADING,
                        text=chunk,
                        heading_path=list(heading_path),
                        clause_ref=detect_clause_ref(chunk),
                    )
                )
                continue
            blocks.append(
                ParsedBlock(
                    block_type=BlockType.PARAGRAPH,
                    text=chunk,
                    heading_path=list(heading_path),
                    clause_ref=detect_clause_ref(chunk),
                )
            )
        return ParseResult(blocks=self._finalize(blocks))


class CsvParser(DocumentParser):
    mime_types = ("text/csv",)

    def parse(self, data: bytes, filename: str) -> ParseResult:
        text = data.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return ParseResult(blocks=[])

        table_name = filename.rsplit(".", 1)[0]
        header = rows[0]
        blocks = [
            ParsedBlock(
                block_type=BlockType.TABLE,
                text=" | ".join(header),
                table_name=table_name,
                heading_path=[table_name],
            )
        ]
        for row_index, row in enumerate(rows[1:], start=1):
            # Pair each cell with its column name so a retrieved row is
            # self-describing rather than a bare list of values.
            labelled = " | ".join(
                f"{header[i]}: {value}" if i < len(header) else value
                for i, value in enumerate(row)
                if value.strip()
            )
            if not labelled:
                continue
            blocks.append(
                ParsedBlock(
                    block_type=BlockType.TABLE_ROW,
                    text=labelled,
                    table_name=table_name,
                    row_index=row_index,
                    heading_path=[table_name],
                )
            )
        return ParseResult(blocks=self._finalize(blocks))


class DocxParser(DocumentParser):
    mime_types = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)

    def parse(self, data: bytes, filename: str) -> ParseResult:
        import docx

        try:
            document = docx.Document(io.BytesIO(data))
        except Exception as exc:
            raise ValidationError("The Word document could not be read.") from exc

        blocks: list[ParsedBlock] = []
        heading_path: list[str] = []

        def emit_paragraph(para) -> None:
            nonlocal heading_path
            text = para.text.strip()
            if not text:
                return
            style = (para.style.name or "").lower() if para.style else ""
            if style.startswith("heading"):
                try:
                    level = int(style.replace("heading", "").strip() or 1)
                except ValueError:
                    level = 1
                heading_path = heading_path[: level - 1] + [text]
                block_type = BlockType.HEADING
            elif style.startswith("list"):
                block_type = BlockType.LIST_ITEM
            else:
                block_type = BlockType.PARAGRAPH
            blocks.append(
                ParsedBlock(
                    block_type=block_type,
                    text=text,
                    heading_path=list(heading_path),
                    clause_ref=detect_clause_ref(text),
                )
            )

        def emit_table(table, table_index: int) -> None:
            # Now genuinely the nearest preceding heading, because this runs at
            # the table's real position in the document.
            table_name = heading_path[-1] if heading_path else f"Table {table_index + 1}"
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            if not rows:
                return
            header = rows[0]
            blocks.append(
                ParsedBlock(
                    block_type=BlockType.TABLE,
                    text=" | ".join(header),
                    table_name=table_name,
                    heading_path=list(heading_path),
                )
            )
            for row_index, row in enumerate(rows[1:], start=1):
                labelled = " | ".join(
                    f"{header[i]}: {value}" if i < len(header) and header[i] else value
                    for i, value in enumerate(row)
                    if value
                )
                if not labelled:
                    continue
                blocks.append(
                    ParsedBlock(
                        block_type=BlockType.TABLE_ROW,
                        text=labelled,
                        table_name=table_name,
                        row_index=row_index,
                        heading_path=list(heading_path),
                    )
                )

        # Walk the body in document order.
        #
        # python-docx exposes `document.paragraphs` and `document.tables` as two
        # separate collections, and this parser used to drain the first and then
        # the second. `heading_path` was therefore fully wound forward before a
        # single table was read, so EVERY table in the document was filed under
        # whichever heading happened to come last -- while a comment two lines
        # above claimed it was the nearest preceding one.
        #
        # In a CV pack that put every person's table under the final section
        # heading, which is how historical CVs came to be described as blank
        # update forms and invented figures as a bank-letter template. Nothing
        # downstream could recover the association: by the time evidence was
        # extracted, the person and their table were in different sections.
        #
        # The body's own child order is the only source of truth for what
        # preceded what, so it is what gets walked.
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        table_index = 0
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                emit_paragraph(Paragraph(child, document))
            elif tag == "tbl":
                emit_table(Table(child, document), table_index)
                table_index += 1

        return ParseResult(blocks=self._finalize(blocks))


class XlsxParser(DocumentParser):
    mime_types = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",)

    def parse(self, data: bytes, filename: str) -> ParseResult:
        import openpyxl

        try:
            # data_only: read computed values, never formulas. Formulas are code.
            workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        except Exception as exc:
            raise ValidationError("The spreadsheet could not be read.") from exc

        blocks: list[ParsedBlock] = []
        for sheet in workbook.worksheets:
            rows = [
                [("" if c is None else str(c).strip()) for c in row]
                for row in sheet.iter_rows(values_only=True)
            ]
            rows = [r for r in rows if any(v for v in r)]
            if not rows:
                continue
            header = rows[0]
            blocks.append(
                ParsedBlock(
                    block_type=BlockType.TABLE,
                    text=" | ".join(v for v in header if v),
                    table_name=sheet.title,
                    heading_path=[sheet.title],
                )
            )
            for row_index, row in enumerate(rows[1:], start=1):
                labelled = " | ".join(
                    f"{header[i]}: {value}" if i < len(header) and header[i] else value
                    for i, value in enumerate(row)
                    if value
                )
                if not labelled:
                    continue
                blocks.append(
                    ParsedBlock(
                        block_type=BlockType.TABLE_ROW,
                        text=labelled,
                        table_name=sheet.title,
                        row_index=row_index,
                        heading_path=[sheet.title],
                    )
                )
        workbook.close()
        return ParseResult(blocks=self._finalize(blocks))


def _recurring_lines(pages: list[str], *, page_count: int) -> set[str]:
    """Lines that repeat across most pages: running headers and footers.

    A header printed on every page is not content, and leaving it in front of
    each block is not merely noise — clause detection anchors at the start of a
    block, so "ADX WAF RFP 2015" sitting ahead of "5.1 Notice to Bid" hides the
    clause number from the regex and the whole document parses as unnumbered.
    """
    if page_count < 3:
        return set()
    seen: Counter[str] = Counter()
    for text in pages:
        for line in {ln.strip() for ln in text.splitlines() if ln.strip()}:
            # Long lines are prose that happens to repeat, not furniture.
            if len(line) <= 120:
                seen[line] += 1
    threshold = max(3, int(page_count * 0.5))
    return {line for line, count in seen.items() if count >= threshold}


def _page_segments(text: str, drop: set[str]) -> list[str]:
    """Split one page's text into clause-sized segments.

    Splitting on blank lines alone is not enough. Plenty of real PDFs extract
    with no blank lines at all, and the entire page then arrives as a single
    block — too coarse for clause-keyed precedence, and impossible to cite. So
    a numbered line also starts a new segment, which is what a clause boundary
    looks like in every tender document this handles.
    """
    segments: list[str] = []
    current: list[str] = []

    def flush() -> None:
        joined = " ".join(" ".join(current).split())
        if joined:
            segments.append(joined)
        current.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if line in drop:
            continue
        if current and (detect_clause_ref(line) or looks_like_section_title(line)):
            flush()
        current.append(line)
    flush()
    return segments


class PdfParser(DocumentParser):
    mime_types = ("application/pdf",)

    def parse(self, data: bytes, filename: str) -> ParseResult:
        import pdfplumber

        blocks: list[ParsedBlock] = []
        heading_path: list[str] = []
        # Which pages were read by OCR rather than extracted. Carried out on the
        # result and into the ingestion audit record, so a reviewer looking at a
        # suspicious excerpt can tell whether they are reading the document or a
        # machine's reading of a photograph of it.
        ocr_pages_used: set[int] = set()
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                page_count = len(pdf.pages)
                # One pass for text first: running headers can only be
                # identified by comparing pages against each other.
                page_texts = [page.extract_text() or "" for page in pdf.pages]
                # Scanned pages have no text layer, and an empty page is
                # indistinguishable downstream from a page that said nothing —
                # the requirement it held is simply absent from the corpus. OCR
                # fills in only those pages, locally, and only if an engine is
                # installed; pages that extracted normally are never touched.
                for index, text in ocr.ocr_pages(data, page_texts).items():
                    page_texts[index] = text
                    ocr_pages_used.add(index + 1)
                running = _recurring_lines(page_texts, page_count=page_count)
                for page_no, page in enumerate(pdf.pages, start=1):
                    for table in page.extract_tables() or []:
                        if not table:
                            continue
                        header = [(c or "").strip() for c in table[0]]
                        table_name = heading_path[-1] if heading_path else f"Table p{page_no}"
                        blocks.append(
                            ParsedBlock(
                                block_type=BlockType.TABLE,
                                text=" | ".join(v for v in header if v),
                                table_name=table_name,
                                page_from=page_no,
                                page_to=page_no,
                                heading_path=list(heading_path),
                            )
                        )
                        for row_index, row in enumerate(table[1:], start=1):
                            cells = [(c or "").strip() for c in row]
                            labelled = " | ".join(
                                f"{header[i]}: {v}" if i < len(header) and header[i] else v
                                for i, v in enumerate(cells)
                                if v
                            )
                            if not labelled:
                                continue
                            blocks.append(
                                ParsedBlock(
                                    block_type=BlockType.TABLE_ROW,
                                    text=labelled,
                                    table_name=table_name,
                                    row_index=row_index,
                                    page_from=page_no,
                                    page_to=page_no,
                                    heading_path=list(heading_path),
                                )
                            )

                    for chunk in _page_segments(page_texts[page_no - 1], running):
                        if not chunk:
                            continue
                        if looks_like_heading(chunk) and len(chunk) < 120:
                            heading_path = heading_path[:-1] + [chunk] if heading_path else [chunk]
                            blocks.append(
                                ParsedBlock(
                                    block_type=BlockType.HEADING,
                                    text=chunk,
                                    page_from=page_no,
                                    page_to=page_no,
                                    heading_path=list(heading_path),
                                    clause_ref=detect_clause_ref(chunk),
                                )
                            )
                            continue
                        blocks.append(
                            ParsedBlock(
                                block_type=BlockType.PARAGRAPH,
                                text=chunk,
                                page_from=page_no,
                                page_to=page_no,
                                heading_path=list(heading_path),
                                clause_ref=detect_clause_ref(chunk),
                            )
                        )
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError("The PDF could not be read.") from exc

        return ParseResult(
            blocks=self._finalize(blocks),
            page_count=page_count,
            ocr_pages=sorted(ocr_pages_used),
        )


_PARSERS: tuple[DocumentParser, ...] = (
    PlainTextParser(),
    CsvParser(),
    DocxParser(),
    XlsxParser(),
    PdfParser(),
)


def get_parser(mime_type: str) -> DocumentParser:
    for parser in _PARSERS:
        if mime_type in parser.mime_types:
            return parser
    raise ValidationError(f"No parser is available for '{mime_type}'.")


def parse_document(data: bytes, filename: str, mime_type: str) -> ParseResult:
    return get_parser(mime_type).parse(data, filename)
