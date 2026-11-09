"""Read the pages that have no text layer, locally.

Tender packs are full of scans: a stamped trade licence, a signed declaration, a
certificate photographed and pasted into a PDF. `pdfplumber` returns an empty
string for those pages, and an empty page is indistinguishable downstream from a
page that genuinely said nothing — the requirement it contained is simply absent
from the corpus, and no amount of better matching recovers it.

Three rules shape this module.

**Local, and pip-only.** The point of OCR here is that confidential bid documents
are read inside the boundary, so an OCR service that posts page images to a
vendor would defeat the purpose. `rapidocr-onnxruntime` is the recommended
engine because it installs with pip on Windows and runs on CPU; Tesseract is
supported for deployments that already have the binary. Neither is a hard
dependency: with no engine installed the pipeline behaves exactly as it did
before.

**Only where there is nothing else.** A page with a text layer is never
rendered. OCR is one to two orders of magnitude slower than text extraction and
strictly worse at it — a page that extracts cleanly must not be replaced with a
guess about its pixels.

**Bounded.** A page cap applies per document. A 400-page scanned appendix is a
real thing to be handed, and it must degrade to "the first N pages were read"
rather than to an ingestion job that never finishes.
"""

from __future__ import annotations

import io
from abc import ABC, abstractmethod
from functools import lru_cache

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("ingestion.ocr")

# Below this many characters a page is treated as having no text layer. Not
# zero: a scanned page often extracts a stray ligature or a page number from an
# overlaid stamp, and calling that a text layer would skip the OCR that page
# actually needs.
MIN_PAGE_CHARS = 40
# Rendering resolution. 200 DPI is the usual floor for reliable OCR of body
# text; higher costs memory per page for little accuracy on documents this size.
RENDER_DPI = 200
# Per-document ceiling on pages rendered.
MAX_OCR_PAGES = 60


class OcrProvider(ABC):
    """Turns one rendered page into text. Never raises: OCR is best-effort."""

    name = "none"

    @abstractmethod
    def image_to_text(self, png: bytes) -> str: ...


class NullOcrProvider(OcrProvider):
    """No engine installed. Scanned pages stay empty, as they did before."""

    name = "none"

    def image_to_text(self, png: bytes) -> str:
        return ""


class RapidOcrProvider(OcrProvider):
    """ONNX runtime, CPU, pip-installable on Windows without a system binary."""

    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR()

    def image_to_text(self, png: bytes) -> str:
        import numpy as np
        from PIL import Image

        image = Image.open(io.BytesIO(png)).convert("RGB")
        result, _elapsed = self._engine(np.array(image))
        if not result:
            return ""
        # Each entry is (box, text, confidence). The engine returns lines in
        # reading order, so joining with newlines preserves the layout the block
        # segmenter downstream expects.
        return "\n".join(str(entry[1]) for entry in result if len(entry) > 1)


class TesseractProvider(OcrProvider):
    """For deployments that already have the Tesseract binary installed."""

    name = "tesseract"

    def __init__(self) -> None:
        import pytesseract
        from PIL import Image  # noqa: F401 - checked here so failure is at build time

        # Fails loudly now rather than once per page later.
        pytesseract.get_tesseract_version()
        self._pytesseract = pytesseract

    def image_to_text(self, png: bytes) -> str:
        from PIL import Image

        return self._pytesseract.image_to_string(Image.open(io.BytesIO(png)))


def _build(name: str) -> OcrProvider:
    if name == "rapidocr":
        return RapidOcrProvider()
    if name == "tesseract":
        return TesseractProvider()
    return NullOcrProvider()


@lru_cache(maxsize=1)
def get_ocr_provider() -> OcrProvider:
    """The configured engine, or the first one that imports under "auto".

    Cached: both engines load model files or probe a binary on construction, and
    doing that per page turns a slow path into an unusable one.
    """
    configured = get_settings().ocr_provider
    candidates = ["rapidocr", "tesseract"] if configured == "auto" else [configured]
    for name in candidates:
        if name == "none":
            break
        try:
            provider = _build(name)
        except Exception as exc:  # noqa: BLE001 - a missing engine is not an error
            log.info("ocr_engine_unavailable", engine=name, reason=type(exc).__name__)
            continue
        log.info("ocr_engine_ready", engine=name)
        return provider
    if configured not in ("auto", "none"):
        log.warning("ocr_engine_missing", requested=configured)
    return NullOcrProvider()


def ocr_available() -> bool:
    return get_ocr_provider().name != "none"


def render_page_png(pdf_bytes: bytes, page_index: int, *, dpi: int = RENDER_DPI) -> bytes | None:
    """Render one page to PNG, or None if the PDF cannot be rendered.

    `pypdfium2` arrives with pdfplumber, so this adds no dependency of its own.
    """
    try:
        import pypdfium2

        document = pypdfium2.PdfDocument(pdf_bytes)
        try:
            page = document[page_index]
            image = page.render(scale=dpi / 72).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        finally:
            document.close()
    except Exception as exc:  # noqa: BLE001 - a page that will not render is skipped
        log.warning("page_render_failed", page=page_index + 1, reason=type(exc).__name__)
        return None


def ocr_pages(pdf_bytes: bytes, page_texts: list[str]) -> dict[int, str]:
    """Fill in the pages that have no text layer.

    Returns {page_index: recovered_text} for pages that were read, so the caller
    can keep everything else exactly as extraction produced it. Pages that
    render but produce nothing are omitted rather than recorded as empty — an
    unreadable scan and a blank page are different facts, and only the first is
    worth telling somebody about.
    """
    provider = get_ocr_provider()
    if provider.name == "none":
        return {}

    needing = [i for i, text in enumerate(page_texts) if len((text or "").strip()) < MIN_PAGE_CHARS]
    if not needing:
        return {}

    capped = needing[:MAX_OCR_PAGES]
    recovered: dict[int, str] = {}
    for index in capped:
        png = render_page_png(pdf_bytes, index)
        if png is None:
            continue
        try:
            text = provider.image_to_text(png).strip()
        except Exception as exc:  # noqa: BLE001 - one bad page must not fail the document
            log.warning("ocr_page_failed", page=index + 1, reason=type(exc).__name__)
            continue
        if text:
            recovered[index] = text

    log.info(
        "ocr_completed",
        engine=provider.name,
        pages_without_text=len(needing),
        pages_attempted=len(capped),
        pages_recovered=len(recovered),
        capped=len(needing) > len(capped),
    )
    return recovered
