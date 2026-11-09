"""Draw a diagram structure as a PNG, for documents that cannot hold an SVG.

It uses the same layout as the SVG drawing (diagram_spec.layout), so a picture in
the Word report is the same picture a person sees on screen.
"""

from __future__ import annotations

import io
import math
from functools import lru_cache

from app.services import diagram_spec as ds

SCALE = 2

_FONT_FILES = (
    "arial.ttf",
    "Arial.ttf",
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "LiberationSans-Regular.ttf",
)
_BOLD_FILES = (
    "arialbd.ttf",
    "Arial Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
)


@lru_cache(maxsize=16)
def _font(size: int, bold: bool):
    from PIL import ImageFont

    for name in _BOLD_FILES if bold else _FONT_FILES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _dashed(draw, pts, fill, width, dash=(10, 6)):
    for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0:
            continue
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        pos, on = 0.0, True
        while pos < length:
            step = min(dash[0] if on else dash[1], length - pos)
            if on:
                draw.line(
                    [
                        (x1 + ux * pos, y1 + uy * pos),
                        (x1 + ux * (pos + step), y1 + uy * (pos + step)),
                    ],
                    fill=fill,
                    width=width,
                )
            pos += step
            on = not on


def _label_text(label: str) -> str:
    """A link label. One the system cut short is left off rather than printed half-said."""
    label = " ".join(label.split())
    return "" if label.endswith("\u2026") else label


def _wrap_label(text: str, width: int = 22) -> list[str]:
    return ds._wrap(text, width)


