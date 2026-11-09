"use client";

import { useEffect, useRef, useState } from "react";

export type Crop = { x: number; y: number; w: number; h: number };
export type FigureMode = "view" | "crop" | "pick";

const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value));

/**
 * A rectangle the person drags over a picture, as fractions of it.
 * Used to crop, and to point at the part the AI should look at.
 */
function RegionBox({
  start,
  onChange,
}: {
  start: Crop;
  onChange: (rect: Crop) => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const [rect, setRect] = useState<Crop>(start);
  const drag = useRef<{
    how: string;
    x: number;
    y: number;
    from: Crop;
  } | null>(null);

  useEffect(() => onChange(rect), [rect, onChange]);

  const begin = (how: string) => (event: React.PointerEvent) => {
    event.preventDefault();
    event.stopPropagation();
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    drag.current = { how, x: event.clientX, y: event.clientY, from: rect };
  };
  const move = (event: React.PointerEvent) => {
    const d = drag.current;
    const box = holder.current?.getBoundingClientRect();
    if (!d || !box) return;
    const dx = (event.clientX - d.x) / box.width;
    const dy = (event.clientY - d.y) / box.height;
    const min = 0.05;
    let { x, y, w, h } = d.from;
    if (d.how === "move") {
      x = clamp(x + dx, 0, 1 - w);
      y = clamp(y + dy, 0, 1 - h);
    } else {
      if (d.how.includes("w")) {
        const nx = clamp(x + dx, 0, x + w - min);
        w = x + w - nx;
        x = nx;
      }
      if (d.how.includes("e")) w = clamp(w + dx, min, 1 - x);
      if (d.how.includes("n")) {
        const ny = clamp(y + dy, 0, y + h - min);
        h = y + h - ny;
        y = ny;
      }
      if (d.how.includes("s")) h = clamp(h + dy, min, 1 - y);
    }
    setRect({ x, y, w, h });
  };
  const end = () => {
    drag.current = null;
  };

  return (
    <div ref={holder} className="region-layer">
      <div
        className="region-box"
        style={{
          left: `${rect.x * 100}%`,
          top: `${rect.y * 100}%`,
          width: `${rect.w * 100}%`,
          height: `${rect.h * 100}%`,
        }}
        onPointerDown={begin("move")}
        onPointerMove={move}
        onPointerUp={end}
      >
        {["nw", "ne", "sw", "se"].map((corner) => (
          <i
            key={corner}
            className={`region-handle ${corner}`}
            onPointerDown={begin(corner)}
            onPointerMove={move}
            onPointerUp={end}
          />
        ))}
      </div>
    </div>
  );
}

/** The caption typed over in place. Enter or clicking away keeps it; Escape drops it. */
function CaptionEditor({
  text,
  onDone,
}: {
  text: string;
  onDone: (value: string | null) => void;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const cancelled = useRef(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  }, []);
  return (
    <span
      ref={ref}
      className="caption-text typing"
      contentEditable
      suppressContentEditableWarning
      role="textbox"
      aria-label="Edit the caption"
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => {
        event.stopPropagation();
        if (event.key === "Enter") {
          event.preventDefault();
          ref.current?.blur();
        } else if (event.key === "Escape") {
          cancelled.current = true;
          ref.current?.blur();
        }
      }}
      onPaste={(event) => {
        event.preventDefault();
        document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
      }}
      onBlur={() =>
        onDone(
          cancelled.current ? null : (ref.current?.textContent ?? "").replace(/\s+/g, " ").trim(),
        )
      }
    >
      {text}
    </span>
  );
}

/**
 * A picture in the document, laid out the way a person set it: its width, the part
 * of it shown, a caption. Selecting it shows its handles and a small bar.
 */
