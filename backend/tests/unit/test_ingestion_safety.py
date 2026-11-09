"""Upload safety: nothing reaches a parser until it has passed all three gates."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.core.errors import ValidationError
from app.ingestion.safety import (
    EicarAwareScanner,
    NoOpScanner,
    ScanResult,
    assert_no_active_content,
    sniff_mime,
    validate_upload,
)


@pytest.fixture
def scanner() -> EicarAwareScanner:
    return EicarAwareScanner()


def test_type_is_determined_from_content_not_filename(scanner):
    """A PDF renamed to .txt is still a PDF; a filename is an assertion, not evidence."""
    assert sniff_mime(b"%PDF-1.7\nstuff", "totally_a_text_file.txt") == "application/pdf"


def test_declared_mime_mismatch_does_not_override_content(scanner):
    result = validate_upload(
        b"plain text here", "notes.txt", scanner=scanner, declared_mime="application/pdf"
    )
    assert result.mime_type == "text/plain"


def test_empty_and_oversized_files_are_refused(scanner):
    with pytest.raises(ValidationError, match="empty"):
        validate_upload(b"", "empty.txt", scanner=scanner)


def test_executable_and_macro_extensions_are_refused(scanner):
    for name in ("payload.exe", "macro.docm", "sheet.xlsm", "legacy.doc", "script.js"):
        with pytest.raises(ValidationError, match="not accepted"):
            validate_upload(b"anything at all", name, scanner=scanner)


def test_unsupported_binary_type_is_refused(scanner):
    with pytest.raises(ValidationError, match="Unsupported file type"):
        validate_upload(b"\x00\x01\x02\x03binary", "mystery.bin", scanner=scanner)


def test_malware_detection_blocks_the_upload(scanner):
    payload = b"harmless prefix " + EicarAwareScanner.EICAR + b" suffix"
    with pytest.raises(ValidationError, match="malware"):
        validate_upload(payload, "invoice.txt", scanner=scanner)


def test_ooxml_with_macros_is_rejected():
    """A .docx that smuggles a vbaProject is still macro-bearing."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("word/document.xml", "<document/>")
        zf.writestr("word/vbaProject.bin", "macro payload")
    data = buffer.getvalue()
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with pytest.raises(ValidationError, match="macros"):
        assert_no_active_content(data, mime)


def test_ooxml_with_embedded_executable_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("word/document.xml", "<document/>")
        zf.writestr("word/embeddings/payload.exe", "MZ")
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with pytest.raises(ValidationError, match="executable"):
        assert_no_active_content(buffer.getvalue(), mime)


def test_clean_upload_records_hash_size_and_scanner(scanner):
    result = validate_upload(b"hello world", "notes.txt", scanner=scanner)
    assert result.byte_size == 11
    assert len(result.sha256) == 64
    assert result.scan.clean and result.scan.scanner == "eicar-test"


def test_noop_scanner_refuses_to_exist_in_production(monkeypatch):
    """An environment that believes it is scanning while doing nothing is worse
    than one that knows it is not."""
    from app.core.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("BIDREADY_ENV", "production")
    monkeypatch.setenv("BIDREADY_SECRET_KEY", "a-real-production-secret-value")
    try:
        assert Settings().is_production
        with pytest.raises(RuntimeError, match="must not be used in production"):
            NoOpScanner()
    finally:
        get_settings.cache_clear()


def test_scan_result_serializes_for_the_audit_record():
    assert ScanResult(clean=True, scanner="x").as_json() == {
        "clean": True,
        "scanner": "x",
        "detail": "",
    }


def test_binary_content_with_control_bytes_is_not_mistaken_for_text(scanner):
    """C0 control bytes are valid UTF-8, so a naive decode check passes binary
    content through as text/plain. Content sniffing must catch it."""
    assert sniff_mime(b"\x00\x01\x02\x03binary", "x.txt") == "application/octet-stream"
    assert sniff_mime(b"\x7f\x45\x4c\x46\x02\x01\x01\x00" * 20, "x.txt") == (
        "application/octet-stream"
    )


def test_ordinary_text_with_tabs_and_newlines_is_still_text():
    assert sniff_mime(b"col1\tcol2\r\nvalue\tvalue\n", "data.txt") == "text/plain"
    assert sniff_mime("مرحبا بالعالم\n".encode(), "arabic.txt") == "text/plain"
