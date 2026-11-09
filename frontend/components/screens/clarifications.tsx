"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * The defects the analysis found in the tender, and what to do about each.
 *
 * This screen exists because scope confidence was unreachable without it. The
 * grader holds a bid at LOW while any BLOCKING clarification is unresolved, and
 * every other screen only *counted* those issues — so a reviewer could see that
 * ten things were blocking and had nowhere to act on them.
 *
 * Nobody asks the tender's issuer, so a gap is settled inside the company. The
 * owning department decides what it means:
 *
 *   Assumption   we decided what the unclear point means; the wording is recorded
 *                and the response is written on it (stored as ANSWERED)
 *   Closed       it makes no difference to the response
 *   Withdrawn    the analysis misread it; it is not a real gap
 *
 * An assumption needs its wording. That is a server rule, not a courtesy: a gap
 * marked settled with nothing written down leaves the next reviewer to inherit it
 * as fact.
 */

type Action = (label: string, work: () => Promise<unknown>) => Promise<void>;

type Issue = {
  id: string;
  issue_type: string;
  description: string;
  severity: string;
  blocks_generation: boolean;
  recommended_question: string | null;
  resolution_note?: string | null;
  status: string;
  client_response: string | null;
  source_a: Record<string, unknown> | null;
  source_b: Record<string, unknown> | null;
  department_id?: string | null;
  department?: string | null;
  owner_user_id?: string | null;
  owner_name?: string | null;
  owner_position?: "Head" | "Team member" | null;
  routing_source?: "category" | "model" | "rules" | "person" | null;
  routing_reason?: string | null;
};

type Candidates = {
  scope: string;
  reason: string;
  candidates: {
    id: string;
    display_name: string;
    positions?: { department: string; position: string }[];
  }[];
};

const SEVERITY_ORDER = ["BLOCKING", "MAJOR", "MINOR", "INFO"];
const OPEN_STATUSES = new Set(["OPEN", "SENT_TO_CLIENT"]);

