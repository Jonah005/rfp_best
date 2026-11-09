"""Assemble parsed blocks into retrieval chunks.

Two rules drive the design:

  1. **Provenance survives.** Every chunk records which blocks it came from and
     the heading path, clause reference, table name and page range they carried.
     A chunk that cannot cite its source is useless as evidence.
  2. **Table rows stay whole and stay labelled.** A retrieved row must be
     self-describing, so each row chunk carries its table's header. This is what
     makes "which government VAPT references do we have?" return the reference
     table rather than a fragment of a CV.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.ingestion.parsers import ParsedBlock
from app.models.enums import BlockType

DEFAULT_MAX_TOKENS = 380
DEFAULT_OVERLAP_TOKENS = 40


def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic estimate.

    Deliberately not tiktoken: the exact count only needs to be close enough to
    keep chunks inside a context budget, and a hard dependency on one vendor's
    tokenizer would contradict the provider abstraction.
    """
    return max(1, (len(text) + 3) // 4)


@dataclass
class AssembledChunk:
    text: str
    token_count: int
    block_ids: list[uuid.UUID] = field(default_factory=list)
    block_ordinals: list[int] = field(default_factory=list)
    heading_path: list[str] = field(default_factory=list)
    clause_ref: str | None = None
    table_name: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    char_start: int = 0
    char_end: int = 0
    language: str = "en"


def _new_chunk(block: ParsedBlock) -> AssembledChunk:
    return AssembledChunk(
        text="",
        token_count=0,
        heading_path=list(block.heading_path),
        clause_ref=block.clause_ref,
        table_name=block.table_name,
        page_from=block.page_from,
        page_to=block.page_to,
        char_start=block.char_start,
        char_end=block.char_end,
        language=block.language,
    )


def _absorb(chunk: AssembledChunk, block: ParsedBlock, text: str) -> None:
    chunk.text = f"{chunk.text}\n{text}".strip() if chunk.text else text
    chunk.token_count = estimate_tokens(chunk.text)
    chunk.block_ordinals.append(block.ordinal)
    chunk.char_end = block.char_end
    if block.page_from is not None:
        chunk.page_from = (
            block.page_from if chunk.page_from is None else min(chunk.page_from, block.page_from)
        )
    if block.page_to is not None:
        chunk.page_to = (
            block.page_to if chunk.page_to is None else max(chunk.page_to, block.page_to)
        )
    # The first clause reference in a chunk wins; a chunk that spans clauses is
    # ambiguous, and the splitter below avoids creating one.
    if chunk.clause_ref is None and block.clause_ref:
        chunk.clause_ref = block.clause_ref


def chunk_blocks(
    blocks: list[ParsedBlock],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[AssembledChunk]:
    chunks: list[AssembledChunk] = []
    current: AssembledChunk | None = None
    # Header text per table, so every row chunk can carry its column names.
    table_headers: dict[str, str] = {}

    def flush() -> None:
        nonlocal current
        if current is not None and current.text.strip():
            chunks.append(current)
        current = None

    for block in blocks:
        if block.block_type is BlockType.TABLE:
            table_headers[block.table_name or ""] = block.text
            flush()
            current = _new_chunk(block)
            _absorb(current, block, f"Table: {block.table_name}\n{block.text}")
            flush()
            continue

        if block.block_type is BlockType.TABLE_ROW:
            # One row, one chunk, prefixed with its table name and header.
            flush()
            header = table_headers.get(block.table_name or "")
            prefix = f"Table: {block.table_name}"
            if header:
                prefix += f"\nColumns: {header}"
            row_chunk = _new_chunk(block)
            _absorb(row_chunk, block, f"{prefix}\n{block.text}")
            chunks.append(row_chunk)
            current = None
            continue

        if block.block_type is BlockType.HEADING:
            # A heading opens a new chunk and leads the text that follows it.
            flush()
            current = _new_chunk(block)
            _absorb(current, block, block.text)
            continue

        text = block.text
        if current is None:
            current = _new_chunk(block)

        # Never let a chunk span two clauses: precedence and citation both key
        # on clause_ref, and a chunk covering 6.1 and 6.2 can cite neither.
        crosses_clause = (
            block.clause_ref is not None
            and current.clause_ref is not None
            and block.clause_ref != current.clause_ref
        )
        would_overflow = current.token_count + estimate_tokens(text) > max_tokens

        if current.text and (crosses_clause or would_overflow):
            tail = _tail_for_overlap(current.text, overlap_tokens) if would_overflow else ""
            flush()
            current = _new_chunk(block)
            if tail and not crosses_clause:
                current.text = tail
                current.token_count = estimate_tokens(tail)

        _absorb(current, block, text)

    flush()
    return _split_oversized(chunks, max_tokens)


def _tail_for_overlap(text: str, overlap_tokens: int) -> str:
    """Carry the end of the previous chunk forward so a sentence split across
    a boundary is still retrievable from either side."""
    if overlap_tokens <= 0:
        return ""
    approx_chars = overlap_tokens * 4
    if len(text) <= approx_chars:
        return text
    tail = text[-approx_chars:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail


def _split_oversized(chunks: list[AssembledChunk], max_tokens: int) -> list[AssembledChunk]:
    """A single block larger than the budget still has to fit somewhere."""
    result: list[AssembledChunk] = []
    limit_chars = max_tokens * 4
    for chunk in chunks:
        if chunk.token_count <= max_tokens * 1.5:
            result.append(chunk)
            continue
        text = chunk.text
        start = 0
        while start < len(text):
            end = min(start + limit_chars, len(text))
            if end < len(text):
                boundary = text.rfind(" ", start, end)
                if boundary > start:
                    end = boundary
            piece = text[start:end].strip()
            if piece:
                clone = AssembledChunk(
                    text=piece,
                    token_count=estimate_tokens(piece),
                    block_ids=list(chunk.block_ids),
                    block_ordinals=list(chunk.block_ordinals),
                    heading_path=list(chunk.heading_path),
                    clause_ref=chunk.clause_ref,
                    table_name=chunk.table_name,
                    page_from=chunk.page_from,
                    page_to=chunk.page_to,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    language=chunk.language,
                )
                result.append(clone)
            start = end
    return result
