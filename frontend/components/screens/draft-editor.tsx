"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * Edit a response one statement at a time.
 *
 * The backend saves a complete new version and has always accepted edits; no
 * screen let a person make one. Changing a sentence, adding a promise or an
 * assumption, and removing a statement all happen here, and every save carries
 * a reason, because the reviewer reads the reason next to the change.
 *
 * What an edit cannot do is give a statement more standing than it has. A fact
 * keeps the evidence it cites, a tender statement keeps its clause, and a
 * promise keeps the role that must approve it. New statements can only be
 * written as a person's own input, an assumption, or a promise -- never as an
 * evidence-backed fact, because a typed sentence is not evidence.
 */

export type EditableStatement = {
  id: string;
  order_index: number;
  statement_class: string;
  text: string;
  evidence_ids: string[] | null;
  source_locator: Record<string, unknown> | null;
  required_approver_role?: string | null;
  section?: string | null;
  diagram_id?: string | null;
  diagram_version_no?: number | null;
  paragraph_no?: number | null;
  requirement_codes?: string[] | null;
};

type Row = {
  key: string;
  statement_class: string;
  text: string;
  evidence_ids: string[];
  source_locator: Record<string, unknown> | null;
  required_approver_role: string | null;
  isNew: boolean;
  // A write-up statement stays in its section through an edit.
  section?: string | null;
  diagram_id?: string | null;
  diagram_version_no?: number | null;
  paragraph_no?: number | null;
  requirement_codes?: string[] | null;
};

const NEW_CLASSES: [string, string][] = [
  ["HUMAN_PROVIDED_INPUT", "My own statement"],
  ["ASSUMPTION", "Assumption (true only if the customer agrees)"],
  ["PROPOSED_COMMITMENT", "Promise the company would make"],
];

const LABEL: Record<string, string> = {
  EVIDENCE_BACKED_FACT: "Fact — keeps its evidence",
  TENDER_DERIVED: "From the tender — keeps its clause",
  PROPOSED_COMMITMENT: "Promise — needs approval",
  ASSUMPTION: "Assumption",
  HUMAN_PROVIDED_INPUT: "Written by a person",
  DIAGRAM_DESCRIPTION: "Describes the diagram — keeps its section",
};

const APPROVERS = [
  "BID_MANAGER",
  "SALES",
  "TECHNICAL_CONTRIBUTOR",
  "SOLUTION_ARCHITECT",
  "PROJECT_MANAGER",
  "SECURITY",
  "HR",
  "COMMERCIAL",
  "FINANCE",
  "LEGAL",
  "EXECUTIVE_APPROVER",
];

let counter = 0;
const nextKey = () => `row-${counter++}`;

