"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useDiagramSrc } from "./diagrams";
import { DocFigure, type Crop, type FigureMode } from "./doc-figure";
import { DocMenu, type MenuItem } from "./doc-menu";
import {
  PictureAskPopup,
  RewritePopup,
  type PictureTarget,
  type RewriteTarget,
} from "./rewrite-popup";

export type ProposalStatement = {
  id: string;
  order_index: number;
  statement_class: string;
  text: string;
  section?: string | null;
  diagram_id?: string | null;
  diagram_version_no?: number | null;
  out_of_date?: boolean;
  out_of_date_reason?: string | null;
  paragraph_no?: number | null;
  requirement_codes?: string[] | null;
};

type PictureMeta = {
  id: string;
  kind: "UPLOAD" | "DIAGRAM";
  section: string | null;
  diagram_id: string | null;
  after_paragraph: number;
  position: number;
  natural_width: number | null;
  natural_height: number | null;
  width_pct: number;
  crop: Crop | null;
  caption: string | null;
};

type Layout = { width_pct?: number; crop?: Crop | null; caption?: string | null };

export const SENTENCE_TITLE: Record<string, string> = {
  PROPOSED_COMMITMENT: "Promise — binds the company and needs an approver",
  ASSUMPTION: "Assumption — true only if the client agrees",
  EVIDENCE_BACKED_FACT: "Fact — backed by approved evidence",
  TENDER_DERIVED: "States what the tender requires",
  DIAGRAM_DESCRIPTION: "Describes the diagram",
  HUMAN_PROVIDED_INPUT: "Written by a person",
};

const BACKED_CLASSES = ["EVIDENCE_BACKED_FACT", "TENDER_DERIVED"];
const MAX_PICTURE_BYTES = 8 * 1024 * 1024;

type Place = { section: string | null; diagramId: string | null; paragraph: number };
type TextPopup = {
  at: { x: number; y: number };
  target: RewriteTarget;
  alternative?: RewriteTarget;
  stage?: "add" | "delete";
};
type AskPicture = {
  at: { x: number; y: number };
  key: string;
  target: PictureTarget;
  region: Crop | null;
  hidden: boolean;
};

function sentenceOf(node: EventTarget | Node | null): HTMLElement | null {
  const element = node instanceof Element ? node : node instanceof Node ? node.parentElement : null;
  return element?.closest<HTMLElement>("[data-sid]") ?? null;
}

function readAsBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("That picture could not be read."));
    reader.readAsDataURL(file);
  });
}

/** One sentence being typed over in place. Enter or clicking away keeps it; Escape drops it. */
function EditableSentence({
  text,
  caret,
  className,
  onSave,
  onCancel,
}: {
  text: string;
  caret: number;
  className: string;
  onSave: (text: string) => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const cancelled = useRef(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.focus();
    const node = el.firstChild;
    const range = document.createRange();
    if (node) range.setStart(node, Math.min(caret, node.textContent?.length ?? 0));
    else range.setStart(el, 0);
    range.collapse(true);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <span
      ref={ref}
      className={`${className} sent-editing`}
      contentEditable
      suppressContentEditableWarning
      spellCheck
      role="textbox"
      aria-label="Edit this sentence"
      data-editing="true"
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          ref.current?.blur();
        } else if (event.key === "Escape") {
          event.preventDefault();
          cancelled.current = true;
          ref.current?.blur();
        }
      }}
      onPaste={(event) => {
        // Pictures are handled by the document; text goes in as plain text.
        if ([...event.clipboardData.files].some((f) => f.type.startsWith("image/"))) return;
        event.preventDefault();
        document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
      }}
      onBlur={() => {
        const next = (ref.current?.textContent ?? "").replace(/\s+/g, " ").trim();
        if (cancelled.current || !next || next === text) onCancel();
        else onSave(next);
      }}
    >
      {text}
    </span>
  );
}

type FigureShared = {
  figureKey: string;
  natural: { w: number; h: number } | null;
  widthPct: number;
  crop: Crop | null;
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
};

function DiagramFigure({
  diagramId,
  versionNo,
  label,
  ...shared
}: FigureShared & { diagramId: string; versionNo: number; label: string }) {
  const src = useDiagramSrc(diagramId, versionNo);
  return <DocFigure src={src} label={label} {...shared} />;
}

