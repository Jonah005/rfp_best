"""SourceLocator: the citation value object.

Deliberately NOT a table. A citation must stay stable even if the chunking
strategy changes, so it records where in a document version something was found
rather than pointing at a chunk row that a re-index could replace.

Every claim in the product resolves to one of these: evidence excerpts,
requirement text, knowledge facts and relations, and artifact statements.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class SourceLocator(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_version_id: uuid.UUID
    heading_path: list[str] = Field(default_factory=list)
    clause_ref: str | None = None
    table_name: str | None = None
    row_index: int | None = None
    col_index: int | None = None
    page_from: int | None = None
    page_to: int | None = None
    char_start: int | None = None
    char_end: int | None = None

    def as_json(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)

    def human_reference(self) -> str:
        """A short citation a reviewer can act on, e.g. 'p11 · 6.1 · Scope'."""
        parts: list[str] = []
        if self.page_from is not None:
            parts.append(
                f"p{self.page_from}"
                if self.page_to in (None, self.page_from)
                else f"pp{self.page_from}-{self.page_to}"
            )
        if self.clause_ref:
            parts.append(self.clause_ref)
        if self.table_name:
            cell = self.table_name
            if self.row_index is not None:
                cell += f" r{self.row_index}"
            parts.append(cell)
        elif self.heading_path and self.heading_path[-1] != self.clause_ref:
            # A numbered heading often IS the clause ref; do not print it twice.
            parts.append(self.heading_path[-1])
        return " · ".join(parts) or "location unrecorded"
