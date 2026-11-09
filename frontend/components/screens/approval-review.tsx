"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * The answer an approval step asks to be approved.
 *
 * An approver used to see an entity type and eight characters of an identifier.
 * Approving that is signing something unread. This shows the task, the
 * requirements the answer covers, every statement with the evidence or promise
 * behind it, and what changed since the previous version.
 */

type Evidence = {
  id: string;
  document: string | null;
  excerpt: string | null;
  excerpt_withheld: boolean;
  status: string;
  expiry_date: string | null;
  is_substantiated: boolean | null;
};

type Statement = {
  id: string;
  statement_class: string;
  text: string;
  change: "NEW" | "UNCHANGED";
  evidence: Evidence[];
  commitment: { required_approver_role: string; approval_status: string } | null;
};

type Review = {
  reviewable: boolean;
  note?: string;
  task?: { code: string; title: string } | null;
  artifact_title?: string;
  version_no?: number;
  previous_version_no?: number | null;
  revalidation_reason?: string | null;
  requirements?: { code: string; text: string; mandatory: boolean }[];
  statements?: Statement[];
  removed_statements?: { statement_class: string; text: string }[];
};

const LABEL: Record<string, string> = {
  EVIDENCE_BACKED_FACT: "Evidence-backed fact",
  TENDER_DERIVED: "From the tender",
  PROPOSED_COMMITMENT: "Proposed commitment",
  ASSUMPTION: "Assumption",
  HUMAN_PROVIDED_INPUT: "Provided by a contributor",
  DIAGRAM_DESCRIPTION: "Describes a diagram",
};

export function ApprovalReview({ approvalId }: { approvalId: string }) {
  const review = useQuery({
    queryKey: ["approval-review", approvalId],
    queryFn: () => api<Review>(`/approvals/${approvalId}/review`),
  });

  if (review.isLoading) return <p className="muted-line">Loading the response…</p>;
  if (review.error) return <p className="muted-line">{(review.error as Error).message}</p>;
  const r = review.data;
  if (!r) return null;
  if (!r.reviewable) return <p className="muted-line">{r.note}</p>;

  return (
    <div className="review">
      <p className="review-head">
        <strong>{r.task ? `${r.task.code} · ${r.task.title}` : r.artifact_title}</strong>
        <span>
          version {r.version_no}
          {r.previous_version_no ? ` (previous: ${r.previous_version_no})` : " (first version)"}
        </span>
      </p>
      {r.revalidation_reason && (
        <p className="review-warn">Sent back for re-checking: {r.revalidation_reason}</p>
      )}

      <h5>Requirements this answers</h5>
      {r.requirements?.length ? (
        <ul className="review-reqs">
          {r.requirements.map((q) => (
            <li key={q.code}>
              <code>{q.code}</code> {q.text}
              {q.mandatory && <span className="badge muted"> mandatory</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted-line">No requirements are linked to this answer.</p>
      )}

      <h5>The response</h5>
      {r.statements?.map((s) => (
        <div key={s.id} className={`review-statement ${s.change === "NEW" ? "is-new" : ""}`}>
          <div className="review-meta">
            <span className="badge muted">{LABEL[s.statement_class] ?? s.statement_class}</span>
            {r.previous_version_no && (
              <span className={`badge ${s.change === "NEW" ? "good" : "muted"}`}>
                {s.change === "NEW" ? "new or changed" : "unchanged"}
              </span>
            )}
          </div>
          <p>{s.text}</p>
          {s.evidence.map((e) => (
            <blockquote key={e.id}>
              <strong>{e.document ?? "Source document"}</strong>
              <span className="muted-line">
                {" "}
                · {e.status.replaceAll("_", " ").toLowerCase()}
                {e.expiry_date ? ` · expires ${e.expiry_date}` : ""}
                {e.is_substantiated === false ? " · NOT judged proof" : ""}
              </span>
              <p>{e.excerpt_withheld ? "Restricted source: excerpt withheld." : e.excerpt}</p>
            </blockquote>
          ))}
          {s.commitment && (
            <p className="muted-line">
              Promise needing {s.commitment.required_approver_role.replaceAll("_", " ")} approval —
              currently {s.commitment.approval_status.toLowerCase().replaceAll("_", " ")}.
            </p>
          )}
        </div>
      ))}

      {r.removed_statements && r.removed_statements.length > 0 && (
        <>
          <h5>Removed since the previous version</h5>
          {r.removed_statements.map((s, index) => (
            <p key={index} className="review-removed">
              {s.text}
            </p>
          ))}
        </>
      )}
    </div>
  );
}
