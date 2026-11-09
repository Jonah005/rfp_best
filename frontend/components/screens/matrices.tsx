"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * The four review matrices.
 *
 * They are four separate views rather than one filtered table because the
 * questions differ. Eligibility asks "can we qualify at all"; technical asks
 * "can we answer"; submission asks "what must be signed and attached";
 * the contract register asks "what are we agreeing to". A reviewer works one
 * of these at a time.
 *
 * Every row carries its clause reference and its evidence ids, so a figure can
 * be followed back to the text it came from.
 */

type Row = {
  requirement_id: string;
  code: string;
  category: string;
  /** Who the clause binds. "CUSTOMER" rows can never be evidenced by us. */
  obligation_owner?: string;
  mandatory: boolean;
  scored: boolean;
  applicability: string;
  requirement_text: string | null;
  clause_ref: string | null;
  evidence_status: string | null;
  bid_readiness: string | null;
  rationale: string | null;
  evidence_ids: string[];
};

const TABS = [
  { key: "eligibility", label: "Eligibility", hint: "Can the company qualify at all?" },
  { key: "technical", label: "Technical", hint: "Can the requirement be answered?" },
  { key: "submission", label: "Submission", hint: "What must be signed or attached?" },
  { key: "contract", label: "Contract register", hint: "What would we be agreeing to?" },
] as const;

const ALARMING = new Set([
  "BLOCKED",
  "NEEDS_CLIENT_CLARIFICATION",
  "NEEDS_COMPANY_CONFIRMATION",
  "NEEDS_REFRESH",
]);
const NOT_CURRENT = new Set(["NONE", "EXPIRED", "HISTORICAL_VERIFIED", "CONFLICTING"]);

/** How much evidence the last run had to work with. */
type EvidencePool = { total: number; approved: number; embedded: number };

/**
 * What the model check did to the shortlists on the last run.
 *
 * Worth showing because it is the difference between two runs that produce the
 * same matrix for opposite reasons. `dropped` at zero means retrieval and the
 * model agreed and the check earned nothing; a high share means retrieval is
 * casting a wide net and the check is doing the work. `failures` above zero
 * means the run finished on the similarity thresholds alone — the matrix is
 * still valid, but it was not checked, and nothing else on the screen would
 * say so.
 */
type Verification = {
  checked: number;
  supported: number;
  partial: number;
  dropped: number;
  unjudged: number;
  failures: number;
  disagreements: number;
  /** What the run actually cost, and what it avoided. */
  calls: number;
  cache_hits: number;
  skipped_requirements: number;
};

