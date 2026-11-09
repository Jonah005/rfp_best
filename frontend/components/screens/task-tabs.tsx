"use client";

import { useState, type ReactNode } from "react";

/** What the chat is about: the whole task, some requirements, or one section. */
export type Focus =
  | { kind: "task" }
  | { kind: "requirements"; ids: string[]; label: string }
  | { kind: "section"; section: string }
  | { kind: "gaps" }
  | { kind: "gap"; id: string; label: string };

/** The key the server files a thread under -- must match `app/services/focus.py`. */
export function focusKey(focus: Focus): string {
  if (focus.kind === "requirements") return "req:" + [...focus.ids].sort().join(",");
  if (focus.kind === "section") return "sec:" + focus.section.slice(0, 280);
  if (focus.kind === "gaps") return "gaps";
  if (focus.kind === "gap") return `gap:${focus.id}`;
  return "task";
}

export function focusPayload(focus: Focus) {
  if (focus.kind === "requirements") return { kind: "requirements", requirement_ids: focus.ids };
  if (focus.kind === "section") return { kind: "section", section: focus.section };
  if (focus.kind === "gaps") return { kind: "gaps" };
  if (focus.kind === "gap") return { kind: "gap", gap_id: focus.id };
  return undefined;
}

export function focusLabel(focus: Focus): string {
  if (focus.kind === "requirements") return focus.label;
  if (focus.kind === "section") return `Section: ${focus.section}`;
  if (focus.kind === "gaps") return "The RFP missing parts";
  if (focus.kind === "gap") return focus.label;
  return "This task";
}

export type ReqRow = {
  requirement_id: string;
  code: string;
  text: string | null;
  bid_readiness: string | null;
  evidence_status: string | null;
  asked_departments?: string[];
  position_status?: string | null;
  position_text?: string | null;
  replies?: { request_id: string; department: string | null; state: string; text: string }[];
};

/** A requirement set aside or answered by a person, as the record shows it. */
export type Decision = {
  requirement_id: string;
  code: string;
  text: string;
  status: "submitted" | "ignored";
  answer: string | null;
  reason: string | null;
  decided_by: string | null;
  decided_at: string | null;
  ai_suggested: string[];
};

const HAVE = new Set(["CURRENT_VERIFIED", "HISTORICAL_VERIFIED", "PARTIAL"]);

/** green: approved company evidence; red: none; orange: asked another department. */
export function tone(r: ReqRow): "have" | "none" | "asked" | "theirs" | "reply" {
  if (r.position_status === "submitted") return "have";
  if (r.replies?.length) return "reply";
  if (r.asked_departments?.length) return "asked";
  if (r.bid_readiness === "CUSTOMER_OBLIGATION") return "theirs";
  if (r.evidence_status && HAVE.has(r.evidence_status)) return "have";
  return "none";
}

const TONE_NOTE: Record<string, (r: ReqRow) => string> = {
  have: (r) =>
    r.position_status === "submitted"
      ? `Our answer is submitted: ${(r.position_text ?? "").slice(0, 110)}`
      : r.evidence_status === "PARTIAL"
        ? "Partly covered by approved evidence"
        : "Approved evidence found",
  none: () => "No approved evidence yet",
  asked: (r) => `Asked ${r.asked_departments?.join(", ")} — waiting`,
  theirs: () => "The customer's own obligation",
  reply: (r) => `Answered — review: ${r.replies?.map((x) => x.department ?? "A department").join(", ")}`,
};

