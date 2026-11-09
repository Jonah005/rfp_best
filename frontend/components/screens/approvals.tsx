"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { ApprovalReview } from "./approval-review";

/**
 * Approvals.
 *
 * Steps are shown in order, because "who still has to sign this, and in what
 * order" is the question the release gate asks. A pending step below an
 * undecided one is not actionable yet, and the screen says so rather than
 * offering a button that would be refused.
 */

type Approval = {
  id: string;
  entity_type: string;
  entity_id: string;
  step_no: number;
  required_role: string;
  state: string;
  comment: string | null;
  decided_at: string | null;
};

type Commitment = {
  id: string;
  text: string;
  required_approver_role: string;
  approval_status: string;
  approved_at: string | null;
};

export function Approvals({
  action,
  bidId,
}: {
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
  bidId?: string;
}) {
  const [comments, setComments] = useState<Record<string, string>>({});

  const approvals = useQuery({
    queryKey: ["approvals"],
    queryFn: () => api<Approval[]>("/approvals"),
  });

  // Proposed commitments -- a schedule, a staffing promise -- each need the
  // named role's approval before release. The server enforced it and nothing
  // on screen offered the approval, so every commitment blocked release.
  const commitments = useQuery({
    queryKey: ["commitments", bidId],
    queryFn: () => api<Commitment[]>(`/bids/${bidId}/commitments?approval_status=PENDING_APPROVAL`),
    enabled: Boolean(bidId),
  });

  const grouped = new Map<string, Approval[]>();
  for (const approval of approvals.data ?? []) {
    const key = `${approval.entity_type}:${approval.entity_id}`;
    grouped.set(key, [...(grouped.get(key) ?? []), approval]);
  }
  for (const steps of grouped.values()) steps.sort((a, b) => a.step_no - b.step_no);

  return (
    <>
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">PROMISES THE COMPANY WOULD MAKE</p>
          <h2>Proposed commitments</h2>
        </div>
        <span className="pill">{commitments.data?.length ?? 0} waiting</span>
      </div>
      {!bidId ? (
        <Empty text="Select a bid to see its proposed commitments." />
      ) : !commitments.data?.length ? (
        <Empty text="No proposed commitments are waiting for approval." />
      ) : (
        <ul className="stack">
          {commitments.data.map((commitment) => (
            <li key={commitment.id} className="option">
              <p>{commitment.text}</p>
              <p className="muted-line">Needs approval by {commitment.required_approver_role}</p>
              <div className="panel-actions">
                <button
                  onClick={() =>
                    action("Commitment approval", () =>
                      api(`/commitments/${commitment.id}/approve`, {
                        method: "POST",
                        body: JSON.stringify({ decision: "APPROVED" }),
                      }),
                    )
                  }
                >
                  Approve commitment
                </button>
                <button
                  className="ghost"
                  onClick={() =>
                    action("Commitment rejection", () =>
                      api(`/commitments/${commitment.id}/approve`, {
                        method: "POST",
                        body: JSON.stringify({
                          decision: "REJECTED",
                          comment: comments[commitment.id] || "Rejected in review.",
                        }),
                      }),
                    )
                  }
                >
                  Reject
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">ORDERED, ROLE-BASED</p>
          <h2>Approval chains</h2>
        </div>
        <span className="pill">{approvals.data?.length ?? 0} step(s)</span>
      </div>

      {approvals.isLoading ? (
        <Empty text="Loading approvals…" />
      ) : approvals.error ? (
        <Empty text={(approvals.error as Error).message} />
      ) : grouped.size === 0 ? (
        <Empty text="Nothing is waiting on an approval." />
      ) : (
        <div className="chain-body">
          {[...grouped.entries()].map(([key, steps]) => {
            const firstPending = steps.find((step) => step.state === "PENDING");
            return (
              <article className="chain" key={key}>
                <header>
                  <strong>{steps[0].entity_type.replaceAll("_", " ").toLowerCase()}</strong>
                  <code>{steps[0].entity_id.slice(0, 8)}</code>
                </header>
                <div className="chain-review">
                  <ApprovalReview approvalId={steps[0].id} />
                </div>
                <ol>
                  {steps.map((step) => {
                    const actionable = firstPending?.id === step.id;
                    return (
                      <li key={step.id} className={step.state.toLowerCase()}>
                        <span className="step-no">{step.step_no}</span>
                        <div className="step-body">
                          <strong>{step.required_role.replaceAll("_", " ")}</strong>
                          <span className={`badge ${badgeTone(step.state)}`}>
                            {step.state.toLowerCase()}
                          </span>
                          {step.comment && <p className="muted-line">{step.comment}</p>}

                          {actionable && (
                            <div className="decide">
                              <input
                                value={comments[step.id] ?? ""}
                                placeholder="Comment (required to reject)"
                                onChange={(event) =>
                                  setComments({ ...comments, [step.id]: event.target.value })
                                }
                              />
                              <button
                                onClick={() =>
                                  action("Approval", () =>
                                    api(`/approvals/${step.id}/decide`, {
                                      method: "POST",
                                      body: JSON.stringify({
                                        decision: "APPROVED",
                                        comment: comments[step.id] || null,
                                      }),
                                    }),
                                  )
                                }
                              >
                                Approve
                              </button>
                              <button
                                className="secondary"
                                disabled={!(comments[step.id] ?? "").trim()}
                                onClick={() =>
                                  action("Rejection", () =>
                                    api(`/approvals/${step.id}/decide`, {
                                      method: "POST",
                                      body: JSON.stringify({
                                        decision: "REJECTED",
                                        comment: comments[step.id],
                                      }),
                                    }),
                                  )
                                }
                              >
                                Reject
                              </button>
                            </div>
                          )}
                          {step.state === "PENDING" && !actionable && (
                            <p className="muted-line">
                              Waiting on step {firstPending?.step_no ?? "—"} first.
                            </p>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ol>
              </article>
            );
          })}
        </div>
      )}
    </section>
    </>
  );
}

function badgeTone(state: string) {
  if (state === "APPROVED") return "good";
  if (state === "REJECTED") return "bad";
  return "muted";
}

function Empty({ text }: { text: string }) {
  return (
    <div className="empty">
      <span>◌</span>
      <p>{text}</p>
    </div>
  );
}
