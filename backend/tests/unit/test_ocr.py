"""Scanned pages must not silently become empty pages.

A tender pack arrives with a stamped trade licence, a signed declaration, a
certificate photographed into a PDF. Those pages have no text layer, and
`pdfplumber` returns "" for them. Downstream, an empty page is indistinguishable
from a page that genuinely said nothing: the requirement or the certificate it
held is simply absent from the corpus, and no amount of better matching recovers
it. That is the failure these cases pin down.

The engine is optional by design — no system binary is required to run BidReady
— so the cases split in two: what must be true with no engine installed (the old
behaviour, unchanged and honest about it) and what must be true with one.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.core.config import get_settings
from app.ingestion import ocr
from app.ingestion.parsers import parse_document

SCAN_LINES = [
    "CERTIFICATE OF ACCREDITATION",
    "Acme Cyber LLC is accredited to CREST",
    "for penetration testing services.",
    "Valid until 31 December 2027",
]


def _scanned_pdf() -> bytes:
    """A PDF that is a picture of text — exactly what a scanner produces."""
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=40)
    except TypeError:  # older Pillow
        font = ImageFont.load_default()
    for index, line in enumerate(SCAN_LINES):
        draw.text((90, 140 + index * 90), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, "PDF", resolution=150)
    return buffer.getvalue()


@pytest.fixture
def no_engine(monkeypatch):
    monkeypatch.setattr(ocr, "get_ocr_provider", lambda: ocr.NullOcrProvider())
    return None


# --- with no engine installed -----------------------------------------------


def test_a_scanned_pdf_yields_nothing_without_an_engine(no_engine):
    """The behaviour this module exists to change, asserted so it stays visible.

    This is not a bug being tolerated: with no OCR engine there is genuinely
    nothing to read. What matters is that it is recorded as such rather than
    presented as a document with no requirements in it.
    """
    result = parse_document(_scanned_pdf(), "scan.pdf", "application/pdf")
    assert result.blocks == []
    assert result.ocr_pages == []


def test_a_text_pdf_is_unaffected_by_the_ocr_path(no_engine):
    """Every other document must parse exactly as it did before."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "The bidder shall hold ISO 27001 certification.")
    pdf.save()
    result = parse_document(buffer.getvalue(), "text.pdf", "application/pdf")
    assert any("ISO 27001" in block.text for block in result.blocks)
    assert result.ocr_pages == []


# --- with an engine installed -----------------------------------------------

engine_required = pytest.mark.skipif(
    not ocr.ocr_available(), reason="no local OCR engine installed in this environment"
)


@engine_required
def test_a_scanned_page_is_read_and_marked_as_ocr():
    result = parse_document(_scanned_pdf(), "scan.pdf", "application/pdf")
    text = " ".join(block.text for block in result.blocks)

    assert "CREST" in text, "the accreditation the page exists to prove"
    # The provenance matters as much as the text: a reviewer judging a
    # surprising excerpt needs to know it came from a machine reading pixels.
    assert result.ocr_pages == [1]


@engine_required
def test_a_page_with_a_text_layer_is_never_rendered(monkeypatch):
    """OCR is slower and strictly worse at pages that extract cleanly."""
    calls = []
    monkeypatch.setattr(ocr, "render_page_png", lambda *a, **k: calls.append(a) or None)
    ocr.ocr_pages(b"", ["a page with plenty of extracted text " * 5])
    assert calls == []


# --- the rules that keep it bounded -----------------------------------------


def test_a_nearly_empty_page_still_counts_as_needing_ocr():
    """A scan often extracts a stray page number from an overlaid stamp.

    Treating that as a text layer would skip the OCR the page actually needs,
    which is why the threshold is a character count and not "is it empty".
    """
    assert len("12") < ocr.MIN_PAGE_CHARS


def test_the_page_cap_is_a_real_bound():
    """A 400-page scanned appendix must degrade, not run forever."""
    assert 0 < ocr.MAX_OCR_PAGES <= 200


def test_the_configured_engine_is_honoured(monkeypatch):
    monkeypatch.setattr(ocr, "get_settings", lambda: type("S", (), {"ocr_provider": "none"})())
    ocr.get_ocr_provider.cache_clear()
    try:
        assert ocr.get_ocr_provider().name == "none"
    finally:
        ocr.get_ocr_provider.cache_clear()


def test_ocr_settings_default_to_auto():
    assert get_settings().ocr_provider == "auto"
