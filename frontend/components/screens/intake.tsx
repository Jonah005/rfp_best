"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

const STILL_READING = ["UPLOADED", "SCANNING", "PARSING", "PARSED", "CLASSIFIED"];

/**
 * Screen 1, all three steps.
 *
 * The steps are separate because each one is a different decision with a
 * different consequence. Creating an opportunity is cheap and reversible —
 * it is a note that a tender exists. Promoting it to a bid is the governed
 * moment: from then on the record has a state machine, an audit trail and a
 * release gate behind it, which is why promotion is its own button and never
 * a side effect of typing a customer name.
 *
 * Uploading is third rather than second because tender documents attach to a
 * bid, not to an opportunity. A file has to know which governed record owns it
 * before it can be scanned, parsed and chunked under that record's isolation.
 *
 * Promotion is idempotent server-side, so a double click costs nothing; the
 * button is still hidden once a bid exists, because showing an action that
 * silently does nothing teaches people to distrust the buttons.
 */

type Action = (label: string, work: () => Promise<unknown>) => Promise<void>;
type Opportunity = {
  id: string;
  customer_name: string;
  tender_name: string;
  tender_reference: string | null;
  region: string | null;
  currency: string | null;
};
type Bid = { id: string; opportunity_id: string; state: string };
type AnalysisOut = {
  precedence?: { resolved?: number; unresolved?: number; clauses?: number };
  requirements?: {
    requirements_created?: number;
    blocks_considered?: number;
    coverage_ratio?: number;
    model_calls?: number;
  };
  deliverables?: number;
  ambiguities?: number;
  scope_confidence?: string;
  detailed_drafting_allowed?: boolean;
  has_facts?: boolean;
};
// Mirrors DocumentVersionOut. The earlier guess at these names (version_number,
// original_filename, parse_status) matched nothing the API returns, so every
// column rendered "—" and the file column showed a truncated uuid.
type ScopeConfidence = {
  level?: string;
  blocking_reasons?: string[] | null;
  detailed_drafting_allowed?: boolean;
  signals?: Record<string, unknown>;
};
type DocumentVersion = {
  id: string;
  version_no?: number;
  mime_type?: string | null;
  byte_size?: number | null;
  state?: string | null;
  page_count?: number | null;
  block_count?: number | null;
  chunk_count?: number | null;
  parse_error?: string | null;
};

const BLANK = {
  customer_name: "",
  tender_name: "",
  tender_reference: "",
  currency: "AED",
  region: "UAE",
};

const LIMITS: Record<string, number> = {
  customer_name: 500,
  tender_name: 500,
  tender_reference: 200,
  currency: 3,
  region: 64,
};

