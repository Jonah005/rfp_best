"""The diagrams drawn during the task work, placed in the report.

Each task already has diagrams (flows, architectures) that people and the system
drew and checked on the task screen. The report shows those same diagrams: each
is drawn as a picture with the layout used on screen and set after the paragraph
of its chapter that talks about the same things. Nothing is invented here: a
diagram a person left out of the response, or one that was archived, is skipped.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import ExecutionContext
from app.models.diagrams import Diagram
from app.services import diagram_spec as ds
from app.services import diagrams as dg
from app.services.diagram_png import render_png
from app.services.storage import ObjectStorage

MARKER = re.compile(r"^\[\[FIGURE:([^|\]]+)\|([^|]*)(?:\|(.*))?\]\]$", re.S)
_WORD = re.compile(r"[a-z][a-z0-9]{3,}")
# Diagrams about the bidder's own bidding steps say nothing about the solution.
_BIDDING_PROCESS = re.compile(
    r"\b(bidder|bidders|signing|signature|submission|nda|tender|eligibility)\b", re.I
)
_COMMON = {
    "that",
    "this",
    "with",
    "from",
    "will",
    "into",
    "have",
    "each",
    "their",
    "which",
    "such",
    "also",
    "than",
    "then",
    "step",
    "flow",
}


def marker(png_key: str, caption: str, alt: str = "") -> str:
    clean = " ".join(alt.replace("|", "/").replace("]", ")").split())
    return f"[[FIGURE:{png_key}|{caption.replace('|', '/')}" + (f"|{clean}]]" if clean else "]]")


def describe(title: str, kind: str, spec: ds.Spec) -> str:
    """Alt text from the diagram's own content: its lanes and its boxes, in order."""
    labels = [n.label for n in spec.nodes]
    shown = "; ".join(labels[:12]) + (f"; and {len(labels) - 12} more" if len(labels) > 12 else "")
    lanes = list(dict.fromkeys(n.lane for n in spec.nodes if n.lane))
    kind_name = {"FLOW": "Process flow", "ARCHITECTURE": "Architecture diagram"}.get(
        kind, "Diagram"
    )
    where = f" Lanes: {', '.join(lanes)}." if len(lanes) > 1 else ""
    return f"{kind_name} titled {title}, with {len(labels)} boxes.{where} Boxes: {shown}."


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower())) - _COMMON


def _png_key(
    ctx: ExecutionContext, bid_id: uuid.UUID, diagram_id, version_no: int, marks: bool
) -> str:
    return ObjectStorage.tenant_key(
        ctx.org_id,
        "report-figures-r6",
        str(bid_id),
        f"{diagram_id}-v{version_no}-{'marked' if marks else 'clean'}.png",
    )


def _upright(spec: ds.Spec, style: ds.Style) -> ds.Spec:
    """A long left-to-right chain is unreadable on a page; stand it up when that is squarer."""
    laid = ds.layout(spec, style)
    if spec.direction != "LR" or laid.width <= laid.height * 2:
        return spec
    flipped = spec.model_copy(update={"direction": "TB"})
    tall = ds.layout(flipped, style)
    wide_ratio = laid.width / laid.height
    tall_ratio = max(tall.width / tall.height, tall.height / tall.width)
    return flipped if tall_ratio < wide_ratio else spec


def _plain(spec: ds.Spec, style: ds.Style) -> tuple[ds.Spec, ds.Style]:
    """The customer's copy: no review marks for unconfirmed parts."""
    nodes = [n.model_copy(update={"basis": "TENDER"}) for n in spec.nodes]
    edges = [e.model_copy(update={"basis": "TENDER"}) for e in spec.edges]
    return (
        spec.model_copy(update={"nodes": nodes, "edges": edges}),
        style.model_copy(update={"show_basis_marks": False}),
    )


def _place(paragraphs: list[dict], subject: set[str]) -> int:
    """The 1-based paragraph after which a diagram sits: the one sharing most of its words."""
    best, best_score = 1, 0.0
    for index, para in enumerate(paragraphs, start=1):
        if para["kind"] != "BODY" or not subject:
            continue
        score = len(subject & _words(para["text"])) / len(subject)
        if score > best_score:
            best, best_score = index, score
    return best


def chapter_figures(
    session: Session,
    ctx: ExecutionContext,
    *,
    bid_id: uuid.UUID,
    artifacts: list,
    paragraphs: list[dict],
    storage: ObjectStorage,
    marks: bool = True,
) -> list[dict]:
    """[{after, png_key, caption}] for the chapter's diagrams, drawing any picture not yet made."""
    task_ids = {a.task_id for a in artifacts}
    if not task_ids:
        return []
    rows = list(
        session.execute(
            select(Diagram)
            .where(
                Diagram.org_id == ctx.org_id,
                Diagram.bid_id == bid_id,
                Diagram.task_id.in_(task_ids),
                Diagram.archived.is_(False),
                Diagram.left_out.is_(False),
            )
            .order_by(Diagram.created_at)
        ).scalars()
    )
    # Two drawings with the same title are variants of one picture (an overview and a
    # swimlane view). The fuller one is shown.
    parsed: dict[uuid.UUID, tuple] = {}
    for diagram in rows:
        if diagram.diagram_type == "SEQUENCE" or diagram.current_version_id is None:
            continue  # a sequence needs its own drawing; it is not forced into boxes
        version = dg.current_version(session, ctx, diagram)
        try:
            spec = ds.parse_spec(version.spec)
            style = ds.parse_style(version.style)
        except ds.SpecError:
            continue
        if ds.readability(spec)["issues"]:
            continue  # too crowded to read at page size, by the diagram screen's own rule
        parsed[diagram.id] = (version, spec, style)
    fullest: dict[str, uuid.UUID] = {}
    for diagram in rows:
        if diagram.id not in parsed:
            continue
        held = fullest.get(diagram.title)
        if held is None or len(parsed[diagram.id][1].nodes) > len(parsed[held][1].nodes):
            fullest[diagram.title] = diagram.id
    out: list[dict] = []
    for diagram in rows:
        if diagram.id not in parsed or fullest.get(diagram.title) != diagram.id:
            continue
        if _BIDDING_PROCESS.search(diagram.title):
            continue
        version, spec, style = parsed[diagram.id]
        key = _png_key(ctx, bid_id, diagram.id, version.version_no, marks)
        if not storage.exists(key):
            drawn_spec, drawn_style = (spec, style) if marks else _plain(spec, style)
            drawn_spec = _upright(drawn_spec, drawn_style)
            storage.put(key, render_png(drawn_spec, drawn_style), content_type="image/png")
        subject = _words(diagram.title + " " + " ".join(n.label for n in spec.nodes))
        after = _place(paragraphs, subject)
        if after < len(paragraphs) and paragraphs[after - 1]["kind"] != "BODY":
            after += 1
        out.append(
            {
                "after": after,
                "png_key": key,
                "caption": diagram.title,
                "alt": describe(diagram.title, diagram.diagram_type, spec),
                "task": str(diagram.task_id),
                "kind": diagram.diagram_type,
                "appendix": False,
            }
        )
    # One picture per topic (a task) stays in the chapter: the architecture when there is
    # one, else the flow. The others go to the appendix of process flows.
    chosen: dict[str, dict] = {}
    for figure in out:
        kept = chosen.get(figure["task"])
        if kept is None or (figure["kind"] == "ARCHITECTURE" and kept["kind"] != "ARCHITECTURE"):
            chosen[figure["task"]] = figure
    for figure in out:
        figure["appendix"] = chosen[figure["task"]] is not figure
    return out
