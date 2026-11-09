"""Joining approved answers into one report.

Each answer was written and approved on its own task. Assembly used to print
them one under another. This pass rewrites each approved answer as report
prose and writes the Executive Summary, so the proposal reads as one document.

The model proposes; code decides. Every rewritten paragraph is checked against
the statements it cites: a figure or capitalised name that is not in them, a
connective line that carries a figure or a promise, a statement left out, or a
citation to something it was not shown -- any of these rejects the rewrite and
assembly prints the approved wording unchanged. Nothing is stored in the
database: the result is cached beside the generated files, keyed by the
approved version, so a changed answer is rewritten again and an unchanged one
is not.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.core.errors import NotFound
from app.core.logging import get_logger
from app.models import (
    ArtifactVersion,
    ProposalSection,
    ResponseArtifact,
    SectionArtifactMap,
    Task,
)
from app.models.enums import ArtifactState, OutputMode
from app.schemas.report_edit import ChapterEdit
from app.services.storage import ObjectStorage, get_storage

log = get_logger(__name__)

CHUNK = 12
MAX_ATTEMPTS = 5
NUMBER = re.compile(r"\d[\d,.:/%\-]*")
NAME = re.compile(r"(?<=[a-z0-9,;)] )[A-Z][A-Za-z0-9\-]{2,}")
COMMITMENT = re.compile(
    r"\b(will|shall|guarantee[sd]?|commit(?:s|ted|ment)?|ensure[sd]?|warrant(?:s|y|ed)?|"
    r"promise[sd]?)\b",
    re.IGNORECASE,
)
# A line that only orients the reader may say "this section will set out ..."; it
# may not make the bidder promise anything.
CONNECTIVE_PROMISE = re.compile(
    r"\b(?:we|our|us)\b[^.]*\b(?:will|shall|guarantee[sd]?|commit(?:s|ted)?|ensure[sd]?|"
    r"warrant(?:s|y|ed)?|promise[sd]?)\b|\b(?:guarantee[sd]?|commitment|warrant(?:s|y|ed)?|"
    r"promise[sd]?)\b",
    re.IGNORECASE,
)
CONNECTIVE_WORDS = 40


def _numbers(text: str) -> set[str]:
    return {n.strip(".,:/-%") for n in NUMBER.findall(text) if n.strip(".,:/-%")}


def check_edit(
    sources: dict[str, str],
    paragraphs: list,
    *,
    allowed_names: set[str],
    require_all: bool = True,
    earlier: str = "",
) -> list[str]:
    """What is wrong with a rewrite, in words. An empty list means it may be used."""
    problems: list[str] = []
    cited: set[str] = set()
    for index, para in enumerate(paragraphs, start=1):
        refs = [r.strip().upper() for r in para.source_refs]
        bad = [r for r in refs if r not in sources]
        if bad:
            problems.append(f"paragraph {index} cites {', '.join(bad)}, which were not given")
        refs = [r for r in refs if r in sources]
        text = para.text
        if para.kind == "CONNECTIVE":
            if _numbers(text):
                problems.append(f"connective paragraph {index} carries a figure")
            if CONNECTIVE_PROMISE.search(text):
                problems.append(f"connective paragraph {index} makes a promise")
            if len(text.split()) > CONNECTIVE_WORDS:
                problems.append(f"connective paragraph {index} is too long")
            known = allowed_names | set(
                re.findall(r"[A-Z][A-Za-z0-9\-]{2,}", " ".join(sources.values()))
            )
            for name in NAME.findall(text):
                if name not in known:
                    problems.append(f"connective paragraph {index} introduces '{name}'")
            continue
        if not refs:
            problems.append(f"paragraph {index} cites no source")
            continue
        cited.update(refs)
        # `earlier` is the part of the chapter already written: a figure or name
        # in it came from approved statements, so a later part may refer to it.
        blob = " ".join([*(sources[r] for r in refs), earlier])
        for number in sorted(_numbers(text) - _numbers(blob)):
            problems.append(f"paragraph {index} has the figure '{number}', not in its sources")
        source_names = set(re.findall(r"[A-Z][A-Za-z0-9\-]{2,}", blob)) | allowed_names
        for name in NAME.findall(text):
            if name not in source_names:
                problems.append(f"paragraph {index} introduces '{name}', not in its sources")
    if require_all:
        for ref in sources:
            if ref not in cited:
                problems.append(f"{ref} was left out")
    return problems


def _key(ctx: ExecutionContext, bid_id: uuid.UUID, *parts: str) -> str:
    return ObjectStorage.tenant_key(ctx.org_id, "report-edit-r4", str(bid_id), *parts)


def _statement_texts(session: Session, ctx: ExecutionContext, version: ArtifactVersion):
    from app.services.release import render_version

    text = render_version(session, ctx, version=version, output_mode=OutputMode.SUBMISSION)
    return [t.strip() for t in text.split("\n\n") if t.strip()]


def _ask(
    session,
    ctx,
    gateway,
    call,
    sources,
    *,
    title,
    neighbours,
    require_all=True,
    already_written=None,
):
    from app.gateway.registry import wrap_untrusted

    payload = {
        "chapter": title,
        "other_topics": neighbours,
        "statements": [{"ref": ref, "text": text} for ref, text in sources.items()],
    }
    if already_written:
        payload["already_written"] = already_written
    feedback: list[str] = []
    for _ in range(2):
        body = dict(payload)
        if feedback:
            body["fix_these_problems"] = feedback
        result = gateway.generate_structured(
            call,
            wrap_untrusted(json.dumps(body, ensure_ascii=False)),
            ctx=ctx,
            session=session,
            cache=not feedback,
        )
        if not isinstance(result, ChapterEdit):
            return None, ["the model returned nothing usable"]
        allowed = set(re.findall(r"[A-Z][A-Za-z0-9\-]{2,}", " ".join([title, *neighbours])))
        feedback = check_edit(
            sources,
            result.paragraphs,
            allowed_names=allowed,
            require_all=require_all,
            earlier=" ".join(already_written or []),
        )
        if not feedback:
            return result, []
    return None, feedback


def edit_artifact(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    artifact: ResponseArtifact,
    chapter_title: str,
    neighbours: list[str],
    gateway,
    storage: ObjectStorage,
    force: bool = False,
    retry_failed: bool = False,
) -> dict:
    version = session.get(ArtifactVersion, artifact.approved_version_id)
    if version is None:
        raise NotFound("That answer has no approved version to read.")
    key = _key(ctx, bid_id, f"{version.id}.json")
    attempts = 0
    if not force and storage.exists(key):
        record = json.loads(storage.get(key))
        attempts = record.get("attempts", 1)
        if "failed" not in record or not retry_failed or attempts >= MAX_ATTEMPTS:
            return record
    statements = _statement_texts(session, ctx, version)
    paragraphs: list[dict] = []
    failures: list[str] = []
    for start in range(0, len(statements), CHUNK):
        chunk = statements[start : start + CHUNK]
        sources = {f"S{i}": text for i, text in enumerate(chunk, start=1)}
        result, problems = _ask(
            session,
            ctx,
            gateway,
            "report_chapter_edit",
            sources,
            title=f"{chapter_title} / {artifact.title}",
            neighbours=neighbours,
            already_written=[p["text"][:400] for p in paragraphs] or None,
        )
        if result is None:
            failures.extend(problems)
            break
        # A later part of a long chapter continues the earlier part: it must not
        # open the chapter again with an orienting line of its own.
        paragraphs.extend(
            {"text": p.text, "kind": p.kind}
            for p in result.paragraphs
            if not (paragraphs and p.kind == "CONNECTIVE")
        )
    record = (
        {"failed": failures, "attempts": attempts + 1} if failures else {"paragraphs": paragraphs}
    )
    storage.put(key, json.dumps(record).encode(), content_type="application/json")
    return record


def _eligible(session: Session, ctx: ExecutionContext, *, structure_id: uuid.UUID):
    rows = session.execute(
        select(ProposalSection, SectionArtifactMap, ResponseArtifact)
        .join(SectionArtifactMap, SectionArtifactMap.section_id == ProposalSection.id)
        .join(ResponseArtifact, ResponseArtifact.id == SectionArtifactMap.artifact_id)
        .where(
            ProposalSection.org_id == ctx.org_id,
            ProposalSection.structure_id == structure_id,
            ResponseArtifact.state == ArtifactState.APPROVED,
            ResponseArtifact.approved_version_id.is_not(None),
        )
        .order_by(ProposalSection.order_index, SectionArtifactMap.order_index)
    ).all()
    return rows


def edit_bid(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    limit: int = 1,
    force: bool = False,
    retry_failed: bool = False,
    gateway=None,
    storage: ObjectStorage | None = None,
) -> dict:
    """Rewrite up to `limit` approved answers that have no current rewrite, then the summary."""
    from app.gateway.gateway import build_gateway
    from app.services.release import structure_for_bid

    storage = storage or get_storage()
    gateway = gateway or build_gateway()
    structure = structure_for_bid(session, ctx, bid_id=bid_id)
    rows = _eligible(session, ctx, structure_id=structure.id)
    by_section: dict[uuid.UUID, list[str]] = {}
    for section, _map, artifact in rows:
        by_section.setdefault(section.id, []).append(artifact.title)

    edited = failed = remaining = 0
    reasons: list[str] = []
    for section, _map, artifact in rows:
        key = _key(ctx, bid_id, f"{artifact.approved_version_id}.json")
        if not force and storage.exists(key):
            old = json.loads(storage.get(key))
            if "paragraphs" in old or not retry_failed or old.get("attempts", 1) >= MAX_ATTEMPTS:
                continue
        if edited + failed >= limit:
            remaining += 1
            continue
        neighbours = [t for t in by_section[section.id] if t != artifact.title]
        record = edit_artifact(
            session,
            ctx,
            bid_id=bid_id,
            artifact=artifact,
            chapter_title=section.title,
            neighbours=neighbours,
            gateway=gateway,
            storage=storage,
            force=force,
            retry_failed=retry_failed,
        )
        if "failed" in record:
            failed += 1
            reasons.append(f"{artifact.title}: " + "; ".join(record["failed"][:4]))
        else:
            edited += 1

    summary = "none"
    if remaining == 0 and edited + failed == 0:
        summary = _summary(
            session,
            ctx,
            bid_id=bid_id,
            rows=rows,
            gateway=gateway,
            storage=storage,
            force=force,
            retry_failed=retry_failed,
        )
        if summary == "failed":
            record = json.loads(storage.get(_key(ctx, bid_id, "summary-v2.json")))
            reasons.append("Executive Summary: " + "; ".join(record.get("failed", [])[:4]))
    return {
        "edited": edited,
        "failed": failed,
        "remaining": remaining,
        "summary": summary,
        "reasons": reasons,
    }


def _summary(session, ctx, *, bid_id, rows, gateway, storage, force, retry_failed=False) -> str:
    """The Executive Summary, from the opening paragraphs of every rewritten answer."""
    key = _key(ctx, bid_id, "summary-v2.json")
    attempts = 0
    sources: dict[str, str] = {}
    for _section, _map, artifact in rows:
        edited_key = _key(ctx, bid_id, f"{artifact.approved_version_id}.json")
        if not storage.exists(edited_key):
            continue
        record = json.loads(storage.get(edited_key))
        for para in [p for p in record.get("paragraphs", []) if p["kind"] == "BODY"][:2]:
            sources[f"S{len(sources) + 1}"] = para["text"]
    # The summary is written from these paragraphs; when they change (an answer was
    # redrafted and rewritten) a summary written from the old ones is out of date.
    fingerprint = hashlib.sha256(
        json.dumps(sources, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    if not force and storage.exists(key):
        old = json.loads(storage.get(key))
        attempts = old.get("attempts", 1)
        if "paragraphs" in old and old.get("fingerprint") == fingerprint:
            return "done"
        if "paragraphs" not in old and (not retry_failed or attempts >= MAX_ATTEMPTS):
            return "failed"
    if not sources:
        return "none"
    sources = dict(list(sources.items())[:80])
    result, problems = _ask(
        session,
        ctx,
        gateway,
        "report_summary",
        sources,
        title="Executive Summary",
        neighbours=[],
        require_all=False,
    )
    if result is None:
        storage.put(key, json.dumps({"failed": problems}).encode(), content_type="application/json")
        return "failed"
    storage.put(
        key,
        json.dumps(
            {
                "paragraphs": [{"text": p.text, "kind": p.kind} for p in result.paragraphs],
                "fingerprint": fingerprint,
            }
        ).encode(),
        content_type="application/json",
    )
    return "done"


# Labels the review copy puts on approved wording, and the filler assumption the
# drafting step adds when nobody has said anything specific. Neither belongs in
# a report a customer reads.
_TAGS = re.compile(
    r"\s*\[(?:Proposed commitment[^\]]*|Assumption|From the tender|Describes a diagram|"
    r"Written as a commitment[^\]]*|Joined from[^\]]*)\]"
)
_FILLER = re.compile(
    r"We have assumed (?:the information needed is available in the RFP and standard "
    r"service documentation|the details follow the RFP and our standard practice)"
    r"[^.]*design phase\.\s*"
)


def _without_internal_sentences(paragraph: str) -> str:
    """The system's own working notes ("no approved company evidence is held") describe the
    review, not the proposal; whatever drafted them, they do not reach a customer document."""
    from app.services.package_lint import INTERNAL_WORDING

    if not INTERNAL_WORDING.search(paragraph):
        return paragraph
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    return " ".join(x for x in sentences if not INTERNAL_WORDING.search(x))


def clean_for_report(text: str) -> str:
    """Approved wording without review labels, list numbers or the filler assumption."""
    out: list[str] = []
    for paragraph in text.split("\n\n"):
        paragraph = _TAGS.sub("", paragraph)
        paragraph = _FILLER.sub("", paragraph)
        paragraph = re.sub(r"^\d{1,3}\.\s+", "", paragraph.strip())
        paragraph = re.sub(r"^ASSUMPTION:\s*", "", paragraph)
        paragraph = _without_internal_sentences(paragraph)
        if paragraph.strip():
            out.append(paragraph.strip())
    return "\n\n".join(out)


def edited_text(
    ctx: ExecutionContext, *, bid_id: uuid.UUID, version_id: uuid.UUID, storage: ObjectStorage
) -> str | None:
    key = _key(ctx, bid_id, f"{version_id}.json")
    if not storage.exists(key):
        return None
    record = json.loads(storage.get(key))
    if "paragraphs" not in record:
        return None
    return clean_for_report("\n\n".join(p["text"] for p in record["paragraphs"]))


def summary_text(ctx: ExecutionContext, *, bid_id: uuid.UUID, storage: ObjectStorage) -> str | None:
    key = _key(ctx, bid_id, "summary-v2.json")
    if not storage.exists(key):
        return None
    record = json.loads(storage.get(key))
    if "paragraphs" not in record:
        return None
    return "\n\n".join(p["text"] for p in record["paragraphs"])


def task_code(session: Session, artifact: ResponseArtifact) -> str:
    task = session.get(Task, artifact.task_id)
    return task.code if task else ""
