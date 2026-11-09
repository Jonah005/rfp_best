"""The structure of a diagram, the rules it must meet, and how it is drawn.

A model proposes a diagram as data -- boxes, links, lanes. This module is what
turns that proposal into something the platform will keep: it checks the shape,
lays it out, and draws it. Nothing a model wrote is ever drawn without passing
through here, and nothing here calls a model.

Why the drawing is deterministic:

* "Change this" and "make it look like that" become edits to a structure and a
  handful of style values, which can be checked, compared and undone.
* The same structure always draws the same picture, so two people looking at
  version 3 see the same thing, and a diff between versions is a real diff.
* A model asked to draw pixels cannot be told "you may only use these colours".
  Here a style is a small set of validated values.

What it will not do: it is an automatic layout, not a designer. Straight-forward
flows, architectures and context diagrams come out clean; a dense graph with
many cross links will be legible but not beautiful, and the review says so
rather than pretending.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

MAX_NODES = 40
MAX_EDGES = 80

NodeKind = Literal["process", "decision", "start_end", "system", "actor", "store", "external"]
# Where a box or a link comes from. Anything other than the tender, approved
# evidence or a named person is drawn so that it can be seen to be a proposal or
# an assumption. PERSON is a box the person working on the diagram asked for:
# they answer for it, as they do for a sentence they write.
Basis = Literal["TENDER", "EVIDENCE", "ASSUMPTION", "PROPOSED", "PERSON"]

_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class SpecError(ValueError):
    """The proposed structure cannot be drawn. The message says why."""


class Node(BaseModel):
    id: str
    label: str = Field(min_length=1, max_length=120)
    kind: NodeKind = "process"
    lane: str | None = Field(default=None, max_length=60)
    basis: Basis = "TENDER"
    # Which listed sources support this box (T1 = tender wording, E2 = evidence).
    sources: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError("node ids use letters, digits, - and _ only (max 40)")
        return value

    @field_validator("label", "lane")
    @classmethod
    def _tidy(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if isinstance(value, str) else value


class Edge(BaseModel):
    source: str
    target: str
    label: str = Field(default="", max_length=80)
    dashed: bool = False
    basis: Basis = "TENDER"
    sources: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("label")
    @classmethod
    def _tidy(cls, value: str) -> str:
        return " ".join(value.split())


class Spec(BaseModel):
    direction: Literal["LR", "TB"] = "LR"
    nodes: list[Node] = Field(min_length=1, max_length=MAX_NODES)
    edges: list[Edge] = Field(default_factory=list, max_length=MAX_EDGES)

    @model_validator(mode="after")
    def _consistent(self) -> Spec:
        ids = [n.id for n in self.nodes]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"node ids must be unique; repeated: {', '.join(dupes)}")
        known = set(ids)
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise ValueError(
                    f"a link joins '{edge.source}' and '{edge.target}', "
                    "and at least one of them is not a box in this diagram"
                )
            if edge.source == edge.target:
                raise ValueError(f"'{edge.source}' links to itself")
        return self


def parse_spec(raw: object) -> Spec:
    """Validate a proposed structure, with a message a person can act on."""
    try:
        return Spec.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise SpecError(f"{first.get('msg', 'invalid')} ({where})") from exc


# --- style -------------------------------------------------------------------

THEMES: dict[str, dict[str, str]] = {
    "CLEAN": {
        "background": "#ffffff",
        "node": "#eef3f8",
        "stroke": "#2b4a6b",
        "text": "#14202b",
        "lane": "#f6f8fa",
        "lane_alt": "#eef1f4",
        "accent": "#1f6f8b",
        "edge": "#44586b",
    },
    "BLUEPRINT": {
        "background": "#0e2a47",
        "node": "#16406b",
        "stroke": "#9cc7f0",
        "text": "#eaf4ff",
        "lane": "#12345a",
        "lane_alt": "#0f2e50",
        "accent": "#ffd166",
        "edge": "#9cc7f0",
    },
    "MONO": {
        "background": "#ffffff",
        "node": "#ffffff",
        "stroke": "#111111",
        "text": "#111111",
        "lane": "#f4f4f4",
        "lane_alt": "#ebebeb",
        "accent": "#111111",
        "edge": "#111111",
    },
    "WARM": {
        "background": "#fffaf3",
        "node": "#fdebd3",
        "stroke": "#8a4b14",
        "text": "#2d1a08",
        "lane": "#fbf1e2",
        "lane_alt": "#f6e6cf",
        "accent": "#b5541c",
        "edge": "#6b4a2b",
    },
}
FONTS = {
    "SANS": "Helvetica, Arial, sans-serif",
    "SERIF": "Georgia, 'Times New Roman', serif",
    "MONO": "Consolas, 'Courier New', monospace",
}


class Style(BaseModel):
    """A small, closed set of look-and-feel values. Anything else is refused.

    A free-text request such as "like a blueprint" or "thin lines, rounded, navy
    boxes" is turned into these values by a model; the values are what is kept
    and what is checked.
    """

    theme: Literal["CLEAN", "BLUEPRINT", "MONO", "WARM"] = "CLEAN"
    font: Literal["SANS", "SERIF", "MONO"] = "SANS"
    corner: int = Field(default=8, ge=0, le=20)
    line_width: float = Field(default=1.6, ge=0.75, le=3.5)
    density: Literal["COMPACT", "COMFORTABLE"] = "COMFORTABLE"
    node_fill: str | None = None
    stroke: str | None = None
    accent: str | None = None
    background: str | None = None
    show_basis_marks: bool = True

    @field_validator("node_fill", "stroke", "accent", "background")
    @classmethod
    def _colour(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _HEX.match(value):
            raise ValueError("colours are written like #1a2b3c")
        return value.lower()


def parse_style(raw: object) -> Style:
    try:
        return Style.model_validate(raw or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        raise SpecError(f"{first.get('msg', 'invalid style')} ({first.get('loc', ())})") from exc


def _luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def readable_on(background: str, light: str = "#ffffff", dark: str = "#111111") -> str:
    """The text colour that stays readable on a chosen fill."""
    return dark if _luminance(background) > 0.35 else light


# --- comparing versions -------------------------------------------------------


def describe_changes(old: Spec | None, new: Spec) -> list[str]:
    """What actually differs, worked out from the structures.

    The platform states the change itself instead of repeating the model's own
    account of what it did, because a model's summary of its edit is a claim,
    and this is a fact.
    """
    if old is None:
        return [f"New diagram with {len(new.nodes)} boxes and {len(new.edges)} links."]
    lines: list[str] = []
    old_nodes = {n.id: n for n in old.nodes}
    new_nodes = {n.id: n for n in new.nodes}
    for nid in new_nodes.keys() - old_nodes.keys():
        lines.append(f"Added box: {new_nodes[nid].label}")
    for nid in old_nodes.keys() - new_nodes.keys():
        lines.append(f"Removed box: {old_nodes[nid].label}")
    for nid in new_nodes.keys() & old_nodes.keys():
        a, b = old_nodes[nid], new_nodes[nid]
        if a.label != b.label:
            lines.append(f"Renamed: {a.label} -> {b.label}")
        if a.lane != b.lane:
            lines.append(f"Moved {b.label} from lane '{a.lane or '-'}' to '{b.lane or '-'}'")
        if a.kind != b.kind:
            lines.append(f"Changed {b.label} from {a.kind} to {b.kind}")

    def name(spec_nodes: dict[str, Node], nid: str) -> str:
        return spec_nodes[nid].label if nid in spec_nodes else nid

    old_edges = {(e.source, e.target): e for e in old.edges}
    new_edges = {(e.source, e.target): e for e in new.edges}
    for key in new_edges.keys() - old_edges.keys():
        lines.append(f"Added link: {name(new_nodes, key[0])} -> {name(new_nodes, key[1])}")
    for key in old_edges.keys() - new_edges.keys():
        lines.append(f"Removed link: {name(old_nodes, key[0])} -> {name(old_nodes, key[1])}")
    for key in new_edges.keys() & old_edges.keys():
        if old_edges[key].label != new_edges[key].label:
            lines.append(
                f"Relabelled link {name(new_nodes, key[0])} -> {name(new_nodes, key[1])}: "
                f"'{old_edges[key].label}' -> '{new_edges[key].label}'"
            )
    if old.direction != new.direction:
        lines.append(f"Direction changed from {old.direction} to {new.direction}")
    return lines or ["No change to the boxes or links."]


def unconfirmed_elements(spec: Spec) -> list[dict]:
    """Boxes and links that rest on an assumption or a promise, not on the tender or evidence."""
    out: list[dict] = []
    labels = {n.id: n.label for n in spec.nodes}
    for node in spec.nodes:
        if node.basis in ("ASSUMPTION", "PROPOSED"):
            out.append({"kind": "box", "label": node.label, "basis": node.basis})
    for edge in spec.edges:
        if edge.basis in ("ASSUMPTION", "PROPOSED"):
            out.append(
                {
                    "kind": "link",
                    "label": f"{labels[edge.source]} -> {labels[edge.target]}",
                    "basis": edge.basis,
                }
            )
    return out


# --- keeping it readable -------------------------------------------------------

# Past these a picture stops being a picture and becomes a wiring diagram. The numbers
# come from looking at what the automatic layout produces, not from taste.
COMFORTABLE_NODES = 14
MAX_READABLE_NODES = 18
MAX_READABLE_EDGES = 26
MAX_LONG_LINKS = 4
MAX_FANOUT = 4
MAX_EDGE_LABEL = 48


def readability(spec: Spec) -> dict:
    """How crowded a structure would look, before anything is drawn."""
    layer, back = _layers(spec)
    long_links = sum(
        1
        for e in spec.edges
        if (e.source, e.target) in back or layer[e.target] - layer[e.source] > 1
    )
    fan: dict[str, int] = defaultdict(int)
    for e in spec.edges:
        fan[e.source] += 1
    widest = max(fan.values(), default=0)
    issues: list[str] = []
    if len(spec.nodes) > MAX_READABLE_NODES:
        issues.append(
            f"it has {len(spec.nodes)} boxes; more than {MAX_READABLE_NODES} cannot be read "
            "at page size"
        )
    if len(spec.edges) > MAX_READABLE_EDGES:
        issues.append(f"it has {len(spec.edges)} links; keep to about {MAX_READABLE_EDGES}")
    if long_links > MAX_LONG_LINKS:
        issues.append(
            f"{long_links} links jump over other columns, which draws lines across the picture; "
            "reorder or merge so most links go to the next step"
        )
    if widest > MAX_FANOUT:
        issues.append(f"one box has {widest} outgoing links; group them behind one box")
    return {
        "nodes": len(spec.nodes),
        "edges": len(spec.edges),
        "long_links": long_links,
        "issues": issues,
    }


def tidy(spec: Spec) -> tuple[Spec, list[str]]:
    """Fix what is wrong with a structure without changing what it says."""
    notes: list[str] = []
    out: dict[str, int] = defaultdict(int)
    for e in spec.edges:
        out[e.source] += 1
    for n in spec.nodes:
        # A diamond asks a question and branches. One that does not is a step drawn
        # in the wrong shape, and it is the shape people notice.
        if n.kind == "decision" and out[n.id] < 2:
            n.kind = "process"
            notes.append(f"'{n.label}' does not branch, so it is drawn as a step, not a question.")
    for e in spec.edges:
        if e.label and len(e.label) > MAX_EDGE_LABEL:
            e.label = e.label[: MAX_EDGE_LABEL - 1].rstrip() + "…"
    return spec, notes


# --- layout -------------------------------------------------------------------

_CHAR_W = {"SANS": 7.0, "SERIF": 7.3, "MONO": 8.4}
_LINE_H = 16
_GAP_MAIN = 78
_GAP_CROSS = 26
_PAD = 28


def _wrap(text: str, width: int = 22) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if len(candidate) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines or [""]


class _Box:
    __slots__ = ("node", "lines", "w", "h", "x", "y", "layer", "lane")

    def __init__(self, node: Node, compact: bool, char_w: float):
        self.node = node
        self.lines = _wrap(node.label, 18 if compact else 22)
        widest = max(len(line) for line in self.lines)
        w = max(84.0, widest * char_w + 26)
        h = max(42.0, len(self.lines) * _LINE_H + 22)
        if node.kind == "decision":
            w, h = w + 44, h + 26
        self.w, self.h = w, h
        self.x = self.y = 0.0
        self.layer = 0
        self.lane = node.lane or ""


def _layers(spec: Spec) -> tuple[dict[str, int], set[tuple[str, str]]]:
    """Longest-path layering. Links that would close a cycle are set aside."""
    out: dict[str, list[str]] = defaultdict(list)
    for e in spec.edges:
        out[e.source].append(e.target)
    back: set[tuple[str, str]] = set()
    state: dict[str, int] = {}

    def visit(start: str) -> None:
        stack = [(start, iter(out[start]))]
        state[start] = 1
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if state.get(nxt) == 1:
                    back.add((node, nxt))
                elif nxt not in state:
                    state[nxt] = 1
                    stack.append((nxt, iter(out[nxt])))
                    advanced = True
                    break
            if not advanced:
                state[node] = 2
                stack.pop()

    for n in spec.nodes:
        if n.id not in state:
            visit(n.id)

    layer = {n.id: 0 for n in spec.nodes}
    changed = True
    guard = 0
    forward = [e for e in spec.edges if (e.source, e.target) not in back]
    while changed and guard < len(spec.nodes) + 2:
        changed = False
        guard += 1
        for e in forward:
            if layer[e.target] < layer[e.source] + 1:
                layer[e.target] = layer[e.source] + 1
                changed = True
    # A box nothing leads into (an actor, an entry point) sits just before the first
    # thing it feeds, not at the far left, so its links are short.
    has_in = {e.target for e in forward}
    out_targets: dict[str, list[str]] = defaultdict(list)
    for e in forward:
        out_targets[e.source].append(e.target)
    for n in spec.nodes:
        if n.id not in has_in and out_targets[n.id]:
            layer[n.id] = min(layer[t] for t in out_targets[n.id]) - 1
    return layer, back


class Laid:
    """A diagram with every position decided."""

    def __init__(self) -> None:
        self.boxes: dict[str, _Box] = {}
        self.lanes: list[tuple[str, float, float]] = []  # name, cross start, cross size
        self.width = 0.0
        self.height = 0.0
        self.paths: list[tuple[Edge, list[tuple[float, float]], tuple[float, float]]] = []


def layout(spec: Spec, style: Style | None = None) -> Laid:
    style = style or Style()
    compact = style.density == "COMPACT"
    tb = spec.direction == "TB"
    char_w = _CHAR_W[style.font]
    boxes = {n.id: _Box(n, compact, char_w) for n in spec.nodes}
    layer, back = _layers(spec)
    for nid, box in boxes.items():
        box.layer = layer[nid]

    lane_names: list[str] = []
    for n in spec.nodes:
        name = n.lane or ""
        if name not in lane_names:
            lane_names.append(name)
    has_lanes = any(lane_names)
    if has_lanes and "" in lane_names:
        lane_names.remove("")
        lane_names.append("")  # unassigned boxes sit in a final band

    def main(b: _Box) -> float:
        return b.h if tb else b.w

    def cross(b: _Box) -> float:
        return b.w if tb else b.h

    n_layers = max(layer.values()) + 1
    cells: dict[tuple[int, str], list[_Box]] = defaultdict(list)
    for n in spec.nodes:
        cells[(layer[n.id], boxes[n.id].lane)].append(boxes[n.id])

    # Order boxes inside each cell by where their predecessors sit, three sweeps.
    preds: dict[str, list[str]] = defaultdict(list)
    for e in spec.edges:
        if (e.source, e.target) not in back:
            preds[e.target].append(e.source)
    order_pos = {n.id: i for i, n in enumerate(spec.nodes)}
    rank: dict[str, float] = {n.id: float(order_pos[n.id]) for n in spec.nodes}
    for _ in range(3):
        for key, group in cells.items():
            group.sort(
                key=lambda b: (
                    sum(rank[p] for p in preds[b.node.id]) / len(preds[b.node.id])
                    if preds[b.node.id]
                    else order_pos[b.node.id],
                    order_pos[b.node.id],
                )
            )
            for i, b in enumerate(group):
                rank[b.node.id] = i + key[0] * 0.001 + order_pos[b.node.id] * 0.0001

    # Main axis: one column per layer, as wide as its widest box.
    col_size = [0.0] * n_layers
    for b in boxes.values():
        col_size[b.layer] = max(col_size[b.layer], main(b))
    lane_gutter = 0.0 if not has_lanes else (34.0 if tb else 130.0)
    # A link label sits in the gap between two columns, so the gap is as wide as the
    # widest label that has to fit in it. Otherwise labels land on top of boxes.
    label_w = 6.4 if style.font != "MONO" else 7.4
    gaps = [float(_GAP_MAIN)] * max(n_layers - 1, 0)
    if not tb:
        for e in spec.edges:
            if (e.source, e.target) in back or not e.label:
                continue
            if layer[e.target] - layer[e.source] == 1:
                gi = layer[e.source]
                gaps[gi] = max(gaps[gi], len(e.label) * label_w + 44)
    col_start: list[float] = []
    cursor = _PAD + (lane_gutter if not tb else 0.0)
    for i, size in enumerate(col_size):
        col_start.append(cursor)
        cursor += size + (gaps[i] if i < len(gaps) else 0.0)
    main_end = cursor + _PAD

    # Cross axis: one band per lane, tall enough for its fullest cell.
    laid = Laid()
    cross_cursor = _PAD + (lane_gutter if tb else 0.0)
    band: dict[str, tuple[float, float]] = {}
    for name in lane_names:
        need = 0.0
        for li in range(n_layers):
            group = cells.get((li, name), [])
            if group:
                need = max(need, sum(cross(b) for b in group) + _GAP_CROSS * (len(group) - 1))
        size = max(need + 2 * _GAP_CROSS, 70.0)
        band[name] = (cross_cursor, size)
        cross_cursor += size + (10 if has_lanes else 0)
    # Links that travel in a channel outside the boxes need room to be seen and
    # labelled; without it their labels run off the edge of the picture.
    travellers = sum(
        1
        for e in spec.edges
        if (e.source, e.target) in back or layer[e.target] - layer[e.source] > 1
    )
    cross_end = cross_cursor + _PAD - (10 if has_lanes else 0) + (36 if travellers else 0)

    for (li, name), group in cells.items():
        start, size = band[name]
        total = sum(cross(b) for b in group) + _GAP_CROSS * (len(group) - 1)
        c = start + (size - total) / 2
        for b in group:
            m = col_start[li] + (col_size[li] - main(b)) / 2
            if tb:
                b.x, b.y = c, m
            else:
                b.x, b.y = m, c
            c += cross(b) + _GAP_CROSS

    laid.boxes = boxes
    if has_lanes:
        laid.lanes = [(name, *band[name]) for name in lane_names]

    # Links. Short ones are elbows; long ones and cycle-closers travel in a channel
    # outside the boxes so they do not cut through anything.
    top_channel = _PAD * 0.45
    bottom_channel = cross_end - _PAD * 0.45
    long_i = back_i = 0
    # Elbow links that share a gap would run on top of each other at the same
    # midpoint; each takes its own lane inside the gap.
    elbows: dict[int, list[Edge]] = defaultdict(list)
    for e in spec.edges:
        if (e.source, e.target) not in back and layer[e.target] - layer[e.source] == 1:
            a_, b_ = boxes[e.source], boxes[e.target]
            if abs((a_.y + a_.h / 2) - (b_.y + b_.h / 2)) >= 1 or tb:
                elbows[layer[e.source]].append(e)
    for e in spec.edges:
        a, b = boxes[e.source], boxes[e.target]
        is_back = (e.source, e.target) in back
        span = b.layer - a.layer
        if not tb:
            p1 = (a.x + a.w, a.y + a.h / 2)
            p2 = (b.x, b.y + b.h / 2)
        else:
            p1 = (a.x + a.w / 2, a.y + a.h)
            p2 = (b.x + b.w / 2, b.y)

        def at(m: float, c: float) -> tuple[float, float]:
            return (c, m) if tb else (m, c)

        def m_of(p: tuple[float, float]) -> float:
            return p[1] if tb else p[0]

        def c_of(p: tuple[float, float]) -> float:
            return p[0] if tb else p[1]

        if is_back:
            back_i += 1
            if not tb:
                # Leave and enter on whichever side has fewer boxes in the way in that
                # column, so the line does not run through a box stacked above or below.
                def blocked(box: _Box, above: bool) -> int:
                    return sum(
                        1
                        for o in boxes.values()
                        if o is not box
                        and o.layer == box.layer
                        and ((o.y < box.y) if above else (o.y > box.y))
                    )

                via_top = blocked(a, True) + blocked(b, True) < blocked(a, False) + blocked(
                    b, False
                )
                if via_top:
                    long_i += 1
                    chan = top_channel + long_i * 7
                    q1 = (a.x + a.w / 2, a.y)
                    q2 = (b.x + b.w / 2, b.y)
                else:
                    chan = bottom_channel - back_i * 7
                    q1 = (a.x + a.w / 2, a.y + a.h)
                    q2 = (b.x + b.w / 2, b.y + b.h)
                pts = [q1, (q1[0], chan), (q2[0], chan), q2]
                label_at = ((q1[0] + q2[0]) / 2, chan)
            else:
                chan = bottom_channel - back_i * 7
                q1 = (a.x + a.w, a.y + a.h / 2)
                q2 = (b.x + b.w, b.y + b.h / 2)
                pts = [q1, (chan, q1[1]), (chan, q2[1]), q2]
                label_at = (chan, (q1[1] + q2[1]) / 2)
        elif span > 1:
            long_i += 1
            chan = top_channel + long_i * 7
            gap = 26.0
            m_out = m_of(p1) + gap
            m_in = m_of(p2) - gap
            pts = [p1, at(m_out, c_of(p1)), at(m_out, chan), at(m_in, chan), at(m_in, c_of(p2)), p2]
            label_at = at((m_out + m_in) / 2, chan)
        else:
            mid = (m_of(p1) + m_of(p2)) / 2
            siblings = elbows.get(a.layer, [])
            if e in siblings and len(siblings) > 1 and not tb:
                room = max(min((m_of(p2) - m_of(p1)) / 2 - 14, 60.0), 0.0)
                step = (2 * room) / (len(siblings) - 1) if room else 0.0
                mid = mid - room + siblings.index(e) * step
            if abs(c_of(p1) - c_of(p2)) < 1:
                pts = [p1, p2]
            else:
                pts = [p1, at(mid, c_of(p1)), at(mid, c_of(p2)), p2]
            label_at = at(mid, (c_of(p1) + c_of(p2)) / 2)
        laid.paths.append((e, pts, label_at))

    laid.width, laid.height = (cross_end, main_end) if tb else (main_end, cross_end)
    return laid


# --- drawing ------------------------------------------------------------------


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _palette(style: Style) -> dict[str, str]:
    p = dict(THEMES[style.theme])
    if style.background:
        p["background"] = style.background
    if style.node_fill:
        p["node"] = style.node_fill
    if style.stroke:
        p["stroke"] = style.stroke
        p["edge"] = style.stroke
    if style.accent:
        p["accent"] = style.accent
    p["node_text"] = readable_on(p["node"], light="#ffffff", dark=p["text"])
    p["bg_text"] = readable_on(p["background"], light="#f4f8fc", dark=p["text"])
    return p


def _shape(b: _Box, p: dict[str, str], style: Style) -> str:
    fill, stroke = p["node"], p["stroke"]
    sw = style.line_width
    dash = ""
    if b.node.basis == "ASSUMPTION":
        dash = ' stroke-dasharray="6 4"'
    elif b.node.basis == "PROPOSED":
        dash = ' stroke-dasharray="2 3"'
    if b.node.kind == "external":
        dash = dash or ' stroke-dasharray="7 4"'
    x, y, w, h = b.x, b.y, b.w, b.h
    common = f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{dash}'
    if b.node.kind == "decision":
        pts = f"{x + w / 2},{y} {x + w},{y + h / 2} {x + w / 2},{y + h} {x},{y + h / 2}"
        return f'<polygon points="{pts}" {common}/>'
    if b.node.kind == "start_end":
        r = h / 2
        return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" {common}/>'
    if b.node.kind == "store":
        e = 7
        return (
            f'<path d="M{x},{y + e} a{w / 2},{e} 0 0 1 {w},0 v{h - 2 * e} '
            f'a{w / 2},{e} 0 0 1 -{w},0 z" {common}/>'
            f'<path d="M{x},{y + e} a{w / 2},{e} 0 0 0 {w},0" fill="none" '
            f'stroke="{stroke}" stroke-width="{sw}"/>'
        )
    radius = 0 if b.node.kind == "system" else style.corner
    extra = ""
    if b.node.kind == "actor":
        extra = (
            f'<rect x="{x + 3}" y="{y + 3}" width="{w - 6}" height="{h - 6}" '
            f'rx="{max(radius - 2, 0)}" fill="none" stroke="{stroke}" stroke-width="{sw * 0.6}"/>'
        )
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" {common}/>{extra}'


def _text(b: _Box, p: dict[str, str]) -> str:
    cx, cy = b.x + b.w / 2, b.y + b.h / 2
    first = cy - (len(b.lines) - 1) * _LINE_H / 2
    tspans = "".join(
        f'<tspan x="{cx:.1f}" y="{first + i * _LINE_H:.1f}">{_esc(line)}</tspan>'
        for i, line in enumerate(b.lines)
    )
    weight = "600" if b.node.kind in ("actor", "system", "start_end") else "400"
    return (
        f'<text text-anchor="middle" dominant-baseline="central" font-size="13" '
        f'font-weight="{weight}" fill="{p["node_text"]}">{tspans}</text>'
    )


def render_svg(spec: Spec, style: Style | None = None, *, title: str = "Diagram") -> str:
    """Draw a structure. The result is a complete, self-contained SVG document."""
    style = style or Style()
    p = _palette(style)
    font = FONTS[style.font]
    laid = layout(spec, style)
    tb = spec.direction == "TB"
    legend_h = 0
    marks = {n.basis for n in spec.nodes} | {e.basis for e in spec.edges}
    show_legend = style.show_basis_marks and bool(marks & {"ASSUMPTION", "PROPOSED"})
    if show_legend:
        legend_h = 34
    width, height = laid.width, laid.height + legend_h

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" font-family="{_esc(font)}" role="img" '
        f'aria-label="{_esc(title)}">',
        f"<title>{_esc(title)}</title>",
        f'<rect width="100%" height="100%" fill="{p["background"]}"/>',
        "<defs>"
        f'<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" '
        f'markerHeight="8" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{p["edge"]}"/></marker></defs>',
    ]

    for i, (name, start, size) in enumerate(laid.lanes):
        fill = p["lane"] if i % 2 == 0 else p["lane_alt"]
        if tb:
            parts.append(
                f'<rect x="{start:.1f}" y="0" width="{size:.1f}" height="{laid.height:.1f}" '
                f'fill="{fill}"/>'
            )
            if name:
                parts.append(
                    f'<text x="{start + size / 2:.1f}" y="20" text-anchor="middle" font-size="12" '
                    f'font-weight="700" fill="{p["bg_text"]}">{_esc(name)}</text>'
                )
        else:
            parts.append(
                f'<rect x="0" y="{start:.1f}" width="{laid.width:.1f}" height="{size:.1f}" '
                f'fill="{fill}"/>'
            )
            if name:
                label_lines = _wrap(name, 14)
                base = start + size / 2 - (len(label_lines) - 1) * 7
                spans = "".join(
                    f'<tspan x="14" y="{base + i * 14:.1f}">{_esc(t)}</tspan>'
                    for i, t in enumerate(label_lines)
                )
                parts.append(
                    f'<text font-size="12" font-weight="700" dominant-baseline="central" '
                    f'fill="{p["bg_text"]}">{spans}</text>'
                )

    for edge, pts, _label_at in laid.paths:
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        dash = ' stroke-dasharray="6 4"' if edge.dashed else ""
        if edge.basis == "ASSUMPTION":
            dash = ' stroke-dasharray="6 4"'
        elif edge.basis == "PROPOSED":
            dash = ' stroke-dasharray="2 3"'
        parts.append(
            f'<path d="{d}" fill="none" stroke="{p["edge"]}" stroke-width="{style.line_width}"'
            f'{dash} marker-end="url(#arrow)" stroke-linejoin="round"/>'
        )

    for b in laid.boxes.values():
        parts.append(_shape(b, p, style))
        parts.append(_text(b, p))

    for edge, _pts, (lx, ly) in laid.paths:
        if not edge.label:
            continue
        text = edge.label
        w = len(text) * (6.4 if style.font != "MONO" else 7.4) + 10
        parts.append(
            f'<rect x="{lx - w / 2:.1f}" y="{ly - 9:.1f}" width="{w:.1f}" height="18" rx="3" '
            f'fill="{p["background"]}" fill-opacity="0.92"/>'
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" dominant-baseline="central" '
            f'font-size="11" fill="{p["bg_text"]}">{_esc(text)}</text>'
        )

    if show_legend:
        y = laid.height + 8
        second = (
            34
            + int(len("assumption (needs confirming)") * (6.2 if style.font != "MONO" else 7.2))
            + 30
        )
        parts.append(
            f'<g font-size="11" fill="{p["bg_text"]}">'
            f'<rect x="{_PAD}" y="{y}" width="26" height="14" fill="none" stroke="{p["stroke"]}" '
            f'stroke-dasharray="6 4" stroke-width="{style.line_width}"/>'
            f'<text x="{_PAD + 34}" y="{y + 7}" dominant-baseline="central">'
            "assumption (needs confirming)</text>"
            f'<rect x="{_PAD + second}" y="{y}" width="26" height="14" fill="none" '
            f'stroke="{p["stroke"]}" stroke-dasharray="2 3" stroke-width="{style.line_width}"/>'
            f'<text x="{_PAD + second + 34}" y="{y + 7}" dominant-baseline="central">'
            "proposal (company has not committed)</text></g>"
        )
    parts.append("</svg>")
    return "".join(parts)


# --- sequence diagrams --------------------------------------------------------


def render_sequence_svg(spec: Spec, style: Style | None = None, *, title: str = "Sequence") -> str:
    """Participants across the top, messages down the page in the order given."""
    style = style or Style()
    p = _palette(style)
    font = FONTS[style.font]
    n = len(spec.nodes)
    col = 170
    left = _PAD + 20
    head_h = 40
    step = 48
    height = head_h + 40 + step * max(len(spec.edges), 1) + 30
    width = left * 2 + col * max(n - 1, 0) + 120
    xs = {node.id: left + 60 + i * col for i, node in enumerate(spec.nodes)}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="{_esc(font)}" role="img" '
        f'aria-label="{_esc(title)}"><title>{_esc(title)}</title>'
        f'<rect width="100%" height="100%" fill="{p["background"]}"/>'
        f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" '
        f'markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
        f'fill="{p["edge"]}"/></marker></defs>'
    ]
    for node in spec.nodes:
        x = xs[node.id]
        parts.append(
            f'<line x1="{x}" y1="{_PAD + head_h}" x2="{x}" y2="{height - 16}" '
            f'stroke="{p["edge"]}" stroke-width="1" stroke-dasharray="4 4"/>'
        )
        lines = _wrap(node.label, 16)
        w = max(96, max(len(t) for t in lines) * _CHAR_W[style.font] + 20)
        parts.append(
            f'<rect x="{x - w / 2:.1f}" y="{_PAD}" width="{w:.1f}" height="{head_h}" '
            f'rx="{style.corner}" fill="{p["node"]}" stroke="{p["stroke"]}" '
            f'stroke-width="{style.line_width}"/>'
            f'<text x="{x}" y="{_PAD + head_h / 2}" text-anchor="middle" '
            f'dominant-baseline="central" font-size="12" font-weight="600" '
            f'fill="{p["node_text"]}">{_esc(lines[0])}</text>'
        )
    y = _PAD + head_h + 40
    for edge in spec.edges:
        x1, x2 = xs[edge.source], xs[edge.target]
        dash = ' stroke-dasharray="6 4"' if edge.dashed else ""
        parts.append(
            f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{p["edge"]}" '
            f'stroke-width="{style.line_width}"{dash} marker-end="url(#arrow)"/>'
        )
        if edge.label:
            parts.append(
                f'<text x="{(x1 + x2) / 2:.1f}" y="{y - 7}" text-anchor="middle" font-size="11" '
                f'fill="{p["bg_text"]}">{_esc(edge.label)}</text>'
            )
        y += step
    parts.append("</svg>")
    return "".join(parts)


def render(diagram_type: str, spec: Spec, style: Style | None = None, *, title: str = "") -> str:
    """The one entry point: choose the drawing for the diagram's type."""
    if diagram_type == "SEQUENCE":
        return render_sequence_svg(spec, style, title=title or "Sequence diagram")
    return render_svg(spec, style, title=title or "Diagram")


