"""Archive intake, tested at the boundary where it is hostile.

A ZIP is the most attacker-controlled input this system accepts: structure as
well as bytes. Each test below corresponds to a defence that would otherwise be
easy to regress, because the happy path keeps working without any of them.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.core.errors import ValidationError
from app.ingestion.archives import (
    MAX_COMPRESSION_RATIO,
    MAX_ENTRIES,
    extract_members,
    looks_like_zip,
)
from app.ingestion.folder_types import document_type_for_folder
from app.models.enums import DocumentType


def build(files: dict[str, bytes | str], compress=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compress) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def test_a_normal_archive_yields_its_files_with_their_folders():
    data = build({"CVs/jane.txt": "Jane Smith", "Policies/infosec.txt": "Policy"})
    members = extract_members(data)
    assert {(m.folder, m.filename) for m in members} == {
        ("CVs", "jane.txt"),
        ("Policies", "infosec.txt"),
    }


def test_a_traversing_entry_is_refused_not_sanitised():
    """Rewriting the name into a valid one would hide the attempt."""
    with pytest.raises(ValidationError) as exc:
        extract_members(build({"../../etc/passwd": "x"}))
    assert "outside itself" in str(exc.value)


def test_an_absolute_path_is_refused():
    with pytest.raises(ValidationError) as exc:
        extract_members(build({"/etc/passwd": "x"}))
    assert "absolute path" in str(exc.value)


def test_a_high_ratio_entry_is_refused_before_it_is_kept():
    """The classic bomb: small archive, enormous expansion."""
    data = build({"bomb.txt": "A" * (MAX_COMPRESSION_RATIO * 200_000)})
    with pytest.raises(ValidationError) as exc:
        extract_members(data)
    assert "expands" in str(exc.value)


def test_a_nested_archive_is_refused_rather_than_unpacked():
    """Recursion is how a bomb gets past a per-archive cap."""
    inner = build({"a.txt": "a"})
    with pytest.raises(ValidationError) as exc:
        extract_members(build({"inner.zip": inner}))
    assert "nested archive" in str(exc.value)


def test_too_many_entries_is_refused_with_the_count():
    data = build({f"f{i}.txt": "x" for i in range(MAX_ENTRIES + 1)})
    with pytest.raises(ValidationError) as exc:
        extract_members(data)
    assert str(MAX_ENTRIES) in str(exc.value)


def test_platform_noise_is_dropped_silently():
    """Otherwise every macOS zip reports a screenful of skipped files."""
    data = build(
        {
            "CVs/jane.txt": "Jane",
            "__MACOSX/._jane.txt": "fork",
            "CVs/.DS_Store": "noise",
            "Thumbs.db": "noise",
        }
    )
    members = extract_members(data)
    assert [m.filename for m in members] == ["jane.txt"]


def test_an_empty_archive_says_so():
    with pytest.raises(ValidationError) as exc:
        extract_members(build({"__MACOSX/._x": "fork"}))
    assert "no files" in str(exc.value)


def test_a_non_zip_is_not_mistaken_for_one():
    assert not looks_like_zip(b"%PDF-1.4", "notes.zip")
    assert not looks_like_zip(build({"a.txt": "a"}), "corpus.pdf")
    assert looks_like_zip(build({"a.txt": "a"}), "corpus.zip")


# --- folder naming ----------------------------------------------------------


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("CVs", DocumentType.CV),
        ("4. CVs", DocumentType.CV),
        ("company_policies", DocumentType.POLICY),
        ("Legal & Registration/Trade Licence", DocumentType.TRADE_LICENCE),
        ("Support/SLA", DocumentType.SLA_DOC),
        ("Rate Cards", DocumentType.RATE_CARD),
        ("Audited Financial Statements", DocumentType.FINANCIAL_STATEMENT),
    ],
)
def test_folder_names_people_actually_use_are_understood(folder, expected):
    assert document_type_for_folder(folder) is expected


def test_the_deepest_folder_decides():
    """`Historical/Previous Tenders/2023` is a tender folder, not "historical"."""
    assert (
        document_type_for_folder("Historical/Previous Tenders/2023")
        is DocumentType.PREVIOUS_TENDER
    )


def test_what_was_asked_is_never_filed_as_what_was_offered():
    """The one mistake in this subsystem that is expensive in both directions."""
    assert document_type_for_folder("Previous Tenders") is DocumentType.PREVIOUS_TENDER
    assert (
        document_type_for_folder("Our Technical Proposals") is DocumentType.PREVIOUS_PROPOSAL
    )


def test_the_leading_concept_wins_over_a_later_one():
    """"Certifications & Accreditations" is a certificates shelf."""
    assert document_type_for_folder("3. Certifications & Accreditations") is (
        DocumentType.CERTIFICATE
    )
    assert document_type_for_folder("Accreditations") is DocumentType.ACCREDITATION


def test_a_specific_kind_is_not_shadowed_by_a_substring_of_itself():
    assert (
        document_type_for_folder("Staff Certificates") is DocumentType.PROFESSIONAL_CERTIFICATE
    )


def test_an_unrecognised_folder_yields_nothing_rather_than_a_guess():
    """Unclassified is correctable; confidently wrong is not."""
    assert document_type_for_folder("misc-stuff-2024") is None
    assert document_type_for_folder("") is None