function UploadFigure({
  artifactId,
  imageId,
  ...shared
}: FigureShared & { artifactId: string; imageId: string }) {
  const data = useQuery({
    queryKey: ["draft-image-data", imageId],
    queryFn: () => api<{ data_url: string }>(`/artifacts/${artifactId}/images/${imageId}/data`),
    staleTime: Infinity,
  });
  return <DocFigure src={data.data?.data_url ?? null} label={null} {...shared} />;
}

export function ProposalView({
  statements,
  artifactId,
  taskId,
  editable,
  onChanged,
  heldCodes = {},
  heldSections = {},
}: {
  statements: ProposalStatement[];
  artifactId?: string;
  taskId?: string;
  editable: boolean;
  onChanged: () => void;
  /** Requirement code -> the department a part of the draft is waiting on. */
  heldCodes?: Record<string, string>;
  /** Section title -> the department the whole section is waiting on. */
  heldSections?: Record<string, string>;
}) {
  const queryClient = useQueryClient();
  const root = useRef<HTMLElement>(null);
  const clickTimer = useRef<number | null>(null);
  const place = useRef<Place | null>(null);
  const [popup, setPopup] = useState<TextPopup | null>(null);
  const [menu, setMenu] = useState<{ at: { x: number; y: number }; items: MenuItem[] } | null>(
    null,
  );
  const [editing, setEditing] = useState<{ id: string; caret: number } | null>(null);
  // What a person just typed, shown until the saved draft comes back.
  const [typed, setTyped] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [mode, setMode] = useState<{ key: string; mode: FigureMode }>({ key: "", mode: "view" });
  const [tweak, setTweak] = useState<Record<string, Layout>>({});
  const [ask, setAsk] = useState<AskPicture | null>(null);
  // The words the AI is working on, kept marked while its bar is open.
  const [lit, setLit] = useState<RewriteTarget["items"] | null>(null);
  const [confirmLeave, setConfirmLeave] = useState<{ id: string; title: string } | null>(null);
  const canEdit = editable && Boolean(artifactId);

  const pictures = useQuery({
    queryKey: ["draft-images", artifactId],
    queryFn: () => api<PictureMeta[]>(`/artifacts/${artifactId}/images`),
    enabled: Boolean(artifactId),
  });
  const metas = pictures.data;
  const refreshPictures = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ["draft-images", artifactId] }),
    [queryClient, artifactId],
  );

  useEffect(() => {
    setTyped({});
  }, [statements]);
  useEffect(() => {
    setTweak({});
  }, [metas]);

  // Clicking anywhere else lets go of the selected picture.
  useEffect(() => {
    const away = (event: MouseEvent) => {
      const el = event.target as HTMLElement;
      if (el.closest?.(".doc-figure, .rewrite-popup, .doc-menu")) return;
      setSelected(null);
      setMode({ key: "", mode: "view" });
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, []);

  useEffect(() => {
    if (!popup) return;
    const away = (event: MouseEvent) => {
      if (!(event.target as HTMLElement).closest?.(".rewrite-popup")) {
        setPopup(null);
        setLit(null);
      }
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [popup]);

  const changed = () => {
    onChanged();
    void refreshPictures();
  };

  useEffect(() => {
    const registry = (CSS as unknown as { highlights?: Map<string, unknown> }).highlights;
    const Mark = (window as unknown as { Highlight?: new (...ranges: Range[]) => unknown })
      .Highlight;
    if (!registry || !Mark || !lit) return;
    const ranges: Range[] = [];
    for (const item of lit) {
      const node = document.getElementById(`st-${item.statement_id}`)?.firstChild;
      if (!node) continue;
      const length = node.textContent?.length ?? 0;
      const range = document.createRange();
      range.setStart(node, Math.min(item.start ?? 0, length));
      range.setEnd(node, Math.min(item.end ?? length, length));
      ranges.push(range);
    }
    registry.set("rw-target", new Mark(...ranges));
    return () => void registry.delete("rw-target");
  }, [lit, statements]);

  const fail = (failure: unknown, fallback: string) =>
    setProblem(failure instanceof Error ? failure.message : fallback);

  // --- pictures: adding ---------------------------------------------------------

  const notePlace = (node: EventTarget | null) => {
    const p = (node instanceof Element ? node : null)?.closest<HTMLElement>("p[data-pno]");
    if (p)
      place.current = {
        section: p.dataset.sec || null,
        diagramId: p.dataset.did || null,
        paragraph: Number(p.dataset.pno),
      };
  };

  const lastPlace = (): Place | null => {
    if (place.current) return place.current;
    const all = Array.from(root.current?.querySelectorAll<HTMLElement>("p[data-pno]") ?? []);
    const p = all[all.length - 1];
    return p
      ? { section: p.dataset.sec || null, diagramId: p.dataset.did || null, paragraph: Number(p.dataset.pno) }
      : null;
  };

  const addPicture = useCallback(
    async (file: Blob, name: string | null, where: Place | null) => {
      if (!artifactId) return;
      if (!where) return setProblem("Click in the document first, then paste the picture.");
      if (!file.type.startsWith("image/")) return setProblem("Only pictures can be added.");
      if (file.size > MAX_PICTURE_BYTES) return setProblem("That picture is larger than 8 MB.");
      setProblem(null);
      try {
        const content_base64 = await readAsBase64(file);
        await api(`/artifacts/${artifactId}/images`, {
          method: "POST",
          body: JSON.stringify({
            content_base64,
            filename: name,
            section: where.section,
            diagram_id: where.diagramId,
            after_paragraph: where.paragraph,
          }),
        });
        await refreshPictures();
        setInfo("Picture added. Drag its corners to resize it, or right-click it for more.");
      } catch (failure) {
        fail(failure, "That picture was not added.");
      }
    },
    [artifactId, refreshPictures],
  );

  const addRef = useRef(addPicture);
  addRef.current = addPicture;
  const placeRef = useRef(lastPlace);
  placeRef.current = lastPlace;

  // Ctrl+V with a picture on the clipboard puts it after the paragraph last clicked.
  useEffect(() => {
    if (!canEdit) return;
    const onPaste = (event: ClipboardEvent) => {
      const file = [...(event.clipboardData?.files ?? [])].find((f) => f.type.startsWith("image/"));
      if (!file) return;
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;
      event.preventDefault();
      void addRef.current(file, file.name || null, placeRef.current());
    };
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
  }, [canEdit]);

  const chooseFile = (where: Place | null) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/gif,image/webp";
    input.onchange = () => {
      const file = input.files?.[0];
      if (file) void addPicture(file, file.name, where);
    };
    input.click();
  };

  const pasteFromClipboard = async (where: Place | null, intoText: boolean) => {
    try {
      const items = await navigator.clipboard.read();
      for (const item of items) {
        const type = item.types.find((t) => t.startsWith("image/"));
        if (type) return void addPicture(await item.getType(type), null, where);
      }
      if (intoText) {
        const text = await navigator.clipboard.readText();
        if (text) document.execCommand("insertText", false, text);
      } else {
        setInfo("There is no picture on the clipboard. Click a sentence to paste text into it.");
      }
    } catch {
      setProblem("The browser did not allow reading the clipboard. Press Ctrl+V instead.");
    }
  };

  // --- pictures: laying out -----------------------------------------------------

  const metaFor = (key: string): PictureMeta | undefined =>
    metas?.find((m) => (m.kind === "UPLOAD" ? `u:${m.id}` : `d:${m.diagram_id}`) === key);

  const layoutOf = (key: string, fallbackWidth: number) => {
    const meta = metaFor(key);
    const t = tweak[key] ?? {};
    return {
      width: t.width_pct ?? meta?.width_pct ?? fallbackWidth,
      crop: t.crop !== undefined ? t.crop : (meta?.crop ?? null),
      caption: t.caption !== undefined ? t.caption : (meta?.caption ?? null),
    };
  };

  const saveLayout = async (key: string, fields: Layout) => {
    if (!artifactId) return;
    setTweak((now) => ({ ...now, [key]: { ...now[key], ...fields } }));
    setProblem(null);
    try {
      const path = key.startsWith("u:")
        ? `/artifacts/${artifactId}/images/${key.slice(2)}`
        : `/artifacts/${artifactId}/figures/${key.slice(2)}`;
      await api(path, {
        method: key.startsWith("u:") ? "PATCH" : "PUT",
        body: JSON.stringify(fields),
      });
      await refreshPictures();
    } catch (failure) {
      setTweak({});
      fail(failure, "That change was not saved.");
    }
  };

  const deletePicture = async (key: string) => {
    if (!artifactId || !key.startsWith("u:")) return;
    try {
      await api(`/artifacts/${artifactId}/images/${key.slice(2)}`, { method: "DELETE" });
      setSelected(null);
      await refreshPictures();
    } catch (failure) {
      fail(failure, "That picture was not deleted.");
    }
  };

  const leaveDiagramOut = async (diagramId: string) => {
    try {
      await api(`/tasks/${taskId}/writeup/diagrams/${diagramId}/leave-out`, { method: "POST" });
      setConfirmLeave(null);
      setSelected(null);
      onChanged();
      await refreshPictures();
      await queryClient.invalidateQueries({ queryKey: ["writeup", taskId] });
    } catch (failure) {
      fail(failure, "That diagram was not left out.");
    }
  };

  const sectionOfKey = (key: string): { title: string | null; versionNo?: number } | null => {
    if (key.startsWith("d:")) {
      const first = statements.find((s) => s.diagram_id === key.slice(2));
      return first ? { title: first.section ?? null } : null;
    }
    return null;
  };

  const askAboutPicture = (key: string, at: { x: number; y: number }) => {
    const meta = metaFor(key);
    let target: PictureTarget;
    if (key.startsWith("u:")) {
      const here = statements
        .filter(
          (s) =>
            (s.section ?? null) === (meta?.section ?? null) &&
            (s.diagram_id ?? null) === (meta?.diagram_id ?? null) &&
            (s.paragraph_no ?? 1) === (meta?.after_paragraph ?? 1),
        )
        .sort((a, b) => a.order_index - b.order_index);
      target = {
        kind: "upload",
        imageId: key.slice(2),
        label: "this picture",
        anchorStatementId: here[here.length - 1]?.id,
      };
    } else {
      const top = statements
        .filter((s) => s.diagram_id === key.slice(2))
        .sort((a, b) => a.order_index - b.order_index)[0];
      target = {
        kind: "figure",
        diagramId: key.slice(2),
        label: "this diagram",
        anchorStatementId: top?.id,
        placement: "start",
      };
    }
    setMenu(null);
    setAsk({ at, key, target, region: null, hidden: false });
  };

  const figureMenu = (key: string, event: React.MouseEvent) => {
    if (!canEdit) return;
    event.preventDefault();
    event.stopPropagation();
    setSelected(key);
    const upload = key.startsWith("u:");
    const at = { x: event.clientX, y: event.clientY };
    const cropped = Boolean(layoutOf(key, 100).crop);
    const where = (() => {
      const meta = metaFor(key);
      if (meta && upload)
        return { section: meta.section, diagramId: meta.diagram_id, paragraph: meta.after_paragraph };
      return lastPlace();
    })();
    setMenu({
      at,
      items: [
        { label: "✨ Ask AI about this picture…", accent: true, onClick: () => askAboutPicture(key, at) },
        { separator: true },
        { label: "Crop", onClick: () => setMode({ key, mode: "crop" }) },
        { label: "Show the whole picture", disabled: !cropped, onClick: () => void saveLayout(key, { crop: null }) },
        { label: "Reset size", onClick: () => void saveLayout(key, { width_pct: 100 }) },
        { separator: true },
        { label: "Paste a picture below", hint: "Ctrl+V", onClick: () => void pasteFromClipboard(where, false) },
        { label: "Insert a picture from a file…", onClick: () => chooseFile(where) },
        { separator: true },
        upload
          ? { label: "Delete picture", hint: "Del", danger: true, onClick: () => void deletePicture(key) }
          : {
              label: "Leave this diagram out of the response…",
              danger: true,
              onClick: () =>
                setConfirmLeave({ id: key.slice(2), title: sectionOfKey(key)?.title ?? "this diagram" }),
            },
      ],
    });
  };

  // --- text: what is selected, and what the menu offers ---------------------------

  const piece = (span: HTMLElement) => {
    const length = span.textContent?.length ?? 0;
    return { statement_id: span.dataset.sid as string, start: 0, end: length };
  };

  const paragraphTarget = (span: HTMLElement): RewriteTarget => {
    const spans = Array.from(
      span.closest("p")?.querySelectorAll<HTMLElement>("[data-sid]") ?? [span],
    );
    return {
      items: spans.map(piece),
      label: spans.length > 1 ? `paragraph (${spans.length} sentences)` : "sentence",
      paragraph: spans.length > 1,
      anchorId: span.dataset.sid as string,
      menu: false,
    };
  };

  // The part of the draft the person has selected: whole sentences, or words inside one.
  const selectedTarget = (): RewriteTarget | null => {
    const selection = window.getSelection();
    const holder = root.current;
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed || !holder) return null;
    const range = selection.getRangeAt(0);
    if (!holder.contains(range.commonAncestorContainer)) return null;
    const items: { statement_id: string; start: number; end: number }[] = [];
    let whole = true;
    for (const span of Array.from(holder.querySelectorAll<HTMLElement>("[data-sid]"))) {
      if (!range.intersectsNode(span)) continue;
      const text = span.textContent ?? "";
      const node = span.firstChild;
      let start = 0;
      let end = text.length;
      if (range.startContainer === node) start = range.startOffset;
      if (range.endContainer === node) end = range.endOffset;
      while (start < end && /\s/.test(text[start])) start += 1;
      while (end > start && /\s/.test(text[end - 1])) end -= 1;
      if (end <= start) continue;
      if (start > 0 || end < text.length) whole = false;
      items.push({ statement_id: span.dataset.sid as string, start, end });
    }
    if (items.length === 0) return null;
    if (items.length > 1) {
      return {
        items: items.map((i) => ({ statement_id: i.statement_id })),
        label: `${items.length} sentences`,
        paragraph: true,
        anchorId: items[0].statement_id,
        menu: false,
      };
    }
    const only = items[0];
    const words = (selection.toString().trim().match(/\S+/g) ?? []).length;
    return {
      items,
      label: whole ? "sentence" : words <= 1 ? "word" : "part of the sentence",
      paragraph: false,
      anchorId: only.statement_id,
      menu: false,
    };
  };

  const onMouseUp = (event: React.MouseEvent) => {
    if (!canEdit || editing || (event.target as HTMLElement).closest(".rewrite-popup")) return;
    notePlace(event.target);
    // Let the browser finish its own selection (double-click picks a word) first.
    window.setTimeout(() => {
      const target = selectedTarget();
      const selection = window.getSelection();
      if (!target || !selection || selection.rangeCount === 0) return;
      const box = selection.getRangeAt(0).getBoundingClientRect();
      setPopup({
        at: { x: box.left, y: Math.min(box.bottom + 8, window.innerHeight - 220) },
        target,
      });
    }, 0);
  };

  const save = async (id: string, text: string) => {
    setEditing(null);
    setTyped((now) => ({ ...now, [id]: text }));
    setProblem(null);
    try {
      await api(`/artifacts/${artifactId}/statements/edits`, {
        method: "POST",
        body: JSON.stringify({ edits: [{ statement_id: id, text }] }),
      });
      onChanged();
    } catch (failure) {
      setTyped({});
      fail(failure, "That change was not saved.");
    }
  };

  // Asking the AI while a sentence is open for typing: keep what was typed first, then
  // work on exactly the words that were selected inside it.
  const askFromEditing = async (at: { x: number; y: number }) => {
    const el = root.current?.querySelector<HTMLElement>('[data-editing="true"]');
    if (!el || !editing) return;
    const id = editing.id;
    const text = (el.textContent ?? "").replace(/\s+/g, " ").trim();
    const selection = window.getSelection();
    let start = 0;
    let end = text.length;
    let words = 0;
    if (selection && selection.rangeCount && !selection.isCollapsed && el.contains(selection.anchorNode)) {
      const range = selection.getRangeAt(0);
      if (range.startContainer === el.firstChild) start = range.startOffset;
      if (range.endContainer === el.firstChild) end = range.endOffset;
      words = (selection.toString().trim().match(/\S+/g) ?? []).length;
    }
    const whole = start <= 0 && end >= text.length;
    const original = statements.find((s) => s.id === id)?.text;
    if (text && text !== original) await save(id, text);
    else setEditing(null);
    window.getSelection()?.removeAllRanges();
    setPopup({
      at,
      target: {
        items: [{ statement_id: id, start, end }],
        label: whole ? "sentence" : words <= 1 ? "word" : "part of the sentence",
        paragraph: false,
        anchorId: id,
        menu: false,
      },
    });
  };

  const onContextMenu = (event: React.MouseEvent) => {
    if (!canEdit) return;
    const at = { x: event.clientX, y: event.clientY };
    const inEdit = Boolean(
      (event.target as HTMLElement).closest?.('[data-editing="true"]') && editing,
    );
    const span = sentenceOf(event.target);
    if (!span && !inEdit) return;
    event.preventDefault();
    notePlace(event.target);
    const selection = window.getSelection();
    const hasSelection = Boolean(selection && !selection.isCollapsed);
    const where = place.current;
    const sid = span?.dataset.sid ?? editing?.id ?? "";

    const chosen = inEdit ? null : selectedTarget();
    const covers = chosen?.items.some((i) => i.statement_id === sid);
    const sentenceTarget: RewriteTarget = {
      items: span ? [piece(span)] : [],
      label: "sentence",
      paragraph: false,
      anchorId: sid,
      menu: false,
    };
    const target = chosen && covers ? chosen : sentenceTarget;
    const alternative = chosen && covers ? undefined : span ? paragraphTarget(span) : undefined;
    const exec = (command: string) => () => void document.execCommand(command);

    const items: MenuItem[] = [
      {
        label: "✨ Ask AI to reword…",
        accent: true,
        onClick: () => {
          if (inEdit) void askFromEditing(at);
          else
            setPopup({
              at: { x: at.x, y: Math.min(at.y, window.innerHeight - 260) },
              target,
              alternative: alternative && alternative.items.length > 1 ? alternative : undefined,
            });
        },
      },
      { separator: true },
      { label: "Undo", hint: "Ctrl+Z", disabled: !inEdit, onClick: exec("undo") },
      { label: "Redo", hint: "Ctrl+Y", disabled: !inEdit, onClick: exec("redo") },
      { separator: true },
      { label: "Cut", hint: "Ctrl+X", disabled: !inEdit || !hasSelection, onClick: exec("cut") },
      { label: "Copy", hint: "Ctrl+C", disabled: !hasSelection, onClick: exec("copy") },
      { label: "Paste", hint: "Ctrl+V", onClick: () => void pasteFromClipboard(where, inEdit) },
      {
        label: "Select All",
        hint: "Ctrl+A",
        onClick: () => {
          if (inEdit) document.execCommand("selectAll");
          else if (root.current) window.getSelection()?.selectAllChildren(root.current);
        },
      },
    ];
    if (!inEdit && span) {
      items.push(
        { separator: true },
        {
          label: "Edit this sentence",
          onClick: () => setEditing({ id: sid, caret: span.textContent?.length ?? 0 }),
        },
        {
          label: "Add a sentence after",
          onClick: () => setPopup({ at, target: sentenceTarget, stage: "add" }),
        },
        {
          label: "Delete sentence",
          danger: true,
          onClick: () => setPopup({ at, target: sentenceTarget, stage: "delete" }),
        },
      );
    }
    items.push(
      { separator: true },
      { label: "Insert a picture from a file…", onClick: () => chooseFile(where) },
    );
    setMenu({ at, items });
  };

  const onClick = (event: React.MouseEvent) => {
    notePlace(event.target);
    if (!canEdit || popup || editing || menu) return;
    if (clickTimer.current) window.clearTimeout(clickTimer.current);
    if (event.detail > 1) return;
    const span = sentenceOf(event.target);
    if (!span) return;
    const selection = window.getSelection();
    const caret =
      selection && selection.anchorNode === span.firstChild
        ? selection.anchorOffset
        : (span.textContent?.length ?? 0);
    const id = span.dataset.sid as string;
    // A beat of delay so a double-click can select a word instead of opening the editor.
    clickTimer.current = window.setTimeout(() => {
      if (window.getSelection()?.isCollapsed) setEditing({ id, caret });
    }, 230);
  };

  // --- laying the document out ------------------------------------------------------

  type Section = {
    key: string;
    title: string | null;
    diagramId: string | null;
    versionNo: number | null;
    stale: string | null;
    paragraphs: ProposalStatement[][];
  };
  const sections: Section[] = [];
  for (const statement of statements.slice().sort((a, b) => a.order_index - b.order_index)) {
    const key = `${statement.section ?? ""}|${statement.diagram_id ?? ""}`;
    let section = sections[sections.length - 1];
    if (!section || section.key !== key) {
      section = {
        key,
        title: statement.section ?? null,
        diagramId: statement.diagram_id ?? null,
        versionNo: statement.diagram_version_no ?? null,
        stale: statement.out_of_date ? (statement.out_of_date_reason ?? "changed") : null,
        paragraphs: [],
      };
      sections.push(section);
    }
    const last = section.paragraphs[section.paragraphs.length - 1];
    if (last && statement.paragraph_no != null && last[0].paragraph_no === statement.paragraph_no) {
      last.push(statement);
    } else {
      section.paragraphs.push([statement]);
    }
  }

  const figureProps = (key: string, defaultWidth: number, natural: PictureMeta | undefined) => {
    const layout = layoutOf(key, defaultWidth);
    const here = mode.key === key ? mode.mode : "view";
    const shared: FigureShared = {
      figureKey: key,
      natural:
        natural?.natural_width && natural.natural_height
          ? { w: natural.natural_width, h: natural.natural_height }
          : null,
      widthPct: layout.width,
      crop: layout.crop,
      caption: layout.caption,
      editable: canEdit,
      selected: selected === key,
      mode: here,
      canDelete: canEdit,
      onSelect: () => setSelected(key),
      onResize: (pct) => void saveLayout(key, { width_pct: pct }),
      onCropMode: () => setMode({ key, mode: "crop" }),
      onRegion: (rect) => {
        if (mode.mode === "pick") {
          setAsk((now) => (now ? { ...now, region: rect, hidden: false } : now));
        } else if (rect) {
          void saveLayout(key, { crop: rect });
        }
        setMode({ key: "", mode: "view" });
      },
      onCancelMode: () => {
        if (mode.mode === "pick") setAsk((now) => (now ? { ...now, hidden: false } : now));
        setMode({ key: "", mode: "view" });
      },
      onCaption: (text) => void saveLayout(key, { caption: text }),
      onResetCrop: () => void saveLayout(key, { crop: null }),
      onResetSize: () => void saveLayout(key, { width_pct: 100 }),
      onAsk: (at) => askAboutPicture(key, at),
      onDelete: () => {
        if (key.startsWith("u:")) void deletePicture(key);
        else setConfirmLeave({ id: key.slice(2), title: sectionOfKey(key)?.title ?? "this diagram" });
      },
      onContextMenu: (event) => figureMenu(key, event),
    };
    return shared;
  };

  const uploads = (metas ?? []).filter((m) => m.kind === "UPLOAD");
  let figure = 0;
  return (
    <article
      ref={root}
      className={`proposal${canEdit ? " editable" : ""}`}
      onMouseUp={onMouseUp}
      onContextMenu={onContextMenu}
      onClick={onClick}
      onDoubleClick={() => {
        if (clickTimer.current) window.clearTimeout(clickTimer.current);
      }}
      onDragOver={(event) => {
        if (canEdit && [...event.dataTransfer.types].includes("Files")) event.preventDefault();
      }}
      onDrop={(event) => {
        if (!canEdit) return;
        const files = [...event.dataTransfer.files].filter((f) => f.type.startsWith("image/"));
        if (files.length === 0) return;
        event.preventDefault();
        notePlace(event.target);
        for (const file of files) void addPicture(file, file.name, place.current ?? lastPlace());
      }}
    >
      <p className="proposal-legend">
        <span className="sent proposed_commitment">Underlined</span> sentences are promises the
        company would make and each needs an approver;{" "}
        <span className="sent assumption">dashed</span> sentences are assumptions.{" "}
        {canEdit
          ? "Click a sentence to edit it. Select words or right-click to have the AI reword just that part. Paste or drop a picture to add it; click a picture to resize, crop or ask the AI about it."
          : "Hover a sentence to see what it is."}
      </p>
      {problem && <div className="notice bad">{problem}</div>}
      {info && !problem && (
        <div className="notice good" onClick={() => setInfo(null)}>
          {info}
        </div>
      )}
      {confirmLeave && (
        <div className="notice bad">
          Leave “{confirmLeave.title}” out of the response? Its section, text and picture, is taken
          out. You can put it back from the panel above.{" "}
          <button type="button" onClick={() => void leaveDiagramOut(confirmLeave.id)}>
            Leave it out
          </button>{" "}
          <button type="button" className="ghost" onClick={() => setConfirmLeave(null)}>
            Keep it
          </button>
        </div>
      )}
      {sections.map((section) => {
        const number = section.diagramId ? (figure += 1) : 0;
        const mine = uploads.filter(
          (m) =>
            (m.section ?? "") === (section.title ?? "") &&
            (m.diagram_id ?? "") === (section.diagramId ?? ""),
        );
        const numbers = section.paragraphs.map((p, i) => p[0].paragraph_no ?? i + 1);
        const shown = (m: PictureMeta) => {
          const key = `u:${m.id}`;
          return (
            <UploadFigure
              key={key}
              artifactId={artifactId as string}
              imageId={m.id}
              {...figureProps(key, 80, m)}
            />
          );
        };
        return (
          <section
            id={section.diagramId ? `sec-${section.diagramId}` : undefined}
            data-section={section.title ?? ""}
            className={`proposal-section${section.stale ? " stale" : ""}${section.title && heldSections[section.title] ? " held" : ""}`}
            key={section.key}
          >
            {section.title && <h3>{section.title}</h3>}
            {section.title && heldSections[section.title] && (
              <span className="badge hold">
                Waiting on {heldSections[section.title]} — fix this part when they reply
              </span>
            )}
            {section.stale && <span className="badge warn">out of date — {section.stale}</span>}
            {section.diagramId && section.versionNo != null && (
              <DiagramFigure
                diagramId={section.diagramId}
                versionNo={section.versionNo}
                label={`Figure ${number}. ${section.title ?? ""}`}
                {...figureProps(`d:${section.diagramId}`, 100, metaFor(`d:${section.diagramId}`))}
              />
            )}
            {section.paragraphs.map((paragraph, index) => {
              const beingEdited = paragraph.find((x) => x.id === editing?.id);
              return (
                <Fragment key={paragraph[0].id}>
                  <p
                    data-pno={numbers[index]}
                    data-sec={section.title ?? ""}
                    data-did={section.diagramId ?? ""}
                  >
                    {paragraph.map((sentence) => {
                      const waitingOn = (sentence.requirement_codes ?? [])
                        .map((c) => heldCodes[c])
                        .find(Boolean);
                      const cls = `sent ${sentence.statement_class.toLowerCase()}${waitingOn ? " held" : ""}`;
                      const text = typed[sentence.id] ?? sentence.text;
                      return (
                        <Fragment key={sentence.id}>
                          {editing?.id === sentence.id ? (
                            <EditableSentence
                              key={`edit-${sentence.id}`}
                              text={sentence.text}
                              caret={editing.caret}
                              className={cls}
                              onSave={(value) => void save(sentence.id, value)}
                              onCancel={() => setEditing(null)}
                            />
                          ) : (
                            <span
                              id={`st-${sentence.id}`}
                              data-sid={sentence.id}
                              className={cls}
                              title={`${SENTENCE_TITLE[sentence.statement_class] ?? sentence.statement_class}${sentence.requirement_codes?.length ? ` · answers ${sentence.requirement_codes.join(", ")}` : ""}${waitingOn ? ` · waiting on ${waitingOn}: fix when they reply` : ""}`}
                            >
                              {text}
                            </span>
                          )}{" "}
                        </Fragment>
                      );
                    })}
                  </p>
                  {beingEdited && BACKED_CLASSES.includes(beingEdited.statement_class) && (
                    <p className="edit-hint">
                      This sentence is backed by{" "}
                      {beingEdited.statement_class === "TENDER_DERIVED"
                        ? "the tender"
                        : "approved evidence"}
                      . Once you change its wording it counts as your own wording, with no
                      citation.
                    </p>
                  )}
                  {mine.filter((m) => m.after_paragraph === numbers[index]).map(shown)}
                </Fragment>
              );
            })}
            {mine.filter((m) => !numbers.includes(m.after_paragraph)).map(shown)}
            {section.title && (
              <small className="muted-line">
                Not right? Tell the chat what to change in this part.
              </small>
            )}
          </section>
        );
      })}
      {menu && <DocMenu at={menu.at} items={menu.items} onClose={() => setMenu(null)} />}
      {popup && artifactId && (
        <RewritePopup
          key={`${popup.target.anchorId}-${popup.target.label}-${popup.at.x}-${popup.at.y}-${popup.stage ?? ""}`}
          at={popup.at}
          target={popup.target}
          alternative={popup.alternative}
          initialStage={popup.stage}
          onTargetChange={setLit}
          artifactId={artifactId}
          onClose={() => {
            setPopup(null);
            setLit(null);
            window.getSelection()?.removeAllRanges();
          }}
          onChanged={changed}
          onEditByHand={(id) => {
            setPopup(null);
            setLit(null);
            window.getSelection()?.removeAllRanges();
            const text = statements.find((x) => x.id === id)?.text ?? "";
            setEditing({ id, caret: text.length });
          }}
        />
      )}
      {ask && !ask.hidden && artifactId && (
        <PictureAskPopup
          key={ask.key}
          at={ask.at}
          target={ask.target}
          artifactId={artifactId}
          region={ask.region}
          onPickPart={() => {
            setAsk((now) => (now ? { ...now, hidden: true } : now));
            setSelected(ask.key);
            setMode({ key: ask.key, mode: "pick" });
          }}
          onClearPart={() => setAsk((now) => (now ? { ...now, region: null } : now))}
          onClose={() => setAsk(null)}
          onChanged={changed}
          onCaption={(text) => saveLayout(ask.key, { caption: text })}
        />
      )}
    </article>
  );
}