# --- reading Mermaid ----------------------------------------------------------
#
# Engineers already keep flow and sequence diagrams as Mermaid text. Reading the
# common subset means such a diagram can be brought in exactly, with no model in
# the loop and nothing guessed, and then reviewed, restyled and versioned like
# any other. Anything outside the subset is refused with the line it choked on.

_M_NODE = re.compile(
    r"""^\s*(?P<id>[A-Za-z0-9_-]+)\s*
        (?:(?P<open>\(\(|\(\[|\[\(|\[|\(|\{)\s*(?P<label>.*?)\s*(?P<close>\)\)|\]\)|\)\]|\]|\)|\}))?
        \s*$""",
    re.X,
)
_M_KIND: dict[str, NodeKind] = {
    "[": "process",
    "(": "process",
    "((": "start_end",
    "([": "start_end",
    "[(": "store",
    "{": "decision",
}
_M_EDGE = re.compile(
    r"^(?P<a>.+?)\s*(?P<arrow>-\.->|-->|---|==>|-\.-)\s*(?:\|(?P<lab>[^|]*)\|\s*)?(?P<b>.+)$"
)


def _m_node(text: str, seen: dict[str, Node]) -> str:
    text = text.strip().rstrip(";")
    m = _M_NODE.match(text)
    if not m:
        raise SpecError(f"cannot read this box: {text!r}")
    nid = m["id"]
    if m["open"]:
        label = (m["label"] or nid).strip().strip('"')
        seen[nid] = Node(
            id=nid, label=label, kind=_M_KIND.get(m["open"], "process"), basis="PERSON"
        )
    elif nid not in seen:
        seen[nid] = Node(id=nid, label=nid, basis="PERSON")
    return nid


