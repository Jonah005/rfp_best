"use client";

import { useEffect, useRef, useState } from "react";

export type MenuItem =
  | { separator: true }
  | {
      label: string;
      hint?: string;
      disabled?: boolean;
      accent?: boolean;
      danger?: boolean;
      onClick: () => void;
    };

/**
 * The right-click menu of the document: the usual editing commands, with the AI
 * option added to them. It replaces the browser's own menu so both live in one place.
 */
export function DocMenu({
  at,
  items,
  onClose,
}: {
  at: { x: number; y: number };
  items: MenuItem[];
  onClose: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState(at);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    setPos({
      x: Math.max(8, Math.min(at.x, window.innerWidth - el.offsetWidth - 8)),
      y: Math.max(8, Math.min(at.y, window.innerHeight - el.offsetHeight - 8)),
    });
  }, [at.x, at.y]);

  useEffect(() => {
    const away = (event: MouseEvent) => {
      if (!(event.target as HTMLElement).closest?.(".doc-menu")) onClose();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", away);
    window.addEventListener("keydown", key);
    window.addEventListener("blur", onClose);
    return () => {
      document.removeEventListener("mousedown", away);
      window.removeEventListener("keydown", key);
      window.removeEventListener("blur", onClose);
    };
  }, [onClose]);

  return (
    <div
      ref={box}
      className="doc-menu"
      role="menu"
      style={{ left: pos.x, top: pos.y }}
      // Keep the selection and the focus where they are while the menu is used.
      onMouseDown={(event) => event.preventDefault()}
      onContextMenu={(event) => event.preventDefault()}
    >
      {items.map((item, index) =>
        "separator" in item ? (
          <hr key={`sep-${index}`} />
        ) : (
          <button
            key={item.label}
            type="button"
            role="menuitem"
            disabled={item.disabled}
            className={`${item.accent ? "accent" : ""}${item.danger ? " danger" : ""}`}
            onClick={() => {
              onClose();
              item.onClick();
            }}
          >
            <span>{item.label}</span>
            {item.hint && <kbd>{item.hint}</kbd>}
          </button>
        ),
      )}
    </div>
  );
}
