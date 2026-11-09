"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

export type RewriteTarget = {
  // Each piece is a sentence, or part of one (start/end are characters of its text).
  items: { statement_id: string; start?: number; end?: number }[];
  label: string; // "word", "sentence", "paragraph (4 sentences)" ...
  paragraph: boolean;
  anchorId: string; // the sentence the menu was opened on
  menu: boolean; // opened by right-click: offer edit/add/delete too
};

type Suggestion = {
  statement_id: string;
  before: string;
  after: string;
  full_after: string;
  becomes_own_wording: boolean;
};

const MODES: { mode: string; label: string }[] = [
  { mode: "rewrite", label: "Rewrite" },
  { mode: "simpler", label: "Simpler" },
  { mode: "shorter", label: "Shorter" },
  { mode: "formal", label: "More formal" },
];

/**
 * What appears when a person selects a piece of the draft, or right-clicks it.
 *
 * The model is shown only the chosen piece and the sentences beside it. What it
 * hands back is a suggestion beside the original; nothing changes until the
 * person accepts it.
 */
export function RewritePopup({
  at,
  target: first,
  alternative,
  initialStage,
  onTargetChange,
  artifactId,
  onClose,
  onChanged,
  onEditByHand,
}: {
  at: { x: number; y: number };
  target: RewriteTarget;
  // What the whole paragraph would be, offered when the menu opened on one sentence.
  alternative?: RewriteTarget;
  initialStage?: "add" | "delete";
  onTargetChange?: (items: RewriteTarget["items"]) => void;
  artifactId: string;
  onClose: () => void;
  onChanged: () => void;
  onEditByHand: (statementId: string) => void;
}) {
  const [target, setTarget] = useState(first);
  const [stage, setStage] = useState<"menu" | "loading" | "suggest" | "add" | "delete">(
    initialStage ?? "menu",
  );
  const [followUp, setFollowUp] = useState("");
  const [custom, setCustom] = useState("");
  const [added, setAdded] = useState("");
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [last, setLast] = useState<{ mode: string; instruction?: string } | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const [left, setLeft] = useState(at.x);
  const [top, setTop] = useState(at.y);

  useEffect(() => {
    const width = box.current?.offsetWidth ?? 320;
    const height = box.current?.offsetHeight ?? 200;
    setLeft(Math.max(8, Math.min(at.x, window.innerWidth - width - 8)));
    setTop(Math.max(8, Math.min(at.y, window.innerHeight - height - 8)));
  }, [at.x, at.y, stage, suggestions.length]);

  // Tell the document which words are being reworded, so they stay marked.
  useEffect(() => {
    onTargetChange?.(target.items);
  }, [target, onTargetChange]);

  useEffect(() => {
    const away = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", away);
    return () => window.removeEventListener("keydown", away);
  }, [onClose]);

  // `again` asks for something different from what is showing now.
  const ask = async (mode: string, instruction?: string, again = false) => {
    setStage("loading");
    setError(null);
    const before = again ? suggestions.map((s) => s.after) : [];
    setLast({ mode, instruction });
    try {
      const result = await api<{ suggestions: Suggestion[] }>(
        `/artifacts/${artifactId}/statements/rewrite-preview`,
        {
          method: "POST",
          body: JSON.stringify({
            items: target.items,
            mode,
            instruction: instruction || null,
            paragraph: target.paragraph,
            previous: before,
          }),
        },
      );
      setSuggestions(result.suggestions);
      setStage("suggest");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "That did not work.");
      setStage("menu");
    }
  };

  const send = async (path: string, init: RequestInit) => {
    setError(null);
    try {
      await api(path, init);
      onChanged();
      onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "That did not work.");
    }
  };

  const accept = () =>
    send(`/artifacts/${artifactId}/statements/edits`, {
      method: "POST",
      body: JSON.stringify({
        edits: suggestions.map((s) => ({ statement_id: s.statement_id, text: s.full_after })),
      }),
    });

  return (
    <div
      ref={box}
      className="rewrite-popup"
      style={{ top, left, maxHeight: Math.min(560, window.innerHeight - 16) }}
      role="dialog"
      aria-label="Reword this part"
      // Keep the selection in the document while the popup is used.
      onMouseDown={(event) => {
        if ((event.target as HTMLElement).tagName !== "TEXTAREA" && (event.target as HTMLElement).tagName !== "INPUT")
          event.preventDefault();
      }}
      onContextMenu={(event) => event.preventDefault()}
    >
      {stage === "menu" && (
        <>
          <div className="rw-head">
            <strong>Reword this {target.label}</strong>
            <button type="button" className="rw-x" onClick={onClose} aria-label="Close">
              ×
            </button>
          </div>
          <div className="rw-modes">
            {MODES.map((m) => (
              <button key={m.mode} type="button" className="ghost" onClick={() => void ask(m.mode)}>
                {m.label}
              </button>
            ))}
          </div>
          <form
            className="rw-custom"
            onSubmit={(event) => {
              event.preventDefault();
              if (custom.trim()) void ask("custom", custom.trim());
            }}
          >
            <input
              value={custom}
              onChange={(event) => setCustom(event.target.value)}
              placeholder="Or say what to change…"
              aria-label="Say what to change"
            />
            <button type="submit" disabled={!custom.trim()}>
              Go
            </button>
          </form>
          {target.menu && (
            <div className="rw-more">
              <button type="button" className="ghost" onClick={() => onEditByHand(target.anchorId)}>
                Edit by hand
              </button>
              <button type="button" className="ghost" onClick={() => setStage("add")}>
                Add a sentence after
              </button>
              <button type="button" className="ghost" onClick={() => setStage("delete")}>
                Delete sentence
              </button>
            </div>
          )}
          {alternative && target !== alternative && (
            <button type="button" className="ghost" onClick={() => setTarget(alternative)}>
              Reword the whole paragraph instead
            </button>
          )}
          {error && <p className="rw-error">{error}</p>}
          <small className="rw-note">
            The AI sees only this {target.label} and the sentences next to it.
          </small>
        </>
      )}
      {stage === "loading" && (
        <div className="rw-loading" role="status">
          <span className="dots" aria-hidden="true" /> Rewording…
        </div>
      )}
      {stage === "suggest" && (
        <>
          <div className="rw-head">
            <strong>New wording</strong>
            <button type="button" className="rw-x" onClick={onClose} aria-label="Close">
              ×
            </button>
          </div>
          <div className="rw-pairs">
            {suggestions.map((s) => (
              <p key={s.statement_id} className="rw-after">
                {s.after}
              </p>
            ))}
          </div>
          {suggestions.some((s) => s.becomes_own_wording) && (
            <small className="rw-note">
              This sentence was backed by the tender or by evidence. Once the wording changes it
              counts as your own wording, with no citation.
            </small>
          )}
          {error && <p className="rw-error">{error}</p>}
          <div className="rw-actions">
            <button type="button" className="rw-apply" onClick={() => void accept()}>
              Apply
            </button>
            <button
              type="button"
              className="ghost"
              onClick={() => last && void ask(last.mode, last.instruction, true)}
            >
              Try again
            </button>
            <button type="button" className="ghost" onClick={() => setStage("menu")}>
              Back
            </button>
          </div>
          <form
            className="rw-custom"
            onSubmit={(event) => {
              event.preventDefault();
              if (followUp.trim()) {
                void ask("custom", followUp.trim(), true);
                setFollowUp("");
              }
            }}
          >
            <input
              value={followUp}
              onChange={(event) => setFollowUp(event.target.value)}
              placeholder="Not quite? Tell it what to change…"
              aria-label="Tell it what to change"
            />
            <button type="submit" disabled={!followUp.trim()}>
              Go
            </button>
          </form>
        </>
      )}
      {stage === "add" && (
        <>
          <div className="rw-head">
            <strong>Add a sentence after this one</strong>
            <button type="button" className="rw-x" onClick={onClose} aria-label="Close">
              ×
            </button>
          </div>
          <textarea
            autoFocus
            value={added}
            onChange={(event) => setAdded(event.target.value)}
            rows={3}
            aria-label="The new sentence"
          />
          {error && <p className="rw-error">{error}</p>}
          <div className="rw-actions">
            <button
              type="button"
              disabled={!added.trim()}
              onClick={() =>
                void send(`/artifacts/${artifactId}/statements/${target.anchorId}/add-after`, {
                  method: "POST",
                  body: JSON.stringify({ text: added.trim() }),
                })
              }
            >
              Add
            </button>
            <button type="button" className="ghost" onClick={() => setStage("menu")}>
              Back
            </button>
          </div>
        </>
      )}
      {stage === "delete" && (
        <>
          <div className="rw-head">
            <strong>Delete this sentence?</strong>
          </div>
          <small className="rw-note">
            If it was a promise or an assumption, that promise or assumption is withdrawn too.
          </small>
          {error && <p className="rw-error">{error}</p>}
          <div className="rw-actions">
            <button
              type="button"
              onClick={() =>
                void send(`/artifacts/${artifactId}/statements/${target.anchorId}`, {
                  method: "DELETE",
                })
              }
            >
              Delete
            </button>
            <button type="button" className="ghost" onClick={() => setStage("menu")}>
              Keep it
            </button>
          </div>
        </>
      )}
    </div>
  );
}


