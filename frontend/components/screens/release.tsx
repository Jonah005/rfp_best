"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { ConsistencyCheck } from "./extras";

/**
 * Release and package.
 *
 * Findings are split by waivability, because "can this be waived at all" is the
 * first question a release manager asks. A NON_WAIVABLE finding is shown with
 * no exception control at all -- offering a disabled button would imply the
 * route exists and is merely unavailable to you, which is the wrong idea to
 * give someone under submission pressure.
 *
 * Submit and approve are deliberately separate controls: the same person
 * cannot do both, and the server refuses it if they try.
 */

type Package = {
  id: string;
  version_no: number;
  state: string;
  output_mode: string;
  submitted_by: string | null;
  released_at: string | null;
  immutable: boolean;
};

type Finding = {
  id: string;
  check_code: string;
  severity: string;
  waivability: string;
  message: string;
  resolved_at: string | null;
};

type Report = { waivable: Finding[]; non_waivable: Finding[] };

export function Release({
  bidId,
  action,
}: {
  bidId: string;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
}) {
  const [packageId, setPackageId] = useState("");
  const [justification, setJustification] = useState("");

  const packages = useQuery({
    queryKey: ["packages", bidId],
    queryFn: () => api<Package[]>(`/bids/${bidId}/packages`),
    enabled: Boolean(bidId),
  });

  const current =
    packages.data?.find((item) => item.id === packageId) ?? packages.data?.[0] ?? null;

  const report = useQuery({
    queryKey: ["validation", current?.id],
    queryFn: () => api<Report>(`/packages/${current!.id}/validation`),
    enabled: Boolean(current),
  });

  if (!bidId) return <Empty text="Select an active bid to assemble a package." />;

  const openNonWaivable = (report.data?.non_waivable ?? []).filter((f) => !f.resolved_at);
  const openWaivable = (report.data?.waivable ?? []).filter((f) => !f.resolved_at);

  return (
    <>
      <section className="toolbar">
        <ConsistencyCheck bidId={bidId} />
        <button
          onClick={() =>
            action("Structure derivation", () =>
              api(`/bids/${bidId}/structure/derive`, { method: "POST", body: "{}" }),
            )
          }
        >
          Derive structure
        </button>
        <button
          className="secondary"
          onClick={() =>
            action("Document assembly", () =>
              api(`/bids/${bidId}/assemble`, {
                method: "POST",
                body: JSON.stringify({ output_mode: "REVIEW" }),
              }),
            )
          }
        >
          Assemble DOCX
        </button>
        <button
          className="ghost"
          onClick={() =>
            action("Package creation", () =>
              api(`/bids/${bidId}/packages`, {
                method: "POST",
                body: JSON.stringify({ output_mode: "REVIEW" }),
              }),
            )
          }
        >
          Create package
        </button>
        <span className="spacer" />
        <label>
          Package
          <select value={current?.id ?? ""} onChange={(event) => setPackageId(event.target.value)}>
            {packages.data?.map((item) => (
              <option key={item.id} value={item.id}>
                v{item.version_no} · {item.output_mode} · {item.state}
              </option>
            ))}
          </select>
        </label>
      </section>

      {!current ? (
        <Empty text="No package yet. Derive the structure, assemble, then create one." />
      ) : (
        <>
          <section className="panel">
            <div className="panel-head">
              <div>
                <p className="kicker">DETERMINISTIC GATES</p>
                <h2>
                  Package v{current.version_no}{" "}
                  <span className={`badge ${current.immutable ? "good" : "muted"}`}>
                    {current.state.replaceAll("_", " ").toLowerCase()}
                  </span>
                </h2>
              </div>
              <div className="panel-actions">
                <button
                  disabled={current.immutable}
                  onClick={() =>
                    action("Validation", () =>
                      api(`/packages/${current.id}/validate`, { method: "POST", body: "{}" }),
                    )
                  }
                >
                  Run validation
                </button>
                <button
                  className="secondary"
                  disabled={current.state !== "RELEASE_PENDING_APPROVALS" || Boolean(current.submitted_by)}
                  onClick={() =>
                    action("Release submission", () =>
                      api(`/packages/${current.id}/submit-for-release`, {
                        method: "POST",
                        body: "{}",
                      }),
                    )
                  }
                >
                  Submit for release
                </button>
                <button
                  className="ghost"
                  disabled={!current.submitted_by || current.immutable}
                  onClick={() =>
                    action("Release approval", () =>
                      api(`/packages/${current.id}/approve-release`, {
                        method: "POST",
                        body: "{}",
                      }),
                    )
                  }
                >
                  Approve release
                </button>
              </div>
            </div>
            <div className="context-body">
              {current.immutable && (
                <div className="notice good">
                  Released {current.released_at ? new Date(current.released_at).toLocaleString() : ""}
                  . This package is frozen; a change means a new version.
                </div>
              )}
              {current.submitted_by && !current.immutable && (
                <div className="notice good">
                  Submitted for release. A different person must approve it.
                </div>
              )}
            </div>
          </section>

          <div className="workspace-split">
            <section className="panel">
              <div className="panel-head">
                <div>
                  <p className="kicker">NO EXCEPTION PATH</p>
                  <h2>Hard blockers</h2>
                </div>
                <span className={`pill ${openNonWaivable.length ? "bad" : ""}`}>
                  {openNonWaivable.length}
                </span>
              </div>
              {!openNonWaivable.length ? (
                <Empty text="No hard blockers." />
              ) : (
                <ul className="finding-list">
                  {openNonWaivable.map((finding) => (
                    <li key={finding.id}>
                      <strong>{finding.check_code}</strong>
                      <p>{finding.message}</p>
                      <em>
                        These cannot be waived. The package cannot ship until the
                        underlying problem is fixed.
                      </em>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="panel">
              <div className="panel-head">
                <div>
                  <p className="kicker">WAIVABLE ON THE RECORD</p>
                  <h2>Blocking findings</h2>
                </div>
                <span className="pill">{openWaivable.length}</span>
              </div>
              {!openWaivable.length ? (
                <Empty text="Nothing waiting on an exception." />
              ) : (
                <ul className="finding-list">
                  {openWaivable.map((finding) => (
                    <li key={finding.id}>
                      <strong>{finding.check_code}</strong>
                      <p>{finding.message}</p>
                      <div className="decide">
                        <input
                          value={justification}
                          placeholder="Justification (recorded against the finding)"
                          onChange={(event) => setJustification(event.target.value)}
                        />
                        <button
                          disabled={justification.trim().length < 3}
                          onClick={() =>
                            action("Exception request", () =>
                              api(`/findings/${finding.id}/exception`, {
                                method: "POST",
                                body: JSON.stringify({ justification }),
                              }),
                            )
                          }
                        >
                          Request exception
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </>
      )}
    </>
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