def parse_mermaid(source: str) -> tuple[str, Spec]:
    """Read a Mermaid flowchart or sequence diagram. Returns (diagram type, spec)."""
    lines = [
        ln.split("%%")[0].strip()
        for ln in source.replace("\r", "").split("\n")
        if ln.split("%%")[0].strip()
    ]
    if not lines:
        raise SpecError("the Mermaid text is empty")
    head = lines[0].lower()
    if head.startswith("sequencediagram"):
        return "SEQUENCE", _parse_mermaid_sequence(lines[1:])
    if not (head.startswith("graph") or head.startswith("flowchart")):
        raise SpecError("only 'graph'/'flowchart' and 'sequenceDiagram' Mermaid is understood")
    direction = "TB" if re.search(r"\b(TB|TD|BT)\b", lines[0], re.I) else "LR"
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    lane: str | None = None
    lane_stack: list[str] = []
    for ln in lines[1:]:
        low = ln.lower()
        if low.startswith("subgraph"):
            title = ln[8:].strip()
            m = re.match(r"^[A-Za-z0-9_-]+\s*\[(.*)\]$", title)
            lane = (m[1] if m else title).strip().strip('"') or None
            lane_stack.append(lane or "")
            continue
        if low == "end":
            if lane_stack:
                lane_stack.pop()
            lane = lane_stack[-1] if lane_stack else None
            continue
        if low.startswith(("style ", "classdef ", "class ", "linkstyle ", "click ", "direction ")):
            continue
        if not re.search(r"-\.->|-->|---|==>|-\.-", ln):
            nid = _m_node(ln, nodes)
            if lane and nodes[nid].lane is None:
                nodes[nid].lane = lane
            continue
        # a chain such as A --> B --> C is read pair by pair
        segments = re.split(r"\s*(-\.->|-->|---|==>|-\.-)\s*(?:\|([^|]*)\|\s*)?", ln)
        ids = [_m_node(segments[0], nodes)]
        arrows: list[tuple[str, str]] = []
        for i in range(1, len(segments) - 1, 3):
            arrows.append((segments[i], (segments[i + 1] or "").strip()))
            ids.append(_m_node(segments[i + 2], nodes))
        for nid in ids:
            if lane and nodes[nid].lane is None:
                nodes[nid].lane = lane
        for (arrow, label), a, b in zip(arrows, ids, ids[1:], strict=False):
            edges.append(
                Edge(
                    source=a,
                    target=b,
                    label=label,
                    dashed=arrow in ("-.->", "-.-"),
                    basis="PERSON",
                )
            )
    if not nodes:
        raise SpecError("no boxes were found in the Mermaid text")
    return "FLOW", parse_spec(
        {
            "direction": direction,
            "nodes": [n.model_dump() for n in nodes.values()],
            "edges": [e.model_dump() for e in edges],
        }
    )