export type PictureTarget = {
  kind: "upload" | "figure";
  imageId?: string;
  diagramId?: string;
  label: string;
  // The paragraph the picture follows; a new paragraph can be added after it.
  anchorStatementId?: string;
  // Where "add as a paragraph" puts it: under the picture, or at the top of the
  // section for a diagram (which sits above the section's first paragraph).
  placement?: "after" | "start";
};

const QUICK = ["Explain this picture", "Write a caption", "Explain it for a non-technical reader"];

/**
 * The small bar for talking to the AI about a picture.
 *
 * A pasted picture is sent to a vision model outside the platform, so the bar says so
 * and asking is the person's consent for that picture. A drawn diagram is not a
 * picture to the model: it is told the diagram's parts and links as text.
 */
export function PictureAskPopup({
  at,
  target,
  artifactId,
  region,
  onPickPart,
  onClearPart,
  onClose,
  onChanged,
  onCaption,
}: {
  at: { x: number; y: number };
  target: PictureTarget;
  artifactId: string;
  region: { x: number; y: number; w: number; h: number } | null;
  onPickPart: () => void;
  onClearPart: () => void;
  onClose: () => void;
  onChanged: () => void;
  onCaption: (text: string) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [answer, setAnswer] = useState<{ text: string; unclear: string[] } | null>(null);
  const [history, setHistory] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const [left, setLeft] = useState(at.x);

  useEffect(() => {
    const width = box.current?.offsetWidth ?? 360;
    setLeft(Math.max(8, Math.min(at.x, window.innerWidth - width - 8)));
  }, [at.x, answer]);

  useEffect(() => {
    const away = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", away);
    return () => window.removeEventListener("keydown", away);
  }, [onClose]);

  const ask = async (instruction: string, refine: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const path =
        target.kind === "upload"
          ? `/artifacts/${artifactId}/images/${target.imageId}/explain`
          : `/artifacts/${artifactId}/figures/${target.diagramId}/explain`;
      const result = await api<{ text: string; unclear: string[] }>(path, {
        method: "POST",
        body: JSON.stringify({
          instruction,
          previous: refine ? history : [],
          region,
          use_vision: target.kind === "upload",
        }),
      });
      setAnswer(result);
      setHistory((now) => (refine ? [...now, result.text] : [result.text]));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      ref={box}
      className="rewrite-popup picture-ask"
      style={{ top: at.y, left, maxHeight: Math.max(260, window.innerHeight - at.y - 12) }}
      role="dialog"
      aria-label="Ask the AI about this picture"
      onMouseDown={(event) => {
        const tag = (event.target as HTMLElement).tagName;
        if (tag !== "TEXTAREA" && tag !== "INPUT") event.preventDefault();
      }}
      onContextMenu={(event) => event.preventDefault()}
    >
      <div className="rw-head">
        <strong>Ask AI about {target.label}</strong>
        <button type="button" className="rw-x" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      {!answer && (
        <>
          <div className="rw-modes">
            {QUICK.map((q) => (
              <button key={q} type="button" className="ghost" disabled={busy} onClick={() => void ask(q, false)}>
                {q}
              </button>
            ))}
          </div>
          {target.kind === "upload" && (
            <div className="rw-more">
              <button type="button" className="ghost" onClick={onPickPart}>
                {region ? "Pick a different part" : "Point at a part"}
              </button>
              {region && (
                <button type="button" className="ghost" onClick={onClearPart}>
                  Use the whole picture
                </button>
              )}
              <small className="rw-note">
                {region ? "Only the part you pointed at is used." : "Or let it look at the whole picture."}
              </small>
            </div>
          )}
        </>
      )}
      {answer && (
        <div className="rw-pairs">
          <p className="rw-after">{answer.text}</p>
          {answer.unclear.length > 0 && (
            <small className="rw-note">Could not make out: {answer.unclear.join("; ")}</small>
          )}
        </div>
      )}
      <form
        className="rw-custom"
        onSubmit={(event) => {
          event.preventDefault();
          if (text.trim() && !busy) {
            void ask(text.trim(), Boolean(answer));
            setText("");
          }
        }}
      >
        <input
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={
            answer ? "Change it, or ask more…" : "Say what you want — e.g. explain the left box"
          }
          aria-label="Tell the AI what you want"
        />
        <button type="submit" disabled={!text.trim() || busy}>
          {busy ? "…" : "Ask"}
        </button>
      </form>
      {busy && (
        <div className="rw-loading" role="status">
          <span className="dots" aria-hidden="true" /> Looking…
        </div>
      )}
      {answer && (
        <div className="rw-actions">
          {target.anchorStatementId && (
            <button
              type="button"
              onClick={async () => {
                try {
                  await api(`/artifacts/${artifactId}/statements/${target.anchorStatementId}/add-after`, {
                    method: "POST",
                    body: JSON.stringify({
                      text: answer.text,
                      new_paragraph: true,
                      before: target.placement === "start",
                    }),
                  });
                  onChanged();
                  onClose();
                } catch (failure) {
                  setError(failure instanceof Error ? failure.message : "That did not work.");
                }
              }}
            >
              Add to the text
            </button>
          )}
          {answer.text.length <= 400 && (
            <button
              type="button"
              className="ghost"
              onClick={() => void onCaption(answer.text).then(() => onClose())}
            >
              Use as caption
            </button>
          )}
          <button
            type="button"
            className="ghost"
            onClick={() => void navigator.clipboard?.writeText(answer.text)}
          >
            Copy
          </button>
        </div>
      )}
      {error && <p className="rw-error">{error}</p>}
      <small className="rw-note">
        {target.kind === "upload"
          ? "Asking sends this picture (or the part you pointed at) to a vision model outside the platform."
          : "The AI is given this diagram's parts and links as text, and the paragraph above it."}
      </small>
    </div>
  );
}