def render_png(spec: ds.Spec, style: ds.Style | None = None) -> bytes:
    from PIL import Image, ImageDraw

    style = style or ds.Style()
    p = ds._palette(style)
    laid = ds.layout(spec, style)
    tb = spec.direction == "TB"
    legend = 34 if {n.basis for n in spec.nodes} & {"ASSUMPTION", "PROPOSED"} else 0
    width, height = int(laid.width * SCALE), int((laid.height + legend) * SCALE)
    image = Image.new("RGB", (width, height), p["background"])
    d = ImageDraw.Draw(image)
    s = SCALE
    lw = max(2, int(style.line_width * s))
    small, normal, bold = _font(11 * s, False), _font(13 * s, False), _font(12 * s, True)

    for i, (name, start, size) in enumerate(laid.lanes):
        fill = p["lane"] if i % 2 == 0 else p["lane_alt"]
        if tb:
            d.rectangle([start * s, 0, (start + size) * s, laid.height * s], fill=fill)
            if name:
                d.text(
                    ((start + size / 2) * s, 20 * s),
                    name,
                    font=bold,
                    fill=p["bg_text"],
                    anchor="mm",
                )
        else:
            d.rectangle([0, start * s, laid.width * s, (start + size) * s], fill=fill)
            if name:
                lines = ds._wrap(name, 14)
                top = (start + size / 2) * s - (len(lines) - 1) * 7 * s
                for k, line in enumerate(lines):
                    d.text(
                        (14 * s, top + k * 14 * s), line, font=bold, fill=p["bg_text"], anchor="lm"
                    )

    for edge, pts, _at in laid.paths:
        scaled = [(x * s, y * s) for x, y in pts]
        if edge.dashed or edge.basis == "ASSUMPTION":
            _dashed(d, scaled, p["edge"], lw)
        elif edge.basis == "PROPOSED":
            _dashed(d, scaled, p["edge"], lw, dash=(4, 6))
        else:
            d.line(scaled, fill=p["edge"], width=lw, joint="curve")
        if len(scaled) >= 2:
            (x1, y1), (x2, y2) = scaled[-2], scaled[-1]
            angle = math.atan2(y2 - y1, x2 - x1)
            size = 9 * s
            d.polygon(
                [
                    (x2, y2),
                    (x2 - size * math.cos(angle - 0.4), y2 - size * math.sin(angle - 0.4)),
                    (x2 - size * math.cos(angle + 0.4), y2 - size * math.sin(angle + 0.4)),
                ],
                fill=p["edge"],
            )

    for b in laid.boxes.values():
        x, y, w, h = b.x * s, b.y * s, b.w * s, b.h * s
        outline, fill = p["stroke"], p["node"]
        kind = b.node.kind
        if kind == "decision":
            d.polygon(
                [(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)],
                fill=fill,
                outline=outline,
                width=lw,
            )
        elif kind == "start_end":
            d.rounded_rectangle(
                [x, y, x + w, y + h], radius=h / 2, fill=fill, outline=outline, width=lw
            )
        elif kind == "store":
            e = 7 * s
            d.rectangle([x, y + e, x + w, y + h - e], fill=fill, outline=outline, width=lw)
            d.ellipse([x, y, x + w, y + 2 * e], fill=fill, outline=outline, width=lw)
            d.ellipse([x, y + h - 2 * e, x + w, y + h], fill=fill, outline=outline, width=lw)
            d.rectangle([x + lw, y + e, x + w - lw, y + h - e], fill=fill)
        elif kind == "system":
            d.rectangle([x, y, x + w, y + h], fill=fill, outline=outline, width=lw)
        else:
            radius = max(style.corner * s, 0)
            d.rounded_rectangle(
                [x, y, x + w, y + h], radius=radius, fill=fill, outline=outline, width=lw
            )
            if kind == "actor":
                d.rounded_rectangle(
                    [x + 3 * s, y + 3 * s, x + w - 3 * s, y + h - 3 * s],
                    radius=max(radius - 2 * s, 0),
                    outline=outline,
                    width=max(1, lw // 2),
                )
        base = bold if kind in ("actor", "system", "start_end") else normal
        font = base
        inner = w - (26 * s if kind == "decision" else 12 * s)
        widest = max(d.textlength(line, font=font) for line in b.lines)
        size = 13
        while widest > inner and size > 8:
            size -= 1
            font = _font(size * s, kind in ("actor", "system", "start_end"))
            widest = max(d.textlength(line, font=font) for line in b.lines)
        first = (y + h / 2) - (len(b.lines) - 1) * ds._LINE_H * s / 2
        for k, line in enumerate(b.lines):
            d.text(
                (x + w / 2, first + k * ds._LINE_H * s),
                line,
                font=font,
                fill=p["node_text"],
                anchor="mm",
            )

    placed: list[tuple[float, float, float, float]] = []
    obstacles = [
        (bx.x * s, bx.y * s, (bx.x + bx.w) * s, (bx.y + bx.h) * s) for bx in laid.boxes.values()
    ]

    def hits(rect, others) -> bool:
        return any(
            rect[0] < o[2] and rect[2] > o[0] and rect[1] < o[3] and rect[3] > o[1] for o in others
        )


    def seg_key(a, b):
        horizontal = abs(a[1] - b[1]) < 1
        if horizontal:
            return ("h", round(a[1] / 4), round(min(a[0], b[0]) / 4), round(max(a[0], b[0]) / 4))
        return ("v", round(a[0] / 4), round(min(a[1], b[1]) / 4), round(max(a[1], b[1]) / 4))

    def overlaps(k1, k2) -> bool:
        return k1[:2] == k2[:2] and k1[2] < k2[3] and k2[2] < k1[3]

    all_segs = [
        seg_key(a, b)
        for _e, pts_, _p in laid.paths
        for a, b in zip(pts_, pts_[1:], strict=False)
        if abs(a[1] - b[1]) < 1 or abs(a[0] - b[0]) < 1
    ]

    def shared(seg) -> int:
        k = seg_key(*seg)
        return sum(1 for o in all_segs if overlaps(k, o)) > 1

    def shared_at(x, y) -> bool:
        return any(
            (o[0] == "h" and abs(o[1] * 4 - y) < 6 and o[2] * 4 <= x <= o[3] * 4)
            and sum(1 for q in all_segs if overlaps(o, q)) > 1
            for o in all_segs
        )

    for edge, pts, (lx, ly) in laid.paths:
        text = _label_text(edge.label)
        if not text:
            continue
        lines = _wrap_label(text)
        line_h = 14 * s
        text_w = max(d.textlength(line, font=small) for line in lines)
        half_w, half_h = text_w / 2 + 5 * s, len(lines) * line_h / 2 + 2 * s
        segments = sorted(
            zip(pts, pts[1:], strict=False),
            key=lambda seg: (
                shared(seg),
                -math.hypot(seg[1][0] - seg[0][0], seg[1][1] - seg[0][1]),
            ),
        )
        candidates = [] if shared_at(lx, ly) else [(lx * s, ly * s)]
        for (x1, y1), (x2, y2) in segments:
            for f in (0.5, 0.3, 0.7, 0.15, 0.85):
                candidates.append(((x1 + (x2 - x1) * f) * s, (y1 + (y2 - y1) * f) * s))
        chosen = None
        for cx, cy in candidates:
            cx = min(max(cx, half_w + 4 * s), width - half_w - 4 * s)
            cy = min(max(cy, half_h + 4 * s), height - half_h - 4 * s)
            rect = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
            if not hits(rect, placed) and not hits(rect, obstacles):
                chosen = (cx, cy, rect)
                break
        if chosen is None:
            cx = min(max(lx * s, half_w + 4 * s), width - half_w - 4 * s)
            cy = min(max(ly * s, half_h + 4 * s), height - half_h - 4 * s)
            chosen = (cx, cy, (cx - half_w, cy - half_h, cx + half_w, cy + half_h))
        cx, cy, rect = chosen
        placed.append(rect)
        d.rectangle(rect, fill=p["background"])
        top = cy - (len(lines) - 1) * line_h / 2
        for k, line in enumerate(lines):
            d.text((cx, top + k * line_h), line, font=small, fill=p["bg_text"], anchor="mm")

    if legend:
        y = (laid.height + 14) * s
        d.text(
            (ds._PAD * s, y),
            "Dashed outline: needs confirming     Dotted outline: proposal, not yet committed",
            font=small,
            fill=p["bg_text"],
            anchor="lm",
        )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