/** The task's requirements, to tick one or several and talk about them. */
export function RequirementsTab({
  rows,
  picked,
  focusedIds,
  decisions,
  onToggle,
  onClear,
  onChat,
  onAsk,
  onDraft,
  onRestore,
  onDraftFromReply,
  onReviewed,
  relatedCodes = [],
  renderVoids,
}: {
  relatedCodes?: string[];
  /** What the RFP leaves out about one requirement, shown under it. */
  renderVoids?: (code: string) => ReactNode;
  rows: ReqRow[];
  picked: Set<string>;
  focusedIds: string[];
  decisions: Decision[];
  onToggle: (id: string) => void;
  onClear: () => void;
  onChat: () => void;
  onAsk: () => void;
  onDraft: () => void;
  onRestore: (requirementId: string) => void;
  onDraftFromReply: (row: ReqRow, text: string) => void;
  onReviewed: (requestIds: string[]) => void;
}) {
  const [showAside, setShowAside] = useState(false);
  const aside = decisions.filter((d) => d.status === "ignored");
  // Set-aside requirements leave the working list; the table below keeps them.
  const shown = rows.filter((r) => r.position_status !== "ignored");
  if (!rows.length) return <p className="muted-line">No requirements attached to this task.</p>;
  return (
    <div className="req-tab">
      <p className="req-legend">
        <span className="dot have" /> evidence found <span className="dot none" /> none yet{" "}
        <span className="dot asked" /> asked a department <span className="dot reply" /> answered — review
      </p>
      {picked.size > 0 && (
        <div className="select-bar" role="toolbar" aria-label="Selected requirements">
          <strong>{picked.size} selected</strong>
          <button onClick={onChat}>Chat about {picked.size === 1 ? "this" : "these"}</button>
          <button onClick={onDraft}>Draft an answer</button>
          <button className="ghost" onClick={onAsk}>
            Ask another department
          </button>
          <button className="ghost" onClick={onClear}>
            Clear
          </button>
        </div>
      )}
      <ul className="pick-list">
        {shown.map((r) => {
          const on = picked.has(r.requirement_id);
          const t = tone(r);
          return (
            <li
              key={r.requirement_id}
              className={`pick ${t}${on ? " on" : ""}${focusedIds.includes(r.requirement_id) ? " talking" : ""}${relatedCodes.includes(r.code) ? " related" : ""}`}
              onClick={() => onToggle(r.requirement_id)}
            >
              <input
                type="checkbox"
                checked={on}
                aria-label={`Select ${r.code}`}
                onChange={() => onToggle(r.requirement_id)}
                onClick={(e) => e.stopPropagation()}
              />
              <div>
                <strong>{r.code}</strong>
                {relatedCodes.includes(r.code) && (
                  <small className="related-tag">the missing part you are chatting about affects this</small>
                )}
                <span>{r.text ?? "—"}</span>
                <em>{TONE_NOTE[t](r)}</em>
                {r.replies?.map((reply) => (
                  <div key={reply.request_id} className="reply-box" onClick={(e) => e.stopPropagation()}>
                    <p>
                      <strong>{reply.department ?? "A department"}:</strong> {reply.text}
                    </p>
                    <div className="reply-actions">
                      <button onClick={() => onDraftFromReply(r, reply.text)}>Draft from this reply</button>
                      <button className="ghost" onClick={() => onReviewed([reply.request_id])}>
                        Dealt with
                      </button>
                    </div>
                  </div>
                ))}
                {renderVoids?.(r.code)}
              </div>
            </li>
          );
        })}
      </ul>
      {aside.length > 0 && (
        <div className="aside">
          <button className="ghost" onClick={() => setShowAside((v) => !v)}>
            {showAside ? "Hide" : "Show"} set aside ({aside.length})
          </button>
          {showAside && (
            <table className="aside-table">
              <thead>
                <tr>
                  <th>Requirement</th>
                  <th>Why</th>
                  <th>Set aside by</th>
                  <th>AI had suggested</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {aside.map((d) => (
                  <tr key={d.requirement_id}>
                    <td>
                      <strong>{d.code}</strong> {d.text.slice(0, 120)}
                    </td>
                    <td>{d.reason || "—"}</td>
                    <td>
                      {d.decided_by ?? "—"}
                      {d.decided_at ? ` · ${new Date(d.decided_at).toLocaleDateString()}` : ""}
                    </td>
                    <td>{d.ai_suggested.length ? d.ai_suggested.join("; ") : "—"}</td>
                    <td>
                      <button className="ghost" onClick={() => onRestore(d.requirement_id)}>
                        Restore
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

export type SectionStatement = {
  id: string;
  order_index: number;
  section?: string | null;
  text: string;
  out_of_date?: boolean;
};

/** The sections of the written response, to open one and talk about it. */
export function SectionsTab({
  statements,
  writing,
  active,
  onPick,
  onGenerate,
}: {
  statements: SectionStatement[];
  writing: { done: number; total: number; current?: string } | null;
  active: string | null;
  onPick: (section: string) => void;
  onGenerate: () => void;
}) {
  const [sure, setSure] = useState(false);
  const sections: { title: string; count: number; first: string; stale: boolean }[] = [];
  for (const s of [...statements].sort((a, b) => a.order_index - b.order_index)) {
    if (!s.section) continue;
    const last = sections[sections.length - 1];
    if (last && last.title === s.section) {
      last.count += 1;
      last.stale = last.stale || Boolean(s.out_of_date);
    } else if (!sections.some((x) => x.title === s.section)) {
      sections.push({ title: s.section, count: 1, first: s.text, stale: Boolean(s.out_of_date) });
    }
  }
  const has = sections.length > 0;
  return (
    <div className="sections-tab">
      <div className="sections-bar">
        <p className="muted-line">
          {writing
            ? `Writing section ${Math.min(writing.done + 1, writing.total)} of ${writing.total}${writing.current ? `: ${writing.current}` : ""}…`
            : has
              ? "Open a section to talk about it. Its text opens on the right."
              : "Nothing has been written yet."}
        </p>
        {sure ? (
          <span className="sections-confirm">
            This writes every section again and replaces any edits.
            <button
              onClick={() => {
                setSure(false);
                onGenerate();
              }}
            >
              Write again
            </button>
            <button className="ghost" onClick={() => setSure(false)}>
              Cancel
            </button>
          </span>
        ) : (
          <button disabled={Boolean(writing)} onClick={() => (has ? setSure(true) : onGenerate())}>
            {has ? "Regenerate" : "Generate"}
          </button>
        )}
      </div>
      {!has && !writing && (
        <p className="muted-line">
          Press Generate to write the response from this task&apos;s requirements and diagrams.
          Your earlier chats stay where they are.
        </p>
      )}
      <ul className="pick-list">
        {sections.map((s) => (
          <li
            key={s.title}
            className={`pick section${active === s.title ? " talking" : ""}`}
            onClick={() => onPick(s.title)}
          >
            <div>
              <strong>{s.title}</strong>
              <span>{s.first.slice(0, 140)}</span>
              <em>
                {s.count} statement{s.count === 1 ? "" : "s"}
                {s.stale ? " · out of date" : ""}
              </em>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