def _parse_mermaid_sequence(lines: list[str]) -> Spec:
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    for ln in lines:
        m = re.match(r"^(participant|actor)\s+([A-Za-z0-9_]+)(?:\s+as\s+(.+))?$", ln, re.I)
        if m:
            nodes[m[2]] = Node(
                id=m[2],
                label=(m[3] or m[2]).strip(),
                kind="actor" if m[1].lower() == "actor" else "system",
                basis="PERSON",
            )
            continue
        m = re.match(
            r"^([A-Za-z0-9_]+)\s*(-->>|->>|-->|->|--x|-x)\s*[+-]?([A-Za-z0-9_]+)\s*:\s*(.*)$", ln
        )
        if m:
            for pid in (m[1], m[3]):
                nodes.setdefault(pid, Node(id=pid, label=pid, basis="PERSON"))
            edges.append(
                Edge(
                    source=m[1],
                    target=m[3],
                    label=m[4].strip(),
                    dashed=m[2].startswith("--"),
                    basis="PERSON",
                )
            )
            continue
        if re.match(
            r"^(note|loop|alt|else|opt|par|end|autonumber|rect|activate|deactivate)\b", ln, re.I
        ):
            continue
        raise SpecError(f"cannot read this line: {ln!r}")
    if not nodes:
        raise SpecError("no participants were found in the Mermaid text")
    return parse_spec(
        {
            "nodes": [n.model_dump() for n in nodes.values()],
            "edges": [e.model_dump() for e in edges],
        }
    )
