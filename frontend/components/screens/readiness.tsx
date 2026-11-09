"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { BidDashboard } from "./extras";

/**
 * Bid readiness.
 *
 * The design rule this screen exists to honour: every figure opens into the
 * requirements that produced it. A number a user cannot open is a number they
 * cannot act on, and an unopenable percentage is exactly the kind of figure
 * that gets quoted in a management meeting and later turns out to be wrong.
 *
 * The two axes are also kept visually apart. `evidence_status` says what the
 * proof shows; `bid_readiness` says what has to happen next. Showing them as
 * one bar would recreate, in the UI, precisely the collapse the data model
 * refuses to make.
 */

type Summary = {
  total_requirements: number;
  applicable_requirements: number;
  not_applicable: number;
  assessed: number;
  by_readiness: Record<string, number>;
  by_evidence_status: Record<string, number>;
  requirement_coverage_pct: number;
  evidence_readiness_pct: number;
  critical_blockers: number;
  open_clarifications: number;
  outstanding_actions: number;
  mandatory_unready: number;
  deliverables_total: number;
};

type Item = {
  requirement_id: string;
  code: string;
  category: string;
  is_mandatory: boolean;
  applicability: string;
  text: string | null;
  evidence_status: string | null;
  bid_readiness: string | null;
  rationale: string | null;
};

/** Readiness states that mean "someone must act before this can ship". */
const ALARMING = new Set([
  "BLOCKED",
  "NEEDS_CLIENT_CLARIFICATION",
  "NEEDS_COMPANY_CONFIRMATION",
  "NEEDS_REFRESH",
]);

/** Evidence statuses that must never be read as current proof. */
const NOT_CURRENT = new Set(["NONE", "EXPIRED", "HISTORICAL_VERIFIED", "CONFLICTING"]);