export function DocFigure({
  figureKey,
  src,
  natural,
  widthPct,
  crop,
  label,
  caption,
  editable,
  selected,
  mode,
  canDelete,
  onSelect,
  onResize,
  onCropMode,
  onRegion,
  onCancelMode,
  onResetCrop,
  onResetSize,
  onAsk,
  onDelete,
  onCaption,
  onContextMenu,
}: {
  figureKey: string;
  src: string | null;
  natural: { w: number; h: number } | null;
  widthPct: number;
  crop: Crop | null;
  label: string | null;
  caption: string | null;
  editable: boolean;
  selected: boolean;
  mode: FigureMode;
  canDelete: boolean;
  onSelect: () => void;
  onResize: (pct: number) => void;
  onCropMode: () => void;
  onRegion: (rect: Crop | null) => void;
  onCancelMode: () => void;
  onResetCrop: () => void;
  onResetSize: () => void;
  onAsk: (at: { x: number; y: number }) => void;
  onDelete: () => void;
  onCaption: (text: string) => void;
  onContextMenu: (event: React.MouseEvent) => void;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const [measured, setMeasured] = useState<{ w: number; h: number } | null>(null);
  const [live, setLive] = useState<number | null>(null);
  const [rect, setRect] = useState<Crop | null>(null);
  const [typing, setTyping] = useState(false);
  const size = natural ?? measured;
  const shownCrop = mode === "crop" ? null : crop;
  const width = live ?? widthPct;

  const startResize = (dir: 1 | -1) => (event: React.PointerEvent) => {
    event.preventDefault();
    event.stopPropagation();
    const holder = frame.current?.parentElement?.getBoundingClientRect();
    const box = frame.current?.getBoundingClientRect();
    if (!holder || !box) return;
    const target = event.currentTarget as HTMLElement;
    target.setPointerCapture(event.pointerId);
    const x0 = event.clientX;
    const w0 = box.width;
    let last = widthPct;
    const move = (e: PointerEvent) => {
      // The picture is centred, so each side moves by half of the change in width.
      const px = w0 + (e.clientX - x0) * dir * 2;
      last = Math.round(clamp((px / holder.width) * 100, 10, 100));
      setLive(last);
    };
    const up = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
      setLive(null);
      if (last !== widthPct) onResize(last);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
  };

  const cropStyle =
    shownCrop && size
      ? { aspectRatio: `${(shownCrop.w * size.w) / (shownCrop.h * size.h)}` }
      : undefined;
  const imageStyle =
    shownCrop && size
      ? {
          position: "absolute" as const,
          maxWidth: "none",
          width: `${100 / shownCrop.w}%`,
          left: `${(-shownCrop.x / shownCrop.w) * 100}%`,
          top: `${(-shownCrop.y / shownCrop.h) * 100}%`,
        }
      : undefined;
  const initial: Crop =
    mode === "crop" ? (crop ?? { x: 0.1, y: 0.1, w: 0.8, h: 0.8 }) : { x: 0.25, y: 0.25, w: 0.5, h: 0.5 };

  return (
    <figure
      className={`doc-figure${selected ? " selected" : ""}`}
      data-figure={figureKey}
      tabIndex={editable ? 0 : undefined}
      onClick={(event) => {
        event.stopPropagation();
        if (editable) onSelect();
      }}
      onContextMenu={onContextMenu}
      onKeyDown={(event) => {
        if (event.key === "Delete" && selected && canDelete) onDelete();
        if (event.key === "Escape" && mode !== "view") onCancelMode();
      }}
    >
      {selected && editable && mode === "view" && (
        <div className="doc-figure-bar" role="toolbar" aria-label="Picture">
          <button type="button" className="ghost" onClick={onCropMode}>
            Crop
          </button>
          {crop && (
            <button type="button" className="ghost" onClick={onResetCrop}>
              Show all
            </button>
          )}
          <button type="button" className="ghost" onClick={onResetSize}>
            Reset size
          </button>
          <button
            type="button"
            className="ai"
            onClick={(event) => {
              const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
              onAsk({ x: box.left, y: box.bottom + 6 });
            }}
          >
            ✨ Ask AI
          </button>
          {canDelete && (
            <button type="button" className="ghost danger" onClick={onDelete}>
              Delete
            </button>
          )}
          <small>{width}% wide · drag a corner to resize</small>
        </div>
      )}
      <div ref={frame} className="doc-figure-frame" style={{ width: `${width}%` }}>
        <div className="doc-crop" style={cropStyle}>
          {src ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={src}
              alt={label ?? "Picture"}
              draggable={false}
              style={imageStyle}
              onLoad={(event) => {
                const img = event.currentTarget;
                if (img.naturalWidth > 0)
                  setMeasured({ w: img.naturalWidth, h: img.naturalHeight || img.naturalWidth });
              }}
            />
          ) : (
            <p className="muted-line">Loading…</p>
          )}
        </div>
        {mode !== "view" && src && <RegionBox start={initial} onChange={setRect} />}
        {selected && editable && mode === "view" &&
          (["nw", "ne", "sw", "se"] as const).map((corner) => (
            <i
              key={corner}
              className={`size-handle ${corner}`}
              onPointerDown={startResize(corner.includes("e") ? 1 : -1)}
            />
          ))}
      </div>
      {mode !== "view" && (
        <div className="doc-figure-bar">
          <strong>{mode === "crop" ? "Drag to choose what to keep" : "Drag over the part to ask about"}</strong>
          <button type="button" onClick={() => onRegion(rect)}>
            {mode === "crop" ? "Apply crop" : "Use this part"}
          </button>
          <button type="button" className="ghost" onClick={onCancelMode}>
            Cancel
          </button>
        </div>
      )}
      {(label || caption || editable) && (
        <figcaption>
          {label}
          {label ? " " : ""}
          {typing ? (
            <CaptionEditor
              text={caption ?? ""}
              onDone={(value) => {
                setTyping(false);
                if (value !== null && value !== (caption ?? "")) onCaption(value);
              }}
            />
          ) : (
            <span
              className={`caption-text${editable ? " editable" : ""}`}
              title={editable ? "Click to edit the caption" : undefined}
              onClick={(event) => {
                if (!editable) return;
                event.stopPropagation();
                onSelect();
                setTyping(true);
              }}
            >
              {caption || (editable ? "Add a caption…" : "")}
            </span>
          )}
        </figcaption>
      )}
    </figure>
  );
}
