"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, API_URL, token } from "@/lib/api";

/**
 * Panels the walkthrough needs that sit inside existing screens:
 * evidence uploaded from inside a task, a task's history, the bid dashboard's
 * separate measures, the customer letter, and the cross-section check.
 */

type EvidenceRow = {
  document_version_id: string;
  filename: string;
  parse_state: string;
  excerpts: number;
  approved: number;
  awaiting_approval: number;
  usable_in_draft: boolean;
};

export function TaskEvidence({ taskId, onRedraft }: { taskId: string; onRedraft?: () => void }) {
  const client = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const rows = useQuery({
    queryKey: ["task-evidence", taskId],
    queryFn: () => api<EvidenceRow[]>(`/tasks/${taskId}/evidence`),
    enabled: Boolean(taskId),
    refetchInterval: 10000,
  });

  async function upload() {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      await api(`/tasks/${taskId}/evidence`, { method: "POST", body: form });
      setFile(null);
      await client.invalidateQueries({ queryKey: ["task-evidence", taskId] });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  }

  const usable = (rows.data ?? []).some((r) => r.usable_in_draft);
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">EVIDENCE FOR THIS TASK</p>
          <h2>Upload supporting material</h2>
        </div>
      </div>
      <p className="muted">
        Uploads become company knowledge. They are used in a draft only after a knowledge administrator approves
        them; you will be notified when that happens.
      </p>
      <div className="row">
        <input type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <button disabled={!file || busy} onClick={upload}>
          {busy ? "Uploading…" : "Upload"}
        </button>
        {usable && onRedraft && <button onClick={onRedraft}>Redraft with approved evidence</button>}
      </div>
      {error && <p className="error">{error}</p>}
      {(rows.data ?? []).length > 0 && (
        <table>
          <thead>
            <tr>
              <th>File</th>
              <th>Parsing</th>
              <th>Excerpts</th>
              <th>Approved</th>
              <th>Usable in draft</th>
            </tr>
          </thead>
          <tbody>
            {rows.data!.map((r) => (
              <tr key={r.document_version_id}>
                <td>{r.filename}</td>
                <td>{r.parse_state}</td>
                <td>{r.excerpts}</td>
                <td>
                  {r.approved} / {r.excerpts}
                </td>
                <td>{r.usable_in_draft ? "Yes" : "Awaiting approval"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

type Event = { at: string; who: string; action: string; entity_type: string; reason: string | null };

export function TaskTimeline({ taskId }: { taskId: string }) {
  const [open, setOpen] = useState(false);
  const events = useQuery({
    queryKey: ["task-timeline", taskId],
    queryFn: () => api<Event[]>(`/tasks/${taskId}/timeline`),
    enabled: Boolean(taskId && open),
  });
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">AUDIT</p>
          <h2>Task history</h2>
        </div>
        <button onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"}</button>
      </div>
      {open && (
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Who</th>
              <th>What</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {(events.data ?? []).map((e, i) => (
              <tr key={i}>
                <td>{new Date(e.at).toLocaleString()}</td>
                <td>{e.who}</td>
                <td>
                  {e.action} <span className="muted">({e.entity_type})</span>
                </td>
                <td>{e.reason ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

const LABELS: Record<string, string> = {
  requirement_coverage_pct: "Requirement coverage",
  evidence_readiness_pct: "Evidence readiness",
  task_completion_pct: "Task completion",
  approvals_pct: "Approvals",
  package_readiness_pct: "Package readiness",
};

export function BidDashboard({ bidId }: { bidId: string }) {
  const board = useQuery({
    queryKey: ["bid-dashboard", bidId],
    queryFn: () => api<Record<string, unknown>>(`/bids/${bidId}/dashboard`),
    enabled: Boolean(bidId),
  });
  const data = board.data ?? {};
  const pct = Object.keys(data).filter((k) => k.endsWith("_pct"));
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">BID DASHBOARD</p>
          <h2>Each measure on its own</h2>
        </div>
      </div>
      <div className="metrics">
        {pct.map((k) => (
          <div className="metric" key={k}>
            <small>{LABELS[k] ?? k.replace(/_pct$/, "").replace(/_/g, " ")}</small>
            <strong>{data[k] == null ? "—" : `${data[k]}%`}</strong>
          </div>
        ))}
        {"tasks_waiting_on_departments" in data && (
          <div className="metric">
            <small>Tasks waiting on departments</small>
            <strong>{String(data.tasks_waiting_on_departments)}</strong>
          </div>
        )}
        {"package_open_findings" in data && (
          <div className="metric">
            <small>Open release findings</small>
            <strong>{String(data.package_open_findings ?? "—")}</strong>
          </div>
        )}
      </div>
    </section>
  );
}

async function download(path: string, method: "GET" | "POST", filename: string): Promise<string | null> {
  const headers = new Headers();
  const access = token();
  if (access) headers.set("Authorization", `Bearer ${access}`);
  const response = await fetch(`${API_URL}${path}`, { method, headers, cache: "no-store" });
  if (!response.ok) throw new Error(`Request failed (${response.status}).`);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
  return response.headers.get("X-Question-Count");
}

export function ClarificationLetter({ bidId }: { bidId: string }) {
  const client = useQueryClient();
  const [message, setMessage] = useState("");
  async function run(send: boolean) {
    setMessage("");
    try {
      const count = await download(
        `/bids/${bidId}/clarification-letter${send ? "/send" : ""}`,
        send ? "POST" : "GET",
        "clarification-request.docx",
      );
      setMessage(send ? `Letter downloaded; ${count ?? 0} questions marked as sent to the customer.` : `Letter downloaded with ${count ?? 0} questions.`);
      if (send) await client.invalidateQueries({ queryKey: ["clarifications", bidId] });
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Download failed.");
    }
  }
  return (
    <div className="row">
      <button onClick={() => run(false)}>Download clarification letter</button>
      <button onClick={() => run(true)}>Download and mark questions sent</button>
      {message && <span className="muted">{message}</span>}
    </div>
  );
}

export function ConsistencyCheck({ bidId }: { bidId: string }) {
  const [result, setResult] = useState("");
  const [busy, setBusy] = useState(false);
  async function run() {
    setBusy(true);
    try {
      const r = await api<{ conflicts: number; tasks_opened: number }>(`/bids/${bidId}/consistency-check`, {
        method: "POST",
      });
      setResult(`${r.conflicts} conflicts found; ${r.tasks_opened} resolution tasks opened.`);
    } catch (e) {
      setResult(e instanceof Error ? e.message : "Check failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="row">
      <button disabled={busy} onClick={run}>
        {busy ? "Checking…" : "Check approved answers for contradictions"}
      </button>
      {result && <span className="muted">{result}</span>}
    </div>
  );
}

type DepartmentRow = { id: string; code: string; name: string; responsibilities: string | null };

/**
 * What each department answers for. Read by the routing call that decides who
 * a missing piece of information should be requested from. Administrators
 * only: the list endpoint needs org:read, so everyone else sees nothing here.
 */
export function DepartmentResponsibilities() {
  const client = useQueryClient();
  const rows = useQuery({
    queryKey: ["admin-departments"],
    queryFn: () => api<DepartmentRow[]>("/admin/departments"),
    retry: false,
  });
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  // Per-row feedback beside its own button. The first version printed one
  // "saved" line under the whole table, off-screen, and the list reordered
  // after each save -- so a save looked like nothing happened.
  const [status, setStatus] = useState<Record<string, string>>({});
  if (rows.isError || !rows.data?.length) return null;
  const sorted = [...rows.data].sort((a, b) => a.name.localeCompare(b.name));

  async function save(row: DepartmentRow) {
    setStatus((s) => ({ ...s, [row.id]: "Saving…" }));
    try {
      await api(`/admin/departments/${row.id}/responsibilities`, {
        method: "PATCH",
        body: JSON.stringify({ responsibilities: drafts[row.id] ?? row.responsibilities ?? "" }),
      });
      setDrafts((d) => {
        const next = { ...d };
        delete next[row.id];
        return next;
      });
      await client.invalidateQueries({ queryKey: ["admin-departments"] });
      setStatus((s) => ({ ...s, [row.id]: "Saved ✓" }));
    } catch (e) {
      setStatus((s) => ({ ...s, [row.id]: e instanceof Error ? e.message : "Not saved." }));
    }
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">WHO ANSWERS WHAT</p>
          <h2>Department responsibilities</h2>
        </div>
      </div>
      <p className="muted">
        When an answer is missing something, the copilot suggests which department to ask by reading these lines.
        Name the kinds of information each one holds. Clear a line and save to go back to the default.
      </p>
      <table>
        <thead>
          <tr>
            <th>Department</th>
            <th>Responsible for</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => {
            const edited = drafts[row.id] !== undefined && drafts[row.id] !== (row.responsibilities ?? "");
            const note = edited ? "Unsaved change" : status[row.id];
            return (
              <tr key={row.id}>
                <td>{row.name}</td>
                <td>
                  <input
                    style={{ width: "100%" }}
                    value={drafts[row.id] ?? row.responsibilities ?? ""}
                    onChange={(e) => {
                      const value = e.target.value;
                      setDrafts((d) => ({ ...d, [row.id]: value }));
                      setStatus((s) => ({ ...s, [row.id]: "" }));
                    }}
                  />
                </td>
                <td className="row-action">
                  <button disabled={!edited || status[row.id] === "Saving…"} onClick={() => save(row)}>
                    {status[row.id] === "Saving…" ? "Saving…" : "Save"}
                  </button>
                  {note && <div className="muted">{note}</div>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}


/**
 * Typing the company documents nobody filed.
 *
 * Every excerpt inherits its document's type, so a corpus uploaded without one
 * reads OTHER throughout -- and a blank form then looks exactly like a signed
 * certificate to the drafting model.
 */
export function ClassifyDocuments() {
  const client = useQueryClient();
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    setStatus("Reading documents…");
    try {
      let remaining = 1;
      let filed = 0;
      let stamped = 0;
      while (remaining > 0) {
        const r = await api<{ classified: number; remaining: number; evidence_restamped: number }>(
          "/knowledge/documents/classify-unclassified?limit=25",
          { method: "POST" },
        );
        filed += r.classified;
        stamped += r.evidence_restamped;
        remaining = r.remaining;
        setStatus(`Filed ${filed} document(s); ${remaining} left…`);
        if (r.classified === 0) break;
      }
      setStatus(`Filed ${filed} document(s); ${stamped} excerpt(s) now carry a type.`);
      await client.invalidateQueries();
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "Filing failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">WHAT EACH DOCUMENT IS</p>
          <h2>File untyped documents</h2>
        </div>
        <button disabled={busy} onClick={run}>
          {busy ? "Filing…" : "File them"}
        </button>
      </div>
      <p className="muted">
        A document nobody typed leaves its excerpts typed &quot;other&quot;, and a blank form then looks like a
        signed certificate. This reads each untyped company document and files it. A type a person chose is
        never changed.
      </p>
      {status && <p className="muted">{status}</p>}
    </section>
  );
}