export function DraftEditor({
  artifactId,
  taskId,
  statements,
  savesAsNewVersion,
  action,
  onDone,
}: {
  artifactId: string;
  taskId?: string;
  statements: EditableStatement[];
  savesAsNewVersion: boolean;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [rows, setRows] = useState<Row[]>(() =>
    statements
      .slice()
      .sort((a, b) => a.order_index - b.order_index)
      .map((s) => ({
        key: nextKey(),
        statement_class: s.statement_class,
        text: s.text,
        evidence_ids: s.evidence_ids ?? [],
        source_locator: s.source_locator,
        required_approver_role: s.required_approver_role ?? null,
        isNew: false,
        section: s.section ?? null,
        diagram_id: s.diagram_id ?? null,
        diagram_version_no: s.diagram_version_no ?? null,
        paragraph_no: s.paragraph_no ?? null,
        requirement_codes: s.requirement_codes ?? null,
      })),
  );
  const [reason, setReason] = useState("");
  const [newClass, setNewClass] = useState("HUMAN_PROVIDED_INPUT");

  const update = (key: string, patch: Partial<Row>) =>
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  const move = (index: number, by: number) =>
    setRows((current) => {
      const target = index + by;
      if (target < 0 || target >= current.length) return current;
      const copy = current.slice();
      [copy[index], copy[target]] = [copy[target], copy[index]];
      return copy;
    });

  const blank = rows.some((row) => !row.text.trim());
  const promiseWithoutApprover = rows.some(
    (row) => row.statement_class === "PROPOSED_COMMITMENT" && !row.required_approver_role,
  );
  const canSave = rows.length > 0 && !blank && !promiseWithoutApprover && reason.trim().length > 0;

  async function save() {
    await action("Saving the edited response", async () => {
      await api(`/artifacts/${artifactId}/versions`, {
        method: "POST",
        body: JSON.stringify({
          change_reason: reason.trim(),
          statements: rows.map((row) => ({
            statement_class: row.statement_class,
            text: row.text.trim(),
            evidence_ids: row.evidence_ids,
            source_locator: row.source_locator,
            required_approver_role: row.required_approver_role,
            section: row.section ?? null,
            diagram_id: row.diagram_id ?? null,
            diagram_version_no: row.diagram_version_no ?? null,
            paragraph_no: row.paragraph_no ?? null,
            requirement_codes: row.requirement_codes ?? null,
          })),
        }),
      });
      await queryClient.invalidateQueries({ queryKey: ["versions", artifactId] });
      await queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] });
      onDone();
    });
  }

  return (
    <div className="context-body draft-editor">
      {savesAsNewVersion && (
        <div className="notice">
          This version is not a draft any more. Saving creates a new draft version and leaves the
          earlier one as it was.
        </div>
      )}
      {rows.map((row, index) => (
        <div className="statement" key={row.key}>
          <header>
            <span className={`tag ${row.statement_class.toLowerCase()}`}>
              {LABEL[row.statement_class] ?? row.statement_class}
            </span>
            <small className="spacer" />
            <button className="ghost" type="button" onClick={() => move(index, -1)} disabled={index === 0}>
              ↑
            </button>
            <button
              className="ghost"
              type="button"
              onClick={() => move(index, 1)}
              disabled={index === rows.length - 1}
            >
              ↓
            </button>
            <button
              className="ghost"
              type="button"
              onClick={() => setRows((current) => current.filter((r) => r.key !== row.key))}
            >
              Remove
            </button>
          </header>
          <textarea
            value={row.text}
            rows={Math.max(2, Math.ceil(row.text.length / 90))}
            onChange={(event) => update(row.key, { text: event.target.value })}
          />
          {row.statement_class === "EVIDENCE_BACKED_FACT" && (
            <small className="muted-line">
              {row.evidence_ids.length} evidence item(s) stay attached. Check your wording still
              says only what they show.
            </small>
          )}
          {row.statement_class === "PROPOSED_COMMITMENT" && (
            <label className="muted-line">
              Must be approved by{" "}
              <select
                value={row.required_approver_role ?? ""}
                onChange={(event) => update(row.key, { required_approver_role: event.target.value })}
              >
                <option value="">Choose a role…</option>
                {APPROVERS.map((role) => (
                  <option key={role} value={role}>
                    {role.replaceAll("_", " ").toLowerCase()}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      ))}

      <div className="panel-actions">
        <select value={newClass} onChange={(event) => setNewClass(event.target.value)}>
          {NEW_CLASSES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <button
          className="secondary"
          type="button"
          onClick={() =>
            setRows((current) => [
              ...current,
              {
                key: nextKey(),
                statement_class: newClass,
                text: "",
                evidence_ids: [],
                source_locator: null,
                required_approver_role: null,
                isNew: true,
              },
            ])
          }
        >
          Add statement
        </button>
      </div>

      <label className="muted-line">
        Why are you changing this? (the reviewer sees it)
        <input
          value={reason}
          placeholder="e.g. Corrected the testing duration to eight weeks"
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      {promiseWithoutApprover && (
        <small className="muted-line">Choose who must approve each promise.</small>
      )}
      <div className="panel-actions">
        <button type="button" disabled={!canSave} onClick={save}>
          Save edited response
        </button>
        <button className="ghost" type="button" onClick={onDone}>
          Cancel
        </button>
      </div>
    </div>
  );
}
