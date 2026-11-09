"""One edited chapter per topic, and one register of assumptions.

The joining pass rewrote each approved answer on its own, so a topic answered
by several tasks still reads as several answers that repeat each other. This
pass writes each chapter once, within a word budget, and moves every assumption
out of the prose into one numbered register.

The model proposes; code decides. A chapter is accepted only if every
paragraph cites the source paragraphs it draws on, none carries a figure or a
capitalised name its sources do not, every figure in the sources survives
somewhere (in the text or in the assumptions), and the length is within budget.
A chapter that fails keeps its joined wording, unchanged. Results are cached in
object storage, keyed by a hash of the chapter's sources, so a changed answer is
written again and an unchanged one is not.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.models import ArtifactVersion
from app.schemas.report_consolidate import (
    AssumptionRegister,
    ChapterAssumption,
    ChapterParagraph,
    ConsolidatedChapter,
)
from app.services import report_edit
from app.services.storage import ObjectStorage, get_storage

REGISTER_PART = 40
REGISTER_TARGET = 25
REGISTER_ROUNDS = 6
MAX_ATTEMPTS = 5
KEEP_SHARE = 0.45
FLOOR_WORDS = 120
SLACK = 1.6
SHORT_CHAPTER = 160
NAME = report_edit.NAME
NAMES = re.compile(r"[A-Z][A-Za-z0-9\-]{2,}")


def _key(ctx: ExecutionContext, bid_id: uuid.UUID, name: str) -> str:
    return ObjectStorage.tenant_key(ctx.org_id, "report-consolidate-r2", str(bid_id), name)


def _words(text: str) -> int:
    return len(text.split())


def budget_for(source_words: int) -> int:
    if source_words <= SHORT_CHAPTER:
        return source_words
    return max(FLOOR_WORDS, int(source_words * KEEP_SHARE))


def check_chapter(
    sources: dict[str, str],
    result: ConsolidatedChapter,
    *,
    budget: int,
    title: str,
) -> list[str]:
    """What is wrong with a consolidated chapter, in words. Empty means it may be used."""
    problems: list[str] = []
    allowed = set(NAMES.findall(title))
    cited: set[str] = set()
    body_words = 0
    output = []
    for index, para in enumerate(result.paragraphs, start=1):
        refs = [r.strip().upper() for r in para.source_refs]
        bad = [r for r in refs if r not in sources]
        if bad:
            problems.append(f"paragraph {index} cites {', '.join(bad)}, which were not given")
        refs = [r for r in refs if r in sources]
        output.append(para.text)
        if para.kind == "SUBHEADING":
            if report_edit._numbers(para.text):
                para.text = (
                    " ".join(re.sub(r"(?<![\w-])\d[\d.,/]*(?![\w-])", " ", para.text).split())
                    or "Details"
                )
            if _words(para.text) > 10:
                problems.append(f"sub-heading {index} is longer than ten words")
            continue
        body_words += _words(para.text)
        if not refs:
            problems.append(f"paragraph {index} cites no source")
            continue
        cited.update(refs)
        blob = " ".join(sources[r] for r in refs)
        for number in sorted(report_edit._numbers(para.text) - report_edit._numbers(blob)):
            problems.append(f"paragraph {index} has the figure '{number}', not in its sources")
        known = set(NAMES.findall(blob)) | allowed
        for name in NAME.findall(para.text):
            if name not in known:
                problems.append(f"paragraph {index} introduces '{name}', not in its sources")
    for n, item in enumerate(result.assumptions, start=1):
        refs = [r.strip().upper() for r in item.source_refs]
        if not refs:
            problems.append(f"assumption {n} cites no paragraph")
            continue
        if any(r not in sources for r in refs):
            problems.append(f"assumption {n} cites a paragraph that was not given")
            continue
        cited.update(refs)
        blob = " ".join(sources[r] for r in refs)
        for number in sorted(report_edit._numbers(item.text) - report_edit._numbers(blob)):
            problems.append(f"assumption {n} has the figure '{number}', not in its sources")
        output.append(item.text)
    kept = report_edit._numbers(" ".join(output))
    lost = sorted(report_edit._numbers(" ".join(sources.values())) - kept)
    if lost:
        problems.append(f"these figures from the sources are missing: {', '.join(lost[:15])}")
    source_total = sum(_words(t) for t in sources.values())
    if body_words > max(budget * SLACK, source_total * 0.8):
        problems.append(f"the chapter is {body_words} words; the budget is {budget}")
    return problems


def _split(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def chapter_sources(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    artifacts: list,
    storage: ObjectStorage,
) -> dict[str, str]:
    """The joined paragraphs of every approved answer in a chapter, numbered P1, P2, ..."""
    from app.services.release import render_version

    out: list[str] = []
    for artifact in artifacts:
        version = session.get(ArtifactVersion, artifact.approved_version_id)
        if version is None:
            continue
        joined = report_edit.edited_text(ctx, bid_id=bid_id, version_id=version.id, storage=storage)
        if joined is None:
            from app.models.enums import OutputMode

            joined = report_edit.clean_for_report(
                render_version(session, ctx, version=version, output_mode=OutputMode.SUBMISSION)
            )
        out.extend(_split(joined))
    return {f"P{i}": t for i, t in enumerate(out, start=1)}


def _fingerprint(sources: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:20]


WINDOW_WORDS = 2500


def _windows(sources: dict[str, str]) -> list[dict[str, str]]:
    """Consecutive paragraphs in groups of about WINDOW_WORDS, so no single reply is huge."""
    groups: list[dict[str, str]] = []
    current: dict[str, str] = {}
    count = 0
    for ref, text in sources.items():
        words = _words(text)
        if current and count + words > WINDOW_WORDS:
            groups.append(current)
            current, count = {}, 0
        current[ref] = text
        count += words
    if current:
        groups.append(current)
    return groups


def _ask_window(session, ctx, gateway, *, title, sources, budget, neighbours):
    payload = {
        "chapter": title,
        "other_chapters": neighbours,
        "word_budget": budget,
        "paragraphs": [{"ref": r, "text": t} for r, t in sources.items()],
    }
    feedback: list[str] = []
    for _ in range(2):
        body = dict(payload)
        if feedback:
            body["fix_these_problems"] = feedback
        try:
            result = gateway_call(gateway, ctx, session, body, cache=not feedback)
        except Exception as exc:  # noqa: BLE001 - a failed chapter must not stop the others
            return None, [f"the model call failed: {str(exc)[:160]}"]
        if not isinstance(result, ConsolidatedChapter):
            return None, ["the model returned nothing usable"]
        feedback = check_chapter(sources, result, budget=budget, title=title)
        if not feedback:
            return result, []
    return None, feedback


def _ask(session, ctx, gateway, *, title, sources, budget, neighbours):
    """One chapter, asked in windows when it is long; all windows must pass."""
    windows = _windows(sources)
    if len(windows) == 1:
        return _ask_window(
            session,
            ctx,
            gateway,
            title=title,
            sources=sources,
            budget=budget,
            neighbours=neighbours,
        )
    total = sum(_words(t) for t in sources.values()) or 1
    paragraphs: list[ChapterParagraph] = []
    assumptions: list[ChapterAssumption] = []
    for part in windows:
        share = sum(_words(t) for t in part.values()) / total
        result, problems = _ask_window(
            session,
            ctx,
            gateway,
            title=title,
            sources=part,
            budget=max(budget_for(sum(_words(t) for t in part.values())), int(budget * share)),
            neighbours=neighbours,
        )
        if result is None:
            # this window could not be edited safely: keep its approved wording as it is
            paragraphs.extend(
                ChapterParagraph(kind="BODY", text=text, source_refs=[ref])
                for ref, text in part.items()
            )
            continue
        paragraphs.extend(result.paragraphs)
        assumptions.extend(result.assumptions)
    return ConsolidatedChapter(paragraphs=paragraphs, assumptions=assumptions), []


def gateway_call(gateway, ctx, session, body, *, cache):
    from app.gateway.registry import wrap_untrusted

    return gateway.generate_structured(
        "report_consolidate",
        wrap_untrusted(json.dumps(body, ensure_ascii=False)),
        ctx=ctx,
        session=session,
        cache=cache,
    )


def consolidate_section(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    section,
    artifacts: list,
    neighbours: list[str],
    gateway,
    storage: ObjectStorage,
    retry_failed: bool = False,
) -> dict | None:
    """The cached record for a chapter, writing it first when it has none. None: no text."""
    sources = chapter_sources(session, ctx, bid_id=bid_id, artifacts=artifacts, storage=storage)
    if not sources:
        return None
    key = _key(ctx, bid_id, f"{section.id}-{_fingerprint(sources)}.json")
    attempts = 0
    if storage.exists(key):
        record = json.loads(storage.get(key))
        attempts = record.get("attempts", 1)
        if "failed" not in record or not retry_failed or attempts >= MAX_ATTEMPTS:
            return record
    source_words = sum(_words(t) for t in sources.values())
    budget = budget_for(source_words)
    result, problems = _ask(
        session,
        ctx,
        gateway,
        title=section.title,
        sources=sources,
        budget=budget,
        neighbours=neighbours,
    )
    if result is None:
        record = {"failed": problems, "attempts": attempts + 1, "source_words": source_words}
    else:
        record = {
            "paragraphs": [{"kind": p.kind, "text": p.text} for p in result.paragraphs],
            "assumptions": [a.text for a in result.assumptions],
            "source_words": source_words,
            "words": sum(_words(p.text) for p in result.paragraphs if p.kind == "BODY"),
        }
    storage.put(key, json.dumps(record).encode(), content_type="application/json")
    return record


def _chapters(session: Session, ctx: ExecutionContext, bid_id: uuid.UUID):
    """Technical-stream chapters with approved answers: (section, [artifacts])."""
    from app.models import GeneratedDocument
    from app.models.enums import DocumentStream
    from app.services.release import structure_for_bid

    structure = structure_for_bid(session, ctx, bid_id=bid_id)
    grouped: dict[uuid.UUID, tuple] = {}
    for section, _map, artifact in report_edit._eligible(session, ctx, structure_id=structure.id):
        document = (
            session.get(GeneratedDocument, section.target_document_id)
            if section.target_document_id
            else None
        )
        if document is not None and document.stream is not DocumentStream.TECHNICAL:
            continue
        grouped.setdefault(section.id, (section, []))[1].append(artifact)
    return list(grouped.values())


def consolidate_bid(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    limit: int = 1,
    retry_failed: bool = False,
    gateway=None,
    storage: ObjectStorage | None = None,
) -> dict:
    """Consolidate up to `limit` chapters that have no current record, then the register."""
    from app.gateway.gateway import build_gateway

    storage = storage or get_storage()
    gateway = gateway or build_gateway()
    chapters = _chapters(session, ctx, bid_id)
    titles = [s.title for s, _ in chapters]
    done = failed = remaining = 0
    reasons: list[str] = []
    records: list[tuple[str, dict]] = []
    for section, artifacts in chapters:
        sources = chapter_sources(session, ctx, bid_id=bid_id, artifacts=artifacts, storage=storage)
        if not sources:
            continue
        key = _key(ctx, bid_id, f"{section.id}-{_fingerprint(sources)}.json")
        stale = True
        if storage.exists(key):
            old = json.loads(storage.get(key))
            stale = (
                "paragraphs" not in old and retry_failed and old.get("attempts", 1) < MAX_ATTEMPTS
            )
        if stale and done + failed >= limit:
            remaining += 1
            continue
        record = consolidate_section(
            session,
            ctx,
            bid_id=bid_id,
            section=section,
            artifacts=artifacts,
            neighbours=[t for t in titles if t != section.title][:12],
            gateway=gateway,
            storage=storage,
            retry_failed=retry_failed,
        )
        if record is None:
            continue
        if stale:
            if "failed" in record:
                failed += 1
                reasons.append(f"{section.title}: " + "; ".join(record["failed"][:3]))
            else:
                done += 1
        records.append((section.title, record))
    register = "none"
    if remaining == 0 and done + failed == 0:
        register = _register(
            session,
            ctx,
            bid_id=bid_id,
            records=records,
            gateway=gateway,
            storage=storage,
            retry_failed=retry_failed,
        )
        if register == "failed":
            old = json.loads(storage.get(_key(ctx, bid_id, "register.json")))
            reasons.append("Assumptions register: " + "; ".join(old.get("failed", [])[:3]))
    return {
        "consolidated": done,
        "failed": failed,
        "remaining": remaining,
        "assumptions_register": register,
        "reasons": reasons,
    }


HOLDINGS_CLAIM = re.compile(
    r"\b(?:references?|referees?|clients?|customers?)\b[^.]{0,40}\bwe\s+(?:hold|have)\b"
    r"|\bwe\s+(?:hold|have\s+(?:obtained|received|secured|confirmed))\b[^.]{0,60}\b(?:references?|letters?|consents?|certificat\w+|approvals?)\b"
    r"|\bconsent\w*\b[^.]{0,80}\b(?:contacts?|referees?|references?)\b"
    r"|\b(?:contacts?|referees?|references?)\b[^.]{0,80}\bconsent\w*\b",
    re.IGNORECASE,
)


def _register(session, ctx, *, bid_id, records, gateway, storage, retry_failed) -> str:
    from app.gateway.registry import wrap_untrusted

    key = _key(ctx, bid_id, "register.json")
    texts: list[str] = []
    for _title, record in records:
        # An assumption is about the customer or the scope. A claim about what the bidder
        # holds or what third parties have agreed to is a company fact; the company's own
        # documents say whether it is true, so it is not carried into the assumptions register.
        texts.extend(t for t in record.get("assumptions", []) if not HOLDINGS_CLAIM.search(t or ""))
    sources = {f"A{i}": t for i, t in enumerate(texts, start=1)}
    fingerprint = _fingerprint({**sources, "format": "parts-9"})
    attempts = 0
    if storage.exists(key):
        old = json.loads(storage.get(key))
        attempts = old.get("attempts", 1)
        if old.get("fingerprint") == fingerprint and "items" in old and "categories" in old:
            return "done"
        if (
            old.get("fingerprint") == fingerprint
            and ("items" not in old)
            and (not retry_failed or attempts >= MAX_ATTEMPTS)
        ):
            return "failed"
    if not sources:
        if storage.exists(key):
            # Nothing is left to register; an earlier register must not outlive its sources.
            storage.put(
                key,
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "items": [],
                        "categories": [],
                        "attempts": attempts,
                    }
                ).encode(),
                content_type="application/json",
            )
        return "none"
    # A long list is registered in parts (one reply holding every item overruns the model's
    # output limit), then the joined list is read again as a whole until it is short enough.
    # Nothing here can fail the build: an item the model gets wrong (a figure or name that is
    # not in its sources, or a source left out) is replaced by the original sentences it
    # cited, so the register is only ever shorter or equal, never wrong.
    names = list(sources)
    parts = [names[i : i + REGISTER_PART] for i in range(0, len(names), REGISTER_PART)]
    problems: list[str] = []
    notes: list[str] = []

    def one_pass(part_sources: dict[str, str], target: int | None = None):
        for _ in range(3):
            body: dict = {"assumptions": [{"ref": r, "text": t} for r, t in part_sources.items()]}
            if target:
                body["target_item_count"] = target
            try:
                result = gateway.generate_structured(
                    "assumption_register",
                    wrap_untrusted(json.dumps(body, ensure_ascii=False)),
                    ctx=ctx,
                    session=session,
                    cache=False,
                )
            except Exception as error:  # a provider error must not lose the whole register
                notes.append(f"model call failed: {str(error)[:120]}")
                continue
            if isinstance(result, AssumptionRegister):
                return result.items
            notes.append("the model returned nothing usable")
        return None

    merged: list = []
    for part in parts:
        part_sources = {r: sources[r] for r in part}
        got = one_pass(part_sources)
        merged.extend(_settle(part_sources, {}, got or []))
    merged = _without_repeats(merged)
    counts = [len(merged)]
    stalls = 0
    for _round in range(REGISTER_ROUNDS):
        if len(merged) <= REGISTER_TARGET * 1.3:
            break
        joined = {f"A{n}": item.text for n, item in enumerate(merged, start=1)}
        cats = {f"A{n}": item.category for n, item in enumerate(merged, start=1)}
        got = one_pass(joined, target=REGISTER_TARGET)
        if got is None:
            stalls += 1
        else:
            again = _settle(joined, cats, got)
            for item in again:
                refs = [r.strip().upper() for r in getattr(item, "source_refs", [])]
                if item.category != "CUSTOMER_DEPENDENCY" and any(
                    cats.get(r) == "CUSTOMER_DEPENDENCY" for r in refs
                ):
                    item.category = "CUSTOMER_DEPENDENCY"
            counts.append(len(again))
            if len(again) < len(merged):
                merged = again  # only ever keep a shorter register
                stalls = 0
            else:
                stalls += 1
        if stalls >= 2:
            break
    second_pass_note = notes[:6] + [f"item counts by round: {counts}"]
    items = SimpleNamespace(items=merged)
    if items is None:
        storage.put(
            key,
            json.dumps(
                {"failed": problems, "attempts": attempts + 1, "fingerprint": fingerprint}
            ).encode(),
            content_type="application/json",
        )
        return "failed"
    storage.put(
        key,
        json.dumps(
            {
                "items": [i.text for i in items.items],
                "categories": [i.category for i in items.items],
                "fingerprint": fingerprint,
                "from": len(sources),
                "second_pass_problems": second_pass_note[:6],
            }
        ).encode(),
        content_type="application/json",
    )
    return "done"


def _item_problems(sources: dict[str, str], item) -> list[str]:
    """What is wrong with one register item, judged against the sentences it cites."""
    refs = [r.strip().upper() for r in item.source_refs]
    if not refs:
        return ["cites no assumption"]
    if any(r not in sources for r in refs):
        return ["cites an assumption that was not given"]
    problems: list[str] = []
    blob = " ".join(sources[r] for r in refs)
    for number in sorted(report_edit._numbers(item.text) - report_edit._numbers(blob)):
        problems.append(f"has the figure '{number}', not in its sources")
    known = set(NAMES.findall(blob))
    for name in NAME.findall(item.text):
        if name not in known:
            problems.append(f"introduces '{name}', not in its sources")
    return problems


def _settle(sources: dict[str, str], cats: dict[str, str], items: list) -> list:
    """Keep the model's items that hold up; put the original sentences back for the rest.

    Every source ends up cited by exactly one item, and no item says more than its
    sources do.
    """
    out: list = []
    cited: set[str] = set()

    def original(ref: str) -> None:
        if ref not in cited:
            cited.add(ref)
            out.append(
                SimpleNamespace(
                    text=sources[ref],
                    category=cats.get(ref, "TECHNICAL_DECISION"),
                    source_refs=[ref],
                )
            )

    for item in items:
        refs = list(dict.fromkeys(r.strip().upper() for r in item.source_refs))
        valid = [r for r in refs if r in sources and r not in cited]
        if not valid:
            continue
        if _item_problems(sources, item):
            for ref in valid:
                original(ref)
            continue
        cited.update(valid)
        item.source_refs = valid
        out.append(item)
    for ref in sources:
        original(ref)
    return out


def _without_repeats(items: list) -> list:
    """Parts are written separately, so the same dependency can come back from two parts.
    An item whose wording mostly repeats an earlier one is dropped; nothing is reworded."""
    kept: list = []
    seen: list[set[str]] = []
    for item in items:
        words = set(re.findall(r"[a-z0-9]{4,}", item.text.lower()))
        if words and any(len(words & old) / len(words | old) > 0.5 for old in seen):
            continue
        kept.append(item)
        seen.append(words)
    return kept


def check_register(sources: dict[str, str], result: AssumptionRegister) -> list[str]:
    problems: list[str] = []
    cited: set[str] = set()
    for n, item in enumerate(result.items, start=1):
        refs = [r.strip().upper() for r in item.source_refs]
        if not refs:
            problems.append(f"item {n} cites no assumption")
            continue
        if any(r not in sources for r in refs):
            problems.append(f"item {n} cites an assumption that was not given")
            continue
        cited.update(refs)
        blob = " ".join(sources[r] for r in refs)
        for number in sorted(report_edit._numbers(item.text) - report_edit._numbers(blob)):
            problems.append(f"item {n} has the figure '{number}', not in its sources")
        if HOLDINGS_CLAIM.search(item.text) and not HOLDINGS_CLAIM.search(blob):
            # The model may merge two harmless sentences into a claim about what the bidder
            # holds or what third parties agreed to; that is a company fact, not an assumption.
            problems.append(f"item {n} claims what the bidder holds or third parties agreed to")
        known = set(NAMES.findall(blob))
        for name in NAME.findall(item.text):
            if name not in known:
                problems.append(f"item {n} introduces '{name}', not in its sources")
    for ref in sources:
        if ref not in cited:
            problems.append(f"{ref} was left out")
    return problems


# --- reading the results back ----------------------------------------------------


def chapter_text(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    section,
    artifacts: list,
    storage: ObjectStorage,
    marks: bool = True,
    appendix_out: list[str] | None = None,
) -> str | None:
    """The consolidated chapter as report text, or None when there is no current one."""
    sources = chapter_sources(session, ctx, bid_id=bid_id, artifacts=artifacts, storage=storage)
    if not sources:
        return None
    key = _key(ctx, bid_id, f"{section.id}-{_fingerprint(sources)}.json")
    if not storage.exists(key):
        return None
    record = json.loads(storage.get(key))
    if "paragraphs" not in record:
        return None
    from app.services import report_figures

    figures = report_figures.chapter_figures(
        session,
        ctx,
        bid_id=bid_id,
        artifacts=artifacts,
        paragraphs=record["paragraphs"],
        storage=storage,
        marks=marks,
    )
    after: dict[int, list[dict]] = {}
    for figure in figures:
        if figure.get("appendix") and appendix_out is not None:
            appendix_out.append(
                report_figures.marker(figure["png_key"], figure["caption"], figure.get("alt", ""))
            )
            continue
        after.setdefault(figure["after"], []).append(figure)
    parts: list[str] = []
    for index, p in enumerate(record["paragraphs"], start=1):
        parts.append(
            f"### {_repair_heading(p['text'])}" if p["kind"] == "SUBHEADING" else p["text"]
        )
        for figure in after.get(index, []):
            parts.append(
                report_figures.marker(figure["png_key"], figure["caption"], figure.get("alt", ""))
            )
    return "\n\n".join(parts)


def _repair_heading(text: str) -> str:
    """Headings written before figures were kept inside identifiers lost their digits."""
    text = re.sub(r"\bB B\b", "B2B", text)
    # A heading must not promise a deliverable that is still only planned.
    text = re.sub(r"\bLoad Test Results\b", "Load Test Approach", text)
    return re.sub(r"\s+for\s+[A-Z]-$", "", text).strip() or text


_CATEGORY_NAMES = {
    "CUSTOMER_DEPENDENCY": "Customer dependency",
    "COMMERCIAL": "Commercial",
    "TECHNICAL_DECISION": "Technical decision to confirm",
    "INTERNAL_DRAFTING": "Internal drafting note",
}
_CUSTOMER_FACING = ("CUSTOMER_DEPENDENCY", "COMMERCIAL")


def register_text(
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    storage: ObjectStorage,
    submission: bool = False,
) -> str | None:
    """The assumptions table. The customer's copy keeps only what affects them; the review
    copy lists every item with its category so the rest can be settled before submission."""
    key = _key(ctx, bid_id, "register.json")
    if not storage.exists(key):
        return None
    record = json.loads(storage.get(key))
    if "items" not in record:
        return None
    categories = record.get("categories") or ["TECHNICAL_DECISION"] * len(record["items"])
    order = ["CUSTOMER_DEPENDENCY", "COMMERCIAL", "TECHNICAL_DECISION", "INTERNAL_DRAFTING"]
    kept = [
        (c, " ".join(t.split()))
        for t, c in zip(record["items"], categories, strict=False)
        # Internal drafting notes are the team's, in any copy; they go in the findings report.
        if c != "INTERNAL_DRAFTING"
        and (not submission or c in _CUSTOMER_FACING)
        # Whatever the stored register holds, a claim about what the bidder holds or what
        # third parties agreed to is a company fact and never printed as an assumption.
        and not HOLDINGS_CLAIM.search(t or "")
    ]
    kept.sort(key=lambda row: order.index(row[0]) if row[0] in order else 9)
    # numbered last, so the ids run 01, 02, 03 ... in the order the reader sees them
    numbered = [(f"AS-{i:02d}", c, t) for i, (c, t) in enumerate(kept, start=1)]
    if not numbered:
        return None
    rows = ["| ID | Type | Assumption |"]
    rows += [f"| {i} | {_CATEGORY_NAMES.get(c, c)} | {t} |" for i, c, t in numbered]
    return "\n".join(rows)
