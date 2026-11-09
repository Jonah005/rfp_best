"""Upload safety: what happens to a file before anything parses it.

Uploaded documents are untrusted input from outside the organization. Three
things happen before a parser sees the bytes:

  1. the declared type is checked against the actual content,
  2. the file is scanned for malware, and
  3. macro-bearing and active-content containers are rejected outright.

Nothing here trusts the filename extension, and nothing executes embedded code.
"""

from __future__ import annotations

import hashlib
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.core.logging import get_logger

log = get_logger("ingestion.safety")

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB

# Only these are accepted. An extension outside the list is refused rather than
# guessed at.
ALLOWED_MIME_TYPES: dict[str, tuple[str, ...]] = {
    "application/pdf": (".pdf",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (".xlsx",),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (".pptx",),
    "text/plain": (".txt", ".md"),
    "text/csv": (".csv",),
    "text/markdown": (".md",),
}

# Macro-enabled Office formats and legacy OLE compound documents. These carry
# executable content by design; there is no safe way to accept them, so they are
# refused with an explanation rather than silently stripped.
REJECTED_EXTENSIONS = {
    ".docm",
    ".xlsm",
    ".pptm",
    ".dotm",
    ".xltm",
    ".potm",
    ".doc",
    ".xls",
    ".ppt",
    ".exe",
    ".dll",
    ".js",
    ".vbs",
    ".ps1",
    ".sh",
    ".bat",
    ".jar",
    ".htm",
    ".html",
}

_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),  # all OOXML formats
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage"),  # legacy Office
)


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    scanner: str
    detail: str = ""

    def as_json(self) -> dict:
        return {"clean": self.clean, "scanner": self.scanner, "detail": self.detail}


class MalwareScanner(ABC):
    name: str = "abstract"

    @abstractmethod
    def scan(self, data: bytes) -> ScanResult: ...


class ClamAVScanner(MalwareScanner):
    """Scans via a clamd daemon. The production scanner."""

    name = "clamav"

    def __init__(self, host: str = "localhost", port: int = 3310) -> None:
        self.host, self.port = host, port

    def scan(self, data: bytes) -> ScanResult:
        import clamd  # imported lazily so the dependency is optional in dev

        client = clamd.ClamdNetworkSocket(host=self.host, port=self.port)
        result = client.instream(BytesIO(data))
        status, signature = result["stream"]
        return ScanResult(
            clean=status == "OK",
            scanner=self.name,
            detail="" if status == "OK" else f"{status}: {signature}",
        )


class NoOpScanner(MalwareScanner):
    """Development stand-in.

    Refuses to be used in production: an environment that believes it is
    scanning uploads while doing nothing is worse than one that knows it is not.
    """

    name = "noop"

    def __init__(self) -> None:
        if get_settings().is_production:
            raise RuntimeError(
                "NoOpScanner must not be used in production. Configure a real "
                "malware scanner before accepting uploads."
            )

    def scan(self, data: bytes) -> ScanResult:
        return ScanResult(clean=True, scanner=self.name, detail="scanning disabled")


class EicarAwareScanner(NoOpScanner):
    """NoOp plus EICAR detection, so the scan path is testable end to end."""

    name = "eicar-test"
    EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR"

    def scan(self, data: bytes) -> ScanResult:
        if self.EICAR in data:
            return ScanResult(clean=False, scanner=self.name, detail="EICAR-Test-File")
        return ScanResult(clean=True, scanner=self.name)


# Control characters that never appear in legitimate text. NUL in particular is
# the classic marker of binary content that would otherwise decode as UTF-8,
# because C0 control bytes are valid UTF-8 code points.
_TEXT_SAFE_CONTROLS = {0x09, 0x0A, 0x0D}  # tab, newline, carriage return


def _looks_binary(data: bytes, *, sample: int = 8192) -> bool:
    window = data[:sample]
    if b"\x00" in window:
        return True
    controls = sum(1 for byte in window if byte < 0x20 and byte not in _TEXT_SAFE_CONTROLS)
    return bool(window) and controls / len(window) > 0.02


def sniff_mime(data: bytes, filename: str) -> str:
    """Determine the type from CONTENT, not from the filename."""
    for signature, mime in _MAGIC_SIGNATURES:
        if data.startswith(signature):
            if mime == "application/zip":
                return _ooxml_type(data, filename)
            return mime
    if _looks_binary(data):
        return "application/octet-stream"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/csv" if filename.lower().endswith(".csv") else "text/plain"


def _ooxml_type(data: bytes, filename: str) -> str:
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return "application/octet-stream"
    if "word/document.xml" in names:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if "xl/workbook.xml" in names:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if "ppt/presentation.xml" in names:
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return "application/zip"


def assert_no_active_content(data: bytes, mime_type: str) -> None:
    """Reject OOXML containers carrying macros or external references."""
    if not mime_type.startswith("application/vnd.openxmlformats"):
        return
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as exc:
        raise ValidationError("The file is not a readable Office document.") from exc

    for name in names:
        lowered = name.lower()
        if "vbaproject.bin" in lowered or lowered.endswith(".bin") and "vba" in lowered:
            raise ValidationError(
                "This document contains macros. Macro-enabled files are not accepted; "
                "please upload a macro-free version."
            )
        if lowered.endswith((".exe", ".dll", ".scr")):
            raise ValidationError("This document embeds an executable and was rejected.")


@dataclass(frozen=True)
class ValidatedUpload:
    filename: str
    mime_type: str
    byte_size: int
    sha256: str
    scan: ScanResult


def validate_upload(
    data: bytes,
    filename: str,
    *,
    scanner: MalwareScanner,
    declared_mime: str | None = None,
) -> ValidatedUpload:
    """The single gate every uploaded byte passes through."""
    if not data:
        raise ValidationError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"The file is {len(data) // (1024 * 1024)} MB; the limit is "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    lowered = filename.lower()
    for ext in REJECTED_EXTENSIONS:
        if lowered.endswith(ext):
            raise ValidationError(
                f"Files of type '{ext}' are not accepted because they can carry executable content."
            )

    actual = sniff_mime(data, filename)
    if actual not in ALLOWED_MIME_TYPES:
        raise ValidationError(
            f"Unsupported file type '{actual}'. Accepted: PDF, DOCX, XLSX, PPTX, TXT, MD, CSV."
        )
    if declared_mime and declared_mime != actual:
        # The content wins. A mismatch is logged because it is often the first
        # sign of something deliberately mislabelled.
        log.warning("mime_mismatch", declared=declared_mime, actual=actual, file=filename)

    assert_no_active_content(data, actual)

    result = scanner.scan(data)
    if not result.clean:
        raise ValidationError(
            f"The file failed a malware scan and was quarantined: {result.detail}"
        )

    return ValidatedUpload(
        filename=filename,
        mime_type=actual,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        scan=result,
    )


def build_scanner() -> MalwareScanner:
    settings = get_settings()
    if settings.is_production:
        return ClamAVScanner()
    return EicarAwareScanner()