export function Intake({
  action,
  bidId,
  onBidSelected,
}: {
  action: Action;
  bidId: string;
  onBidSelected: (id: string) => void;
}) {
  const [form, setForm] = useState(BLANK);
  const [file, setFile] = useState<File | null>(null);
  const [analysis, setAnalysis] = useState<AnalysisOut | null>(null);

  const opportunities = useQuery({
    queryKey: ["opportunities"],
    queryFn: () => api<Opportunity[]>("/opportunities"),
  });
  const bids = useQuery({ queryKey: ["bids"], queryFn: () => api<Bid[]>("/bids") });
  // The grader already records why it withheld confidence. Leaving that on the
  // server and showing only "LOW" turns a specific, actionable list into a
  // verdict with no appeal.
  const scope = useQuery({
    queryKey: ["scope-confidence", bidId],
    queryFn: () => api<ScopeConfidence>(`/bids/${bidId}/scope-confidence`),
    enabled: Boolean(bidId),
    retry: false,
  });
  const documents = useQuery({
    queryKey: ["tender-documents", bidId],
    queryFn: () => api<DocumentVersion[]>(`/bids/${bidId}/documents`),
    enabled: Boolean(bidId),
    // Parsing runs after the upload returns; keep checking until it is done.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((d) => STILL_READING.includes(String(d.state))) ? 3000 : false,
  });
  const reading = (documents.data ?? []).some((d) => STILL_READING.includes(String(d.state)));

  const bidFor = new Map((bids.data ?? []).map((bid) => [bid.opportunity_id, bid]));
  const activeBid = (bids.data ?? []).find((bid) => bid.id === bidId);

  return (
    <>
      <section className="panel split">
        <div>
          <p className="kicker">STEP 1 OF 3 · CREATE</p>
          <h2>Create the opportunity</h2>
          <form
            className="grid-form"
            onSubmit={(event) => {
              event.preventDefault();
              action("Opportunity creation", async () => {
                await api("/opportunities", { method: "POST", body: JSON.stringify(form) });
                setForm(BLANK);
              });
            }}
          >
            {Object.entries(form).map(([key, value]) => (
              <label key={key}>
                {key.replaceAll("_", " ")}
                {key === "currency" && <em className="hint"> ISO code, 3 letters</em>}
                <input
                  value={value}
                  maxLength={LIMITS[key]}
                  required={key === "customer_name" || key === "tender_name"}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      [key]:
                        key === "currency"
                          ? event.target.value.toUpperCase().slice(0, 3)
                          : event.target.value,
                    })
                  }
                />
              </label>
            ))}
            <button>Create opportunity</button>
          </form>
        </div>
        <div className="process">
          <h3>What happens next</h3>
          <ol>
            <li>
              <b>Promote</b>
              <span>The opportunity becomes a governed bid with an audit trail.</span>
            </li>
            <li>
              <b>Upload</b>
              <span>Files are scanned, parsed, classified and chunked.</span>
            </li>
            <li>
              <b>Understand</b>
              <span>Requirements, facts and precedence become reviewable records.</span>
            </li>
          </ol>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="kicker">STEP 2 OF 3 · PROMOTE</p>
            <h2>Opportunities</h2>
          </div>
          <div className="panel-actions">
            <span className="pill">Promotion is the governed moment</span>
          </div>
        </div>
        {opportunities.isLoading ? (
          <Empty text="Loading opportunities…" />
        ) : opportunities.error ? (
          <Empty text={opportunities.error.message} />
        ) : (opportunities.data ?? []).length === 0 ? (
          <Empty text="No opportunities yet. Create one above." />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Customer</th>
                  <th>Tender</th>
                  <th>Reference</th>
                  <th>Region</th>
                  <th>Bid</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(opportunities.data ?? []).map((opportunity) => {
                  const bid = bidFor.get(opportunity.id);
                  return (
                    <tr key={opportunity.id}>
                      <td>{opportunity.customer_name}</td>
                      <td>{opportunity.tender_name}</td>
                      <td>{opportunity.tender_reference || "—"}</td>
                      <td>{opportunity.region || "—"}</td>
                      <td>{bid ? <span className="pill">{bid.state}</span> : "Not promoted"}</td>
                      <td className="row-action">
                        {bid ? (
                          <button
                            className="ghost"
                            disabled={bid.id === bidId}
                            onClick={() => onBidSelected(bid.id)}
                          >
                            {bid.id === bidId ? "Active bid" : "Make active"}
                          </button>
                        ) : (
                          <button
                            onClick={() =>
                              action("Promotion to bid", async () => {
                                const created = await api<Bid>(
                                  `/opportunities/${opportunity.id}/promote`,
                                  { method: "POST" },
                                );
                                onBidSelected(created.id);
                              })
                            }
                          >
                            Promote to bid
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="kicker">STEP 3 OF 3 · UPLOAD &amp; ANALYSE</p>
            <h2>Tender package</h2>
          </div>
          <div className="panel-actions">
            {activeBid && <span className="pill">Bid state: {activeBid.state}</span>}
          </div>
        </div>
        {!bidId ? (
          <Empty text="Promote an opportunity above, or pick an active bid in the header, to upload its tender documents." />
        ) : (
          <>
            <form
              className="upload-row"
              onSubmit={(event) => {
                event.preventDefault();
                if (!file) return;
                // Captured now: React nulls `currentTarget` before the await
                // below resolves, so clearing the picker afterwards needs a
                // reference taken synchronously.
                const uploadForm = event.currentTarget;
                action("Tender document upload", async () => {
                  // Multipart, not JSON: `api` leaves FormData alone so the
                  // browser sets its own boundary. Scanning and validation are
                  // synchronous server-side; parsing is queued.
                  const body = new FormData();
                  body.append("file", file);
                  body.append("confidentiality_class", "CONFIDENTIAL");
                  body.append("language", "en");
                  await api(`/bids/${bidId}/documents`, { method: "POST", body });
                  setFile(null);
                  uploadForm.reset();
                  // The last analysis described the package before this file;
                  // showing it now would read as a verdict on the new document.
                  setAnalysis(null);
                  await documents.refetch();
                });
              }}
            >
              <input
                type="file"
                accept=".pdf,.docx,.xlsx,.pptx,.csv,.txt,.md"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
              <button disabled={!file}>Upload document</button>
              <button
                type="button"
                className="ghost"
                disabled={reading}
                title={reading ? "Wait until every document has been read (INDEXED)." : undefined}
                onClick={() =>
                  action("Tender analysis", async () => {
                    setAnalysis(
                      await api<AnalysisOut>(`/bids/${bidId}/analyze`, { method: "POST" }),
                    );
                  })
                }
              >
                {reading ? "Reading documents…" : "Run analysis"}
              </button>
            </form>
            {reading && (
              <p className="hint block">
                <b>Reading your document.</b> Text extraction and indexing run after the upload and can take
                a minute (longer the first time, while the search model loads). Analysis unlocks when every
                document below shows INDEXED.
              </p>
            )}
            {analysis && !reading && <AnalysisSummary result={analysis} />}
            {scope.data?.blocking_reasons?.length ? (
              <div className="scope-reasons">
                <p className="kicker">
                  WHY SCOPE CONFIDENCE IS {scope.data.level ?? "LOW"}
                </p>
                {!analysis && (
                  <p className="hint">
                    <b>This is from the last analysis run.</b> If you have uploaded or re-parsed
                    documents since, press Run analysis to update it.
                  </p>
                )}
                <ul>
                  {scope.data.blocking_reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
                <p className="hint">
                  Each line is a condition the grader checks. Resolve them — on the
                  matrices and clarifications screens — and re-run the analysis to
                  lift the level.
                </p>
              </div>
            ) : null}
            <p className="hint block">
              Upload every file in the package before running analysis: precedence is
              resolved across documents, so a missing amendment changes the answer.
              Accepted: PDF, DOCX, XLSX, PPTX, CSV, TXT, MD, up to 200&nbsp;MB each.
              Macro-enabled and legacy Office formats (.docm, .doc, .xls) are refused
              rather than stripped.
            </p>
            {documents.isLoading ? (
              <Empty text="Loading documents…" />
            ) : documents.error ? (
              <Empty text={documents.error.message} />
            ) : (documents.data ?? []).length === 0 ? (
              <Empty text="No tender documents on this bid yet." />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Version</th>
                      <th>State</th>
                      <th>Pages</th>
                      <th>Blocks</th>
                      <th>Chunks</th>
                      <th>Size</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {(documents.data ?? []).map((version) => (
                      <tr key={version.id}>
                        <td>v{version.version_no ?? "?"}</td>
                        <td>
                          {version.state ?? "—"}
                          {version.parse_error && (
                            <em className="hint"> {version.parse_error}</em>
                          )}
                        </td>
                        <td>{version.page_count ?? "—"}</td>
                        {/* Blocks is the number to watch: a long document that
                            parsed into very few blocks did not really parse. */}
                        <td>{version.block_count ?? "—"}</td>
                        <td>{version.chunk_count ?? "—"}</td>
                        <td>
                          {version.byte_size ? `${Math.round(version.byte_size / 1024)} KB` : "—"}
                        </td>
                        <td className="row-action">
                          {/* A parser improvement does not reach documents that
                              are already stored; without this the only way to
                              benefit is to upload the same file again and end
                              up with a duplicate in the bid. */}
                          <button
                            className="ghost"
                            onClick={() =>
                              action("Re-parse", () =>
                                api(`/bids/${bidId}/documents/${version.id}/reparse`, {
                                  method: "POST",
                                }),
                              )
                            }
                          >
                            Re-parse
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>
    </>
  );
}

/** What the analysis pass actually found.
 *
 *  Reported rather than announced: "completed" tells a reviewer nothing, and
 *  the difference between "0 requirements because the package has no text" and
 *  "0 requirements because extraction never ran" is the whole question. The
 *  model-call count is on screen for exactly that reason.
 */
function AnalysisSummary({ result }: { result: AnalysisOut }) {
  const requirements = result.requirements ?? {};
  const precedence = result.precedence ?? {};
  const figures: [string, string][] = [
    ["Requirements found", String(requirements.requirements_created ?? 0)],
    ["Blocks eligible", String(requirements.blocks_considered ?? 0)],
    ["Clauses resolved", `${precedence.resolved ?? 0} of ${precedence.clauses ?? 0}`],
    ["Deliverables", String(result.deliverables ?? 0)],
    ["Ambiguities", String(result.ambiguities ?? 0)],
    ["Scope confidence", String(result.scope_confidence ?? "—")],
    ["Model calls", String(requirements.model_calls ?? 0)],
  ];
  // Requirement extraction reads only blocks that won a clause resolution, so
  // a zero here is almost always upstream: no clause references were found in
  // the document, so no clauses were resolved, so no blocks were eligible and
  // the model was never called. Naming that chain is the difference between a
  // diagnosable result and an unexplained zero.
  const noModel = (requirements.model_calls ?? 0) === 0;
  const noClauses = (precedence.clauses ?? 0) === 0;
  const noBlocks = (requirements.blocks_considered ?? 0) === 0;
  return (
    <div className="analysis-summary">
      <div className="analysis-figures">
        {figures.map(([label, value]) => (
          <div key={label}>
            <small>{label}</small>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
      {noModel && noClauses && (
        <p className="hint block warn">
          <b>No clause references were found, so no requirements could be extracted.</b>{" "}
          Requirement extraction reads only blocks that won a clause resolution, so
          with zero clauses nothing was eligible and the model was never called. If
          this document was uploaded before the parser was last improved, press{" "}
          <b>Re-parse</b> on its row below and run the analysis again — parsing
          happens at upload time, so an old document keeps the old parser&rsquo;s
          output until it is re-parsed. A long document showing very few blocks is
          the tell.
        </p>
      )}
      {noModel && !noClauses && noBlocks && (
        <p className="hint block">
          Clauses were found but none passed the confidence threshold for
          requirement extraction, so no blocks were eligible.
        </p>
      )}
      {noModel && !noClauses && !noBlocks && (
        <p className="hint block">
          Blocks were eligible but no model call was made — the gateway is likely
          running the null provider because DEEPSEEK_API_KEY is unset.
        </p>
      )}
      {!result.detailed_drafting_allowed && (
        <p className="hint block">
          Detailed drafting stays blocked until scope confidence rises. That gate is
          deliberate: drafting against a package the system does not yet understand
          produces confident answers to the wrong questions. The specific conditions
          are listed below.
        </p>
      )}
    </div>
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
