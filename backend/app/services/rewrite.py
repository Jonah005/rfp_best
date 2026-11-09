"""Rewording one exact piece of the draft, with only the context it needs.

A person selects a word, a sentence or a paragraph in the reading view and asks for
it to be reworded. The model sees only the pieces chosen and the sentences beside
them -- never the whole response -- and what it hands back is a SUGGESTION: the
person sees it beside the original and decides. Nothing here writes to the draft;
accepting goes through ``artifacts.edit_statement_text``, which decides what the
sentence is allowed to be afterwards.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound, ValidationError
from app.gateway.gateway import ModelGateway
from app.models import ArtifactStatement
from app.models.enums import StatementClass
from app.schemas.rewrite import RewriteResult
from app.services import artifacts
from app.services import diagrams as dg

MODES = ("rewrite", "simpler", "shorter", "formal", "custom")
MOST_PIECES = 40
_DREF = re.compile(r"\bD\d+\b")
_NUMBER = re.compile(r"\d[\d,.]*")
_TWO_SENTENCES = re.compile(r"[.!?][\"')\]]?\s+[A-Z]")
# Sentences whose backing a person's edit takes away.
_BACKED = (StatementClass.EVIDENCE_BACKED_FACT, StatementClass.TENDER_DERIVED)


def _kind(sentence: str, start: int, end: int, paragraph: bool) -> str:
    if paragraph:
        return "paragraph_sentence"
    piece = sentence[start:end].strip()
    if start <= 0 and end >= len(sentence):
        return "sentence"
    return "word" if len(piece.split()) <= 1 else "phrase"


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text).strip().lower()


def _clean(value: str, kind: str) -> str:
    text = " ".join(str(value).split()).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if kind in ("word", "phrase"):
        text = text.rstrip(".;")
    return text


def _complaints(piece: str, kind: str, new: str, allowed_numbers: set[str]) -> list[str]:
    out: list[str] = []
    if not new:
        out.append("a replacement was empty")
        return out
    if "[[" in new or "]]" in new:
        out.append("square brackets are not allowed")
    if _DREF.search(new):
        out.append("do not write diagram references like D1")
    if set(_NUMBER.findall(new)) - allowed_numbers:
        out.append("a figure appears that was not in the original or the instruction")
    if kind == "word" and len(new.split()) > 4:
        out.append("a single word must be replaced by a word or a very short phrase")
    if kind == "phrase" and len(new.split()) > max(12, 3 * len(piece.split())):
        out.append("a phrase was replaced by something far longer")
    if kind in ("sentence", "paragraph_sentence"):
        if _TWO_SENTENCES.search(new):
            out.append("a sentence must be replaced by exactly one sentence")
        if len(new) > max(260, 3 * len(piece)):
            out.append("the sentence became much longer than the original")
    return out


def preview(
    session: Session,
    ctx: ExecutionContext,
    gateway: ModelGateway,
    *,
    artifact_id: uuid.UUID,
    items: list[dict],
    mode: str,
    instruction: str | None,
    paragraph: bool = False,
    previous: list[str] | None = None,
) -> dict:
    """Suggest new wording for each piece. Items: {statement_id, start?, end?}.

    A piece is the characters ``start:end`` of the sentence, or all of it. Returns
    {suggestions: [{statement_id, start, end, before, after, full_after,
    becomes_own_wording}]}.
    """
    if mode not in MODES:
        raise ValidationError(f"Unknown rewrite mode '{mode}'. Allowed: {', '.join(MODES)}.")
    instruction = (instruction or "").strip() or None
    if mode == "custom" and not instruction:
        raise ValidationError("Say what you want changed.")
    if not items:
        raise ValidationError("Select something to reword.")
    if len(items) > MOST_PIECES:
        raise ValidationError(f"Reword at most {MOST_PIECES} sentences at a time.")

    if previous and len(previous) != len(items):
        raise ValidationError("Give one earlier suggestion for each sentence, or none.")
    artifact = artifacts._artifact(session, ctx, artifact_id)
    current = artifacts._current_version(session, ctx, artifact)
    rows = artifacts.statements_for_version(session, ctx, version_id=current.id)
    by_id = {row.id: row for row in rows}
    position = {row.id: i for i, row in enumerate(rows)}
    chosen = set()
    pieces: list[dict] = []
    for item in items:
        sid = uuid.UUID(str(item["statement_id"]))
        row = by_id.get(sid)
        if row is None:
            raise NotFound("That sentence is not in the current draft.")
        if sid in chosen:
            raise ValidationError("Each sentence can be reworded once at a time.")
        chosen.add(sid)
        start = int(item.get("start") or 0)
        end = item.get("end")
        end = len(row.text) if end is None else int(end)
        start = max(0, min(start, len(row.text)))
        end = max(start, min(end, len(row.text)))
        if not row.text[start:end].strip():
            raise ValidationError("Select some words to reword.")
        pieces.append({"row": row, "start": start, "end": end})

    def neighbour(row: ArtifactStatement, step: int) -> str | None:
        i = position[row.id] + step
        while 0 <= i < len(rows):
            other = rows[i]
            if other.id not in chosen and other.section == row.section:
                return other.text
            if other.id in chosen:
                i += step
                continue
            return None
        return None

    customer = _customer(session, ctx, artifact.task_id)
    payload_pieces = []
    for index, p in enumerate(pieces):
        row, start, end = p["row"], p["start"], p["end"]
        p["previous"] = (previous[index] if previous else "").strip() or None
        p["kind"] = _kind(row.text, start, end, paragraph)
        payload_pieces.append(
            {
                "kind": p["kind"],
                "piece": row.text[start:end],
                "sentence": row.text,
                "before": neighbour(row, -1),
                "after": neighbour(row, 1),
                "section": row.section,
                # An earlier suggestion the person did not accept: start from it and
                # change it the way the instruction says.
                "previous_suggestion": p["previous"],
            }
        )
    base = {
        "mode": mode,
        "instruction": instruction,
        "customer": customer,
        "pieces": payload_pieces,
    }
    allowed = [
        set(_NUMBER.findall(x["piece"])) | set(_NUMBER.findall(instruction or ""))
        for x in payload_pieces
    ]

    fix: list[str] | None = None
    for attempt in (1, 2):
        try:
            result = dg._ask(
                gateway,
                "text_rewrite",
                {**base, "fix_these": fix, "asked_at": datetime.now(timezone.utc).isoformat()},
                ctx=ctx,
                session=session,
                expect=RewriteResult,
            )
        except Exception as exc:  # noqa: BLE001 - reported once, in words
            if attempt == 2:
                raise ValidationError(f"The rewrite could not be used: {exc}") from exc
            fix = [f"Your last answer was rejected: {exc}"]
            continue
        if len(result.replacements) != len(pieces):
            problems = [f"give exactly {len(pieces)} replacements, one per piece"]
            cleaned: list[str] = []
        else:
            cleaned = [
                _clean(r, p["kind"]) for r, p in zip(result.replacements, pieces, strict=True)
            ]
            problems = []
            for i, (new, p) in enumerate(zip(cleaned, pieces, strict=True), start=1):
                piece = p["row"].text[p["start"] : p["end"]]
                found = _complaints(piece, p["kind"], new, allowed[i - 1])
                same = {_norm(piece), _norm(p["previous"] or "")} - {""}
                if _norm(new) in same:
                    found.append(
                        "it is the same wording as before -- change it as the instruction asks"
                        if instruction
                        else "it is the same wording as before -- reword it"
                    )
                problems += [f"piece {i}: {c}" for c in found]
        if not problems:
            break
        if attempt == 2:
            raise ValidationError("The rewrite broke the rules twice: " + problems[0])
        fix = problems
    suggestions = []
    for new, p in zip(cleaned, pieces, strict=True):
        row, start, end = p["row"], p["start"], p["end"]
        before = row.text[start:end]
        full = row.text[:start] + new + row.text[end:]
        suggestions.append(
            {
                "statement_id": row.id,
                "start": start,
                "end": end,
                "before": before,
                "after": new,
                "full_after": " ".join(full.split()),
                "becomes_own_wording": row.statement_class in _BACKED and full != row.text,
            }
        )
    return {"suggestions": suggestions}


def _customer(session: Session, ctx: ExecutionContext, task_id: uuid.UUID) -> str | None:
    from app.services import writeup

    try:
        return writeup.bid_context(session, ctx, task_id).get("customer")
    except Exception:  # noqa: BLE001 - the name is only a hint for the wording
        return None