export function Clarifications({ bidId, action }: { bidId: string; action: Action }) {
  const [showResolved, setShowResolved] = useState(false);
  const [answering, setAnswering] = useState<string | null>(null);
  const [response, setResponse] = useState("");
  const [filter, setFilter] = useState("ALL");

  const me = useQuery({
    queryKey: ["me-id"],
    queryFn: () => api<{ user: { id: string } }>("/auth/me"),
  });
  const myId = me.data?.user.id;

  const issues = useQuery({
    queryKey: ["clarifications", bidId],
    queryFn: () => api<Issue[]>(`/bids/${bidId}/clarifications`),
    enabled: Boolean(bidId),
  });

  if (!bidId) {
    return (
      <section className="panel">
        <Empty text="Pick an active bid in the header to see what its RFP leaves out." />
      </section>
    );
  }

  const all = issues.data ?? [];
  const shown = all.filter((issue) => (showResolved ? true : OPEN_STATUSES.has(issue.status)));
  const departments = Array.from(
    new Set(shown.map((issue) => issue.department).filter((d): d is string => Boolean(d))),
  ).sort();
  const mine = shown.filter((issue) => myId && issue.owner_user_id === myId).length;
  const visible = shown
    .filter((issue) =>
      filter === "ALL"
        ? true
        : filter === "MINE"
          ? Boolean(myId) && issue.owner_user_id === myId
          : issue.department === filter,
    )
    // Blocking first: these are the ones holding scope confidence down, and a
    // reviewer working top-to-bottom should meet them before the advisory ones.
    .sort(
      (a, b) =>
        SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity),
    );

  const blocking = all.filter(
    (issue) => issue.blocks_generation && OPEN_STATUSES.has(issue.status),
  ).length;

  function setStatus(issue: Issue, status: string, clientResponse?: string) {
    return action(`RFP gap ${status.toLowerCase().replaceAll("_", " ")}`, async () => {
      await api(`/clarifications/${issue.id}/status`, {
        method: "POST",
        body: JSON.stringify({
          status,
          ...(clientResponse ? { client_response: clientResponse } : {}),
        }),
      });
      setAnswering(null);
      setResponse("");
    });
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">
            TENDER DEFECTS · {blocking} BLOCKING ·{" "}
            {(issues.data ?? []).filter((i) => i.status === "OPEN" && i.recommended_question).length}{" "}
            TO DECIDE
          </p>
          <h2>Missing from the RFP</h2>
        </div>
        <div className="panel-actions">
          <button
            className="ghost"
            title="Have the AI choose the department of every open RFP gap that is not tied to a requirement"
            onClick={() =>
              action("RFP gaps routed", async () => {
                await api(`/bids/${bidId}/clarifications/route`, { method: "POST" });
              })
            }
          >
            Ask AI to route
          </button>
          <label className="toggle">
            <input
              type="checkbox"
              checked={showResolved}
              onChange={(event) => setShowResolved(event.target.checked)}
            />
            Show resolved
          </label>
        </div>
      </div>

      {all.length > 0 && (
        <div className="filter-chips" role="group" aria-label="Filter by owner or department">
          <button
            className={filter === "ALL" ? "chip on" : "chip"}
            onClick={() => setFilter("ALL")}
          >
            All ({shown.length})
          </button>
          <button
            className={filter === "MINE" ? "chip on" : "chip"}
            onClick={() => setFilter("MINE")}
          >
            Mine ({mine})
          </button>
          {departments.map((name) => (
            <button
              key={name}
              className={filter === name ? "chip on" : "chip"}
              onClick={() => setFilter(name)}
            >
              {name} ({shown.filter((issue) => issue.department === name).length})
            </button>
          ))}
        </div>
      )}

      {blocking > 0 && (
        <p className="hint block warn">
          <b>
            {blocking} blocking issue{blocking === 1 ? "" : "s"} hold scope confidence at
            LOW.
          </b>{" "}
          Each one is a gap in the tender itself — a blank, a placeholder, a
          contradiction — not a judgement about your bid. The owning department
          records the assumption the response will rest on, then re-run the
          analysis on Screen 1 to lift the level.
        </p>
      )}

      <RoutingTable action={action} />
      <RoutingComparison bidId={bidId} />

      {issues.isLoading ? (
        <Empty text="Loading the RFP gaps…" />
      ) : issues.error ? (
        <Empty text={issues.error.message} />
      ) : visible.length === 0 ? (
        <Empty
          text={
            showResolved
              ? "No gaps found in this RFP."
              : "No open gaps in the RFP. Tick “Show resolved” to see the closed ones."
          }
        />
      ) : (
        <ul className="issue-list">
          {visible.map((issue) => (
            <li key={issue.id} className={issue.blocks_generation ? "blocking" : ""}>
              <div className="issue-head">
                <span className={`sev sev-${issue.severity.toLowerCase()}`}>
                  {issue.severity}
                </span>
                <span className="pill">{issue.issue_type.replaceAll("_", " ")}</span>
                <span className="issue-status">{issue.status.replaceAll("_", " ")}</span>
                {issue.status === "OPEN" && !issue.recommended_question && (
                  <span className="pill">internal note &mdash; nothing to decide</span>
                )}
              </div>
              <IssueOwner issue={issue} bidId={bidId} action={action} />
              <p className="issue-body">{issue.description}</p>
              {issue.recommended_question && (
                <p className="issue-question">
                  <b>The open point:</b> {issue.recommended_question}
                </p>
              )}
              {issue.resolution_note && issue.status !== "OPEN" && (
                <p className="issue-answer">
                  <b>Why closed:</b> {issue.resolution_note}
                </p>
              )}
              {issue.client_response && (
                <p className="issue-answer">
                  <b>Our assumption:</b> {issue.client_response}
                </p>
              )}
              <ClauseRefs a={issue.source_a} b={issue.source_b} />

              {OPEN_STATUSES.has(issue.status) && (
                <div className="issue-actions">
                  <button className="ghost" onClick={() => setAnswering(issue.id)}>
                    Record our assumption
                  </button>
                  <button className="ghost" onClick={() => setStatus(issue, "CLOSED")}>
                    No impact — close
                  </button>
                  <button className="ghost" onClick={() => setStatus(issue, "WITHDRAWN")}>
                    Not a real issue
                  </button>
                </div>
              )}

              {answering === issue.id && (
                <form
                  className="answer-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    if (!response.trim()) return;
                    setStatus(issue, "ANSWERED", response.trim());
                  }}
                >
                  <label>
                    What we are assuming this means, and why
                    <textarea
                      value={response}
                      onChange={(event) => setResponse(event.target.value)}
                      rows={3}
                      required
                      placeholder="For example: we treat the one-week period as running from NDA signature."
                    />
                  </label>
                  <div className="issue-actions">
                    <button disabled={!response.trim()}>Save answer</button>
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => {
                        setAnswering(null);
                        setResponse("");
                      }}
                    >
                      Cancel
                    </button>
                  </div>
                </form>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

type Comparison = {
  total: number;
  ai_answered: number;
  agree: number;
  rows: {
    id: string;
    problem: string;
    fixed: string | null;
    fixed_by: string;
    ai: string | null;
    ai_reason: string | null;
    agree: boolean;
  }[];
};

/** Fixed rules against the AI for every open clarification. Changes nothing. */
function RoutingComparison({ bidId }: { bidId: string }) {
  const [data, setData] = useState<Comparison | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [onlyDiff, setOnlyDiff] = useState(true);

  async function run() {
    setBusy(true);
    setError("");
    try {
      setData(await api<Comparison>(`/bids/${bidId}/clarifications/compare-routing`, { method: "POST" }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not compare.");
    } finally {
      setBusy(false);
    }
  }

  const rows = (data?.rows ?? []).filter((r) => (onlyDiff ? !r.agree : true));
  return (
    <details className="routing-table">
      <summary>Compare AI with the fixed rules</summary>
      <p className="hint">
        Shows who the fixed rules (requirement category, then wording) would pick next to who the
        AI picks, for every open RFP gap. Nothing is changed.
      </p>
      <button className="ghost" disabled={busy} onClick={run}>
        {busy ? "Asking the AI…" : data ? "Run again" : "Run comparison"}
      </button>
      {error && <p className="hint">{error}</p>}
      {data && (
        <>
          <p>
            <b>{data.agree}</b> of {data.ai_answered} agree; {data.ai_answered - data.agree} differ
            {data.total > data.ai_answered ? ` (${data.total - data.ai_answered} had no AI answer)` : ""}.{" "}
            <label className="toggle">
              <input type="checkbox" checked={onlyDiff} onChange={(e) => setOnlyDiff(e.target.checked)} />
              Only show differences
            </label>
          </p>
          <table>
            <thead>
              <tr>
                <th>Problem</th>
                <th>Fixed rules</th>
                <th>AI</th>
                <th>AI reason</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td style={{ textTransform: "none" }}>{r.problem}</td>
                  <td style={{ textTransform: "none" }}>
                    {r.fixed ?? "—"} <span className="hint">({r.fixed_by})</span>
                  </td>
                  <td style={{ textTransform: "none" }}>{r.ai ?? "—"}</td>
                  <td style={{ textTransform: "none" }}>{r.ai_reason ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </details>
  );
}

type RoutingData = {
  can_edit: boolean;
  routes: {
    category: string;
    department_id: string | null;
    department: string | null;
    is_default: boolean;
    default_department: string | null;
  }[];
  departments: { id: string; name: string }[];
};

/**
 * Which department each kind of requirement goes to. Tasks, requirement
 * recommendations and clarifications all read this one table, so a change here
 * applies to all of them, for work created from then on.
 */
function RoutingTable({ action }: { action: Action }) {
  const queryClient = useQueryClient();
  const routing = useQuery({
    queryKey: ["category-routing"],
    queryFn: () => api<RoutingData>("/admin/category-routing"),
  });
  const data = routing.data;
  if (!data) return null;

  async function change(category: string, departmentId: string | null) {
    await action("Routing updated", async () => {
      await api(
        `/admin/category-routing/${category}`,
        departmentId
          ? { method: "PUT", body: JSON.stringify({ department_id: departmentId }) }
          : { method: "DELETE" },
      );
      await queryClient.invalidateQueries({ queryKey: ["category-routing"] });
    });
  }

  return (
    <details className="routing-table">
      <summary>
        How work is sent to departments{" "}
        <span className="hint">
          ({data.routes.filter((r) => !r.is_default).length} changed from the default)
        </span>
      </summary>
      <p className="hint">
        Every requirement has a category. This table says which department answers for each
        category. Tasks and RFP gaps follow it. Changing a row affects work created
        afterwards; tasks already made keep their department.
      </p>
      <table>
        <thead>
          <tr>
            <th>Requirement category</th>
            <th>Department</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data.routes.map((row) => (
            <tr key={row.category}>
              <td>{row.category.replaceAll("_", " ").toLowerCase()}</td>
              <td>
                {data.can_edit ? (
                  <select
                    aria-label={`Department for ${row.category}`}
                    value={row.department_id ?? ""}
                    onChange={(event) => event.target.value && change(row.category, event.target.value)}
                  >
                    {!row.department_id && <option value="">None</option>}
                    {data.departments.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name}
                      </option>
                    ))}
                  </select>
                ) : (
                  (row.department ?? "None")
                )}
              </td>
              <td>
                {row.is_default ? (
                  <span className="hint">default</span>
                ) : (
                  data.can_edit && (
                    <button className="ghost small" onClick={() => change(row.category, null)}>
                      Reset to {row.default_department ?? "default"}
                    </button>
                  )
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

/**
 * Whose question this is: the department that answers for it, and the person
 * who decides it. The list of people is fetched only when someone opens
 * it, so a long list of issues does not make a long list of requests.
 */
function IssueOwner({
  issue,
  bidId,
  action,
}: {
  issue: Issue;
  bidId: string;
  action: Action;
}) {
  const [editing, setEditing] = useState(false);
  const queryClient = useQueryClient();
  const options = useQuery({
    queryKey: ["clarification-assignable", issue.id],
    queryFn: () => api<Candidates>(`/clarifications/${issue.id}/assignable`),
    enabled: editing,
  });
  const done = issue.status === "CLOSED" || issue.status === "WITHDRAWN";

  const who = issue.owner_name
    ? `${issue.owner_name}${issue.owner_position ? ` · ${issue.owner_position}` : ""}`
    : "No owner yet";

  return (
    <p className="issue-owner">
      <span
        className="pill"
        title={
          issue.routing_source === "model"
            ? `Chosen by AI: ${issue.routing_reason ?? ""}`
            : issue.routing_source === "category"
              ? "From the requirement it concerns"
              : issue.routing_source === "person"
                ? "Moved by a person"
                : "Matched on wording"
        }
      >
        {issue.department ?? "No department"}
      </span>
      {issue.routing_source === "model" && issue.routing_reason && (
        <span className="hint">AI: {issue.routing_reason}</span>
      )}
      <span>
        Owner: <b>{who}</b>
      </span>
      {!done && (
        <button className="ghost small" onClick={() => setEditing((on) => !on)}>
          {editing ? "Cancel" : "Change owner"}
        </button>
      )}
      {editing &&
        (options.isLoading ? (
          <span className="hint">Loading…</span>
        ) : options.data && options.data.candidates.length > 0 ? (
          <select
            aria-label="Owner"
            value={issue.owner_user_id ?? ""}
            onChange={(event) => {
              const owner = event.target.value;
              if (!owner) return;
              void action("Owner changed", async () => {
                await api(`/clarifications/${issue.id}/owner`, {
                  method: "POST",
                  body: JSON.stringify({ owner_user_id: owner }),
                });
                await queryClient.invalidateQueries({ queryKey: ["clarifications", bidId] });
                setEditing(false);
              });
            }}
          >
            <option value="">Choose a person…</option>
            {options.data.candidates.map((person) => (
              <option key={person.id} value={person.id}>
                {person.display_name} (
                {(person.positions ?? [])
                  .map((p) => `${p.department} · ${p.position}`)
                  .join(", ") || "no department"}
                )
              </option>
            ))}
          </select>
        ) : (
          <span className="hint">{options.data?.reason ?? "You cannot change the owner."}</span>
        ))}
    </p>
  );
}

/** Where the issue came from. A defect a reviewer cannot locate is not actionable. */
function ClauseRefs({
  a,
  b,
}: {
  a: Record<string, unknown> | null;
  b: Record<string, unknown> | null;
}) {
  const refs = [a, b]
    .filter(Boolean)
    .map((source) => (source as Record<string, unknown>).clause_ref)
    .filter((ref): ref is string => typeof ref === "string" && ref.length > 0);
  if (refs.length === 0) return null;
  return (
    <p className="issue-source">
      {refs.length > 1 ? "Conflicting clauses" : "Clause"}: {refs.join(" vs ")}
    </p>
  );
}

function Empty({ text }: { text: string }) {
  return (
    <div className="empty">
      <span>◌</span>
      <p>{text}</p>
    </div>
  );
}
