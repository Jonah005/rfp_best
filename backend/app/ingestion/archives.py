"""Unpack a ZIP of company documents, defensively.

A bid team's company material lives in folders, not in one file at a time, so
bulk intake is the difference between this subsystem being usable and being a
chore. But an archive is the most hostile input this system accepts: it is
attacker-controlled structure as well as attacker-controlled bytes, and the
classic failures are all cheap to trigger and expensive to suffer.

What is defended against, and why each limit exists:

* **Zip slip.** An entry named ``../../etc/passwd`` writes outside the intended
  directory. Nothing here writes to disk, but a traversing name would still
  become a document *title*, so names are normalised and anything that escapes
  is refused rather than sanitised into something plausible.
* **Zip bombs.** A few kilobytes can expand to gigabytes. Three independent
  caps apply — per-entry size, total uncompressed size, and compression ratio —
  because each catches a bomb the others miss: one enormous entry, many modest
  entries, or a pathological ratio.
* **Decompression before inspection.** The declared size in the central
  directory is a claim, not a fact, so every entry is read through a bounded
  reader that stops at the cap instead of trusting ``file_size``.
* **Nested archives.** A ZIP inside a ZIP is not unpacked. Recursive unpacking
  is how a bomb defeats a per-archive cap, and a legitimate company document is
  never a nested archive.

Every extracted file still goes through the same ``validate_upload`` as a
single upload: the archive is a delivery mechanism, never a way past scanning.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass

from app.core.errors import ValidationError
from app.core.logging import get_logger

log = get_logger("ingestion.archives")

MAX_ENTRIES = 500
MAX_ENTRY_BYTES = 200 * 1024 * 1024  # matches the single-upload cap
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GB unpacked across the whole archive
MAX_COMPRESSION_RATIO = 200  # a real document corpus is nowhere near this

# Directory noise that carries no company document and would otherwise be
# reported to the user as a skipped file for every entry.
IGNORED_PREFIXES = ("__MACOSX/", ".git/")
IGNORED_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")

ARCHIVE_MIME_TYPES = frozenset(
    {"application/zip", "application/x-zip-compressed", "application/octet-stream"}
)


@dataclass(frozen=True)
class ArchiveMember:
    """One file lifted out of an archive, with the folder it came from."""

    filename: str
    folder: str
    data: bytes


def looks_like_zip(data: bytes, filename: str) -> bool:
    """Content first, extension second — the same rule as every other check."""
    return data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08") and filename.lower().endswith(
        ".zip"
    )


def _is_ignorable(name: str) -> bool:
    if name.endswith("/"):
        return True
    if any(name.startswith(prefix) for prefix in IGNORED_PREFIXES):
        return True
    base = posixpath.basename(name)
    return base in IGNORED_NAMES or base.startswith(".")


def _safe_relative_path(name: str) -> str:
    """Reject anything that escapes the archive root.

    Refused rather than sanitised: a name that tries to traverse is not a
    filename with a typo, and quietly rewriting it into a valid one hides the
    attempt from whoever reviews the upload.
    """
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or ":" in normalised.split("/")[0]:
        raise ValidationError(f"The archive contains an absolute path: '{name}'.")
    resolved = posixpath.normpath(normalised)
    if resolved.startswith("../") or resolved == ".." or resolved.startswith("/"):
        raise ValidationError(f"The archive contains a path outside itself: '{name}'.")
    return resolved


def _read_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Read one entry, stopping at the cap rather than trusting its header."""
    with archive.open(info, "r") as handle:
        data = handle.read(MAX_ENTRY_BYTES + 1)
    if len(data) > MAX_ENTRY_BYTES:
        raise ValidationError(
            f"'{info.filename}' expands beyond the "
            f"{MAX_ENTRY_BYTES // (1024 * 1024)} MB per-file limit."
        )
    if info.compress_size > 0:
        ratio = len(data) / info.compress_size
        if ratio > MAX_COMPRESSION_RATIO:
            raise ValidationError(
                f"'{info.filename}' expands {ratio:.0f}x, past the "
                f"{MAX_COMPRESSION_RATIO}x limit. The archive was not unpacked."
            )
    return data


def extract_members(data: bytes) -> list[ArchiveMember]:
    """Return every usable file in the archive, in archive order.

    Raises rather than partially unpacking: a caller that received half an
    archive plus an error cannot tell which half, and the safe reading of a
    refused archive is that nothing from it was accepted.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValidationError("The archive could not be read as a ZIP file.") from exc

    with archive:
        infos = [info for info in archive.infolist() if not _is_ignorable(info.filename)]
        if len(infos) > MAX_ENTRIES:
            raise ValidationError(
                f"The archive holds {len(infos)} files; the limit is {MAX_ENTRIES}. "
                "Split it into smaller archives."
            )
        if not infos:
            raise ValidationError("The archive contains no files.")

        members: list[ArchiveMember] = []
        total = 0
        for info in infos:
            relative = _safe_relative_path(info.filename)
            base = posixpath.basename(relative)
            if base.lower().endswith(".zip"):
                # Not unpacked on purpose: recursion is how a bomb gets past a
                # per-archive cap, and a company document is never an archive.
                raise ValidationError(
                    f"'{relative}' is a nested archive. Unpack it and upload its "
                    "contents instead."
                )
            payload = _read_bounded(archive, info)
            total += len(payload)
            if total > MAX_TOTAL_BYTES:
                raise ValidationError(
                    f"The archive expands past the "
                    f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB total limit."
                )
            members.append(
                ArchiveMember(
                    filename=base,
                    folder=posixpath.dirname(relative),
                    data=payload,
                )
            )

    log.info("archive_unpacked", entries=len(members), bytes=total)
    return members