export function Matrices({
  bidId,
  action,
}: {
  bidId: string;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
}) {
  const [tab, setTab] = useState<(typeof TABS)[number]["key"]>("eligibility");
  const [pool, setPool] = useState<EvidencePool | null>(null);
  const [verification, setVerification] = useState<Verification | null>(null);

  const rows = useQuery({
    queryKey: ["matrix", bidId, tab],
    queryFn: () => api<Row[]>(`/bids/${bidId}/matrices/${tab}`),
    enabled: Boolean(bidId),
  });

  if (!bidId) return <Empty text="Select an active bid to load the matrices." />;

  const active = TABS.find((t) => t.key === tab)!;

  // Every requirement reading "no supporting evidence" looks identical whether
  // the corpus is empty, unapproved, un-embedded, or the matcher is failing.
  // These three numbers are what separate those cases, so they are shown
  // rather than left for someone to deduce.
  const poolNote = !pool ? null : pool.total === 0 ? (
    <p className="hint block warn">
      <b>No company evidence exists yet.</b> Every requirement below will read as
      unevidenced until documents are uploaded on the Company knowledge screen and
      extraction produces excerpts from them. This is not a matching failure.
    </p>
  ) : pool.approved === 0 ? (
    <p className="hint block warn">
      <b>
        {pool.total} excerpt{pool.total === 1 ? "" : "s"} exist but none are approved.
      </b>{" "}
      Only approved evidence may back a claim, so nothing can match. Approve it on
      the Company knowledge screen as the Knowledge Admin, then run this again.
    </p>
  ) : pool.embedded < pool.approved ? (
    <p className="hint block warn">
      <b>
        {pool.approved - pool.embedded} of {pool.approved} approved excerpts have no
        embedding.
      </b>{" "}
      Those can only be found by shared words, not by meaning. Run the embedding
      backfill on the Company knowledge screen, then assess again.
    </p>
  ) : (
    <p className="hint block">
      Matched against {pool.approved} approved excerpt
      {pool.approved === 1 ? "" : "s"}, all embedded for semantic search.
    </p>
  );

  const checkNote = !verification?.checked ? null : verification.failures ? (
    <p className="hint block warn">
      <b>The relevance check stopped partway through this run.</b> The rest of the
      matrix was matched on similarity scores alone, which attaches more than a
      reader would. Assess again once the model provider is reachable.
    </p>
  ) : (
    <p className="hint block">
      The model read {verification.checked} candidate
      {verification.checked === 1 ? "" : "s"} against their requirements and
      discarded {verification.dropped} as unrelated. {verification.supported}{" "}
      attached as proof, {verification.partial} as related-only
      {verification.unjudged ? `, ${verification.unjudged} unchecked` : ""}.
      {" "}
      <span className="muted">
        {verification.calls} model call
        {verification.calls === 1 ? "" : "s"}
        {verification.cache_hits + verification.skipped_requirements > 0
          ? ` (${verification.cache_hits} reused, ${verification.skipped_requirements} not needed)`
          : ""}
        .
      </span>
      {verification.disagreements > 0 && (
        <>
          {" "}
          <b>
            It reads {verification.disagreements} row
            {verification.disagreements === 1 ? "" : "s"} differently from the
            computed status.
          </b>{" "}
          Those rows keep the computed verdict and say so in their rationale —
          they are the ones worth reading first.
        </>
      )}
    </p>
  );

  return (
    <>
      {poolNote}
      {checkNote}
      <section className="toolbar tabs">
        {TABS.map((item) => (
          <button
            key={item.key}
            className={tab === item.key ? "" : "ghost"}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
        <span className="spacer" />
        <button
          className="secondary"
          onClick={() =>
            action("Requirement assessment", async () => {
              const report = await api<{
                evidence_pool?: EvidencePool;
                verification?: Verification;
              }>(`/bids/${bidId}/assess`, { method: "POST", body: "{}" });
              setPool(report.evidence_pool ?? null);
              setVerification(report.verification ?? null);
            })
          }
        >
          Run assessment
        </button>
      </section>

      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="kicker">{active.hint.toUpperCase()}</p>
            <h2>{active.label}</h2>
          </div>
          <span className="pill">{rows.data?.length ?? 0} requirement(s)</span>
        </div>

        {rows.isLoading ? (
          <Empty text="Loading the matrix…" />
        ) : rows.error ? (
          <Empty text={(rows.error as Error).message} />
        ) : !rows.data?.length ? (
          <Empty text="No requirements in this matrix yet. Requirements come from the tender analysis: open Tender package, wait until every document shows INDEXED, press Run analysis, then come back and run the assessment." />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Code</th>
                  <th>Clause</th>
                  <th>Requirement</th>
                  <th>Evidence status</th>
                  <th>Bid readiness</th>
                  <th>Evidence</th>
                  <th>Rationale</th>
                </tr>
              </thead>
              <tbody>
                {rows.data.map((row) => (
                  <tr key={row.requirement_id}>
                    <td>
                      <strong>{row.code}</strong>
                      {row.mandatory && <span className="tag">mandatory</span>}
                      {row.scored && <span className="tag">scored</span>}
                    </td>
                    <td>{row.clause_ref ?? "—"}</td>
                    <td>{row.requirement_text ?? "—"}</td>
                    <td>
                      <Badge value={row.evidence_status} bad={NOT_CURRENT} />
                    </td>
                    <td>
                      <Badge value={row.bid_readiness} bad={ALARMING} />
                    </td>
                    <td>
                      {row.evidence_ids.length
                        ? `${row.evidence_ids.length} item(s)`
                        : "none"}
                    </td>
                    <td>
                      {row.obligation_owner === "CUSTOMER" && (
                        <span
                          className="pill"
                          title="A right the customer reserves for itself; nothing for us to evidence"
                        >
                          Customer&nbsp;term
                        </span>
                      )}{" "}
                      {row.rationale ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
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