export function Readiness({ bidId }: { bidId: string }) {
  const [metric, setMetric] = useState<string | null>(null);

  const summary = useQuery({
    queryKey: ["readiness", bidId],
    queryFn: () => api<Summary>(`/bids/${bidId}/readiness`),
    enabled: Boolean(bidId),
  });

  const items = useQuery({
    queryKey: ["readiness-items", bidId, metric],
    queryFn: () => api<Item[]>(`/bids/${bidId}/readiness/${metric}/items`),
    enabled: Boolean(bidId && metric),
  });

  if (!bidId) return <Empty text="Select an active bid to load readiness." />;
  if (summary.isLoading) return <Empty text="Loading readiness…" />;
  if (summary.error) return <Empty text={(summary.error as Error).message} />;
  const data = summary.data;
  if (!data) return <Empty text="No readiness data yet." />;

  const unassessed = data.applicable_requirements - data.assessed;

  return (
    <>
      <BidDashboard bidId={bidId} />
      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="kicker">SEVERAL MEASURES, NEVER ONE PERCENTAGE</p>
            <h2>Where this bid stands</h2>
          </div>
          {metric && (
            <button className="ghost" onClick={() => setMetric(null)}>
              Clear drill-through
            </button>
          )}
        </div>

        <div className="drill-grid">
          <Figure
            label="Requirement coverage"
            value={`${data.requirement_coverage_pct}%`}
            note={`${data.assessed} of ${data.applicable_requirements} applicable assessed`}
          />
          <Figure
            label="Evidence readiness"
            value={`${data.evidence_readiness_pct}%`}
            note="Current or partial proof"
          />
          <Figure
            label="Critical blockers"
            value={data.critical_blockers}
            metric="CRITICAL_BLOCKERS"
            active={metric}
            onOpen={setMetric}
            tone={data.critical_blockers ? "bad" : "ok"}
          />
          <Figure
            label="Mandatory unready"
            value={data.mandatory_unready}
            metric="MANDATORY_UNREADY"
            active={metric}
            onOpen={setMetric}
            tone={data.mandatory_unready ? "bad" : "ok"}
          />
          <Figure
            label="Outstanding actions"
            value={data.outstanding_actions}
            metric="OUTSTANDING_ACTIONS"
            active={metric}
            onOpen={setMetric}
          />
          <Figure
            label="Unassessed"
            value={unassessed}
            metric="UNASSESSED"
            active={metric}
            onOpen={setMetric}
            tone={unassessed ? "warn" : "ok"}
          />
          <Figure
            label="Open RFP gaps"
            value={data.open_clarifications}
            note="Asked of the customer"
          />
          <Figure
            label="Not applicable"
            value={data.not_applicable}
            metric="NOT_APPLICABLE"
            active={metric}
            onOpen={setMetric}
            note="Excluded from coverage"
          />
        </div>
      </section>

      <div className="axes">
        <Axis
          kicker="WHAT MUST HAPPEN NEXT"
          title="Bid readiness"
          counts={data.by_readiness}
          alarming={ALARMING}
          active={metric}
          onOpen={setMetric}
        />
        <Axis
          kicker="WHAT THE PROOF SHOWS"
          title="Evidence status"
          counts={data.by_evidence_status}
          alarming={NOT_CURRENT}
          active={metric}
          onOpen={setMetric}
        />
      </div>

      {metric && (
        <section className="panel">
          <div className="panel-head">
            <div>
              <p className="kicker">BEHIND THE NUMBER</p>
              <h2>{metric.replaceAll("_", " ").toLowerCase()}</h2>
            </div>
            <span className="pill">{items.data?.length ?? 0} requirement(s)</span>
          </div>
          {items.isLoading ? (
            <Empty text="Loading the requirements behind this figure…" />
          ) : items.error ? (
            <Empty text={(items.error as Error).message} />
          ) : !items.data?.length ? (
            <Empty text="Nothing sits behind this figure." />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Code</th>
                    <th>Category</th>
                    <th>Requirement</th>
                    <th>Evidence</th>
                    <th>Readiness</th>
                    <th>Why</th>
                  </tr>
                </thead>
                <tbody>
                  {items.data.map((item) => (
                    <tr key={item.requirement_id}>
                      <td>
                        <strong>{item.code}</strong>
                        {item.is_mandatory && <span className="tag">mandatory</span>}
                      </td>
                      <td>{item.category}</td>
                      <td>{item.text ?? "—"}</td>
                      <td>
                        <Badge value={item.evidence_status} bad={NOT_CURRENT} />
                      </td>
                      <td>
                        <Badge value={item.bid_readiness} bad={ALARMING} />
                      </td>
                      <td>{item.rationale ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </>
  );
}

function Figure({
  label,
  value,
  note,
  metric,
  active,
  onOpen,
  tone,
}: {
  label: string;
  value: string | number;
  note?: string;
  metric?: string;
  active?: string | null;
  onOpen?: (metric: string) => void;
  tone?: "ok" | "warn" | "bad";
}) {
  const openable = Boolean(metric && onOpen);
  return (
    <button
      type="button"
      className={`figure ${openable ? "openable" : ""} ${active === metric ? "on" : ""} ${tone ?? ""}`}
      disabled={!openable}
      onClick={() => metric && onOpen?.(metric)}
    >
      <small>{label}</small>
      <strong>{value}</strong>
      <span>{note ?? (openable ? "Open the requirements behind this" : " ")}</span>
    </button>
  );
}

function Axis({
  kicker,
  title,
  counts,
  alarming,
  active,
  onOpen,
}: {
  kicker: string;
  title: string;
  counts: Record<string, number>;
  alarming: Set<string>;
  active: string | null;
  onOpen: (metric: string) => void;
}) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, count]) => sum + count, 0) || 1;
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">{kicker}</p>
          <h2>{title}</h2>
        </div>
      </div>
      <div className="axis-body">
        {entries.length === 0 && <Empty text="Nothing assessed yet." />}
        {entries.map(([state, count]) => (
          <button
            key={state}
            type="button"
            className={`bar-row ${active === state ? "on" : ""}`}
            onClick={() => onOpen(state)}
          >
            <span className="bar-label">
              {state.replaceAll("_", " ").toLowerCase()}
            </span>
            <span className="bar-track">
              <span
                className={`bar-fill ${alarming.has(state) ? "bad" : ""}`}
                style={{ width: `${Math.max(4, (count / total) * 100)}%` }}
              />
            </span>
            <span className="bar-count">{count}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function Badge({ value, bad }: { value: string | null; bad: Set<string> }) {
  if (!value) return <span className="badge muted">unassessed</span>;
  return (
    <span className={`badge ${bad.has(value) ? "bad" : "good"}`}>
      {value.replaceAll("_", " ").toLowerCase()}
    </span>
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
