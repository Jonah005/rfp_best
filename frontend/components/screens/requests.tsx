"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * Incoming cross-department requests.
 *
 * The three resolutions are offered in least-privilege order, and the UI says
 * plainly what each one grants. That ordering is the whole point: answering
 * gives the requester the answer and no access at all, sharing evidence gives
 * one narrow task-scoped grant per item, and granting access is the widest and
 * still expires. Presenting them as equivalent buttons would quietly undo the
 * design.
 *
 * Rejecting is a first-class outcome and requires a reason, because "no" is
 * information the requester needs in order to find another route.
 */

type Request = {
  id: string;
  request_type: string;
  request_text: string;
  business_reason: string;
  state: string;
  origin_task_id: string;
  target_department_id: string;
  resolution_text: string | null;
  rejection_reason: string | null;
  created_at: string;
  requested_resource_type?: string | null;
  requested_resource_id?: string | null;
};

type Resolution = {
  state: string;
  asked: string;
  business_reason: string;
  resolution_text: string | null;
  rejection_reason: string | null;
  grants_created: {
    id: string;
    resource_type: string;
    permission: string;
    expires_at: string;
    revoked_at: string | null;
  }[];
};

const OPEN = new Set(["PENDING", "ACKNOWLEDGED"]);


type EvidenceHit = { id: string; excerpt: string };

export function Requests({
  action,
  focusRequestId,
}: {
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
  /** Arrived from a notification: open this request rather than none. */
  focusRequestId?: string;
}) {
  const [openOnly, setOpenOnly] = useState(true);
  const [selected, setSelected] = useState<Request | null>(null);
  const [answer, setAnswer] = useState("");
  const [reason, setReason] = useState("");
  const [shareReason, setShareReason] = useState("");
  const [grantReason, setGrantReason] = useState("");
  const [picked, setPicked] = useState<EvidenceHit[]>([]);
  const [search, setSearch] = useState("");
  const hits = useQuery({
    queryKey: ["evidence-search", search],
    queryFn: () => api<EvidenceHit[]>(`/knowledge/evidence?q=${encodeURIComponent(search.trim())}`),
    enabled: search.trim().length >= 3,
  });

  const inbox = useQuery({
    queryKey: ["requests-inbox", openOnly],
    queryFn: () => api<Request[]>(`/requests/inbox?open_only=${openOnly}`),
  });

  useEffect(() => {
    // A linked request may be resolved already, and the list defaults to open
    // ones -- so widen the filter rather than silently selecting nothing.
    if (!focusRequestId) return;
    const found = inbox.data?.find((row) => row.id === focusRequestId);
    if (found) setSelected(found);
    else if (inbox.data && openOnly) setOpenOnly(false);
  }, [focusRequestId, inbox.data, openOnly]);

  const resolution = useQuery({
    queryKey: ["request-resolution", selected?.id],
    queryFn: () => api<Resolution>(`/requests/${selected!.id}/resolution`),
    enabled: Boolean(selected && !OPEN.has(selected.state)),
  });

  function clearForms() {
    setAnswer("");
    setReason("");
    setShareReason("");
    setGrantReason("");
    setPicked([]);
    setSearch("");
  }

  return (
    <>
      <section className="toolbar">
        <label>
          <input
            type="checkbox"
            checked={openOnly}
            onChange={(event) => setOpenOnly(event.target.checked)}
          />
          Open requests only
        </label>
      </section>

      <div className="workspace-split">
        <section className="panel">
          <div className="panel-head">
            <div>
              <p className="kicker">ASKED OF THIS DEPARTMENT</p>
              <h2>Inbox</h2>
            </div>
            <span className="pill">{inbox.data?.length ?? 0}</span>
          </div>
          {inbox.isLoading ? (
            <Empty text="Loading requests…" />
          ) : inbox.error ? (
            <Empty text={(inbox.error as Error).message} />
          ) : !inbox.data?.length ? (
            <Empty text="Nothing has been asked of this department." />
          ) : (
            <ul className="request-list">
              {inbox.data.map((request) => (
                <li key={request.id}>
                  <button
                    className={selected?.id === request.id ? "on" : ""}
                    onClick={() => {
                      setSelected(request);
                      clearForms();
                    }}
                  >
                    <span className="row-top">
                      <strong>{request.request_type.replaceAll("_", " ")}</strong>
                      <em className={OPEN.has(request.state) ? "open" : ""}>
                        {request.state.toLowerCase()}
                      </em>
                    </span>
                    <span className="ask-text">{request.request_text}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <div>
              <p className="kicker">LEAST PRIVILEGE FIRST</p>
              <h2>Resolve</h2>
            </div>
          </div>

          {!selected ? (
            <Empty text="Select a request to see what was asked and why." />
          ) : (
            <div className="resolve-body">
              <h4>Asked</h4>
              <p>{selected.request_text}</p>
              <h4>Business reason</h4>
              <p className="muted-line">{selected.business_reason}</p>

              {OPEN.has(selected.state) ? (
                <>
                  <button
                    className="ghost"
                    onClick={() =>
                      action("Acknowledgement", () =>
                        api(`/requests/${selected.id}/acknowledge`, {
                          method: "POST",
                          body: "{}",
                        }),
                      )
                    }
                  >
                    Acknowledge (seen, not yet answered)
                  </button>

                  <div className="option">
                    <h4>Evidence to use (optional, for options 1–3)</h4>
                    <p className="grant-note">
                      Search approved evidence and tick what should leave your department. Each item
                      chosen becomes a narrow, expiring grant to the asking task only.
                    </p>
                    <input
                      value={search}
                      onChange={(event) => setSearch(event.target.value)}
                      placeholder="Find evidence to attach (type 3+ letters, e.g. licence)"
                    />
                    {search.trim().length >= 3 && (
                      <ul className="evidence-hits">
                        {(hits.data ?? []).map((hit) => (
                          <li key={hit.id}>
                            <label>
                              <input
                                type="checkbox"
                                checked={picked.some((p) => p.id === hit.id)}
                                onChange={(event) =>
                                  setPicked((list) =>
                                    event.target.checked
                                      ? [...list, hit]
                                      : list.filter((p) => p.id !== hit.id),
                                  )
                                }
                              />{" "}
                              {hit.excerpt.slice(0, 160)}
                              {hit.excerpt.length > 160 ? "…" : ""}
                            </label>
                          </li>
                        ))}
                        {hits.data && !hits.data.length && <li className="muted">No approved evidence matches.</li>}
                      </ul>
                    )}
                    {picked.length > 0 && <p className="grant-note">{picked.length} evidence item(s) attached.</p>}
                  </div>

                  <div className="option">
                    <h4>1 · Provide an answer</h4>
                    <p className="grant-note">
                      Write the answer. Nothing is attached unless you chose evidence above, and an
                      answer with nothing attached grants no access at all.
                    </p>
                    <textarea
                      value={answer}
                      onChange={(event) => setAnswer(event.target.value)}
                      placeholder="The answer, in words."
                    />
                    <button
                      disabled={!answer.trim()}
                      onClick={() =>
                        action("Answer", () =>
                          api(`/requests/${selected.id}/answer`, {
                            method: "POST",
                            body: JSON.stringify({
                              resolution_text: answer,
                              evidence_ids: picked.map((p) => p.id),
                            }),
                          }),
                        )
                      }
                    >
                      {picked.length ? `Send answer with ${picked.length} evidence item(s)` : "Send answer"}
                    </button>
                  </div>

                  <div className="option">
                    <h4>2 · Share evidence only</h4>
                    <p className="grant-note">
                      No written answer: the chosen evidence is shared with the asking task and
                      nothing else. Use this when the documents are the answer.
                    </p>
                    <input
                      value={shareReason}
                      onChange={(event) => setShareReason(event.target.value)}
                      placeholder="Why this evidence answers the request"
                    />
                    <button
                      disabled={!picked.length || shareReason.trim().length < 3}
                      onClick={() =>
                        action("Evidence sharing", () =>
                          api(`/requests/${selected.id}/share-evidence`, {
                            method: "POST",
                            body: JSON.stringify({
                              evidence_ids: picked.map((p) => p.id),
                              reason: shareReason,
                            }),
                          }),
                        )
                      }
                    >
                      {picked.length ? `Share ${picked.length} evidence item(s)` : "Choose evidence above"}
                    </button>
                  </div>

                  <div className="option">
                    <h4>3 · Grant task access</h4>
                    <p className="grant-note">
                      The widest option: the asking task can read the resource itself, not just an
                      answer. It is still limited to that one task and it expires. Use it when
                      they need to see the document, and answering is not enough.
                    </p>
                    {selected.requested_resource_type && selected.requested_resource_id ? (
                      <p className="muted-line">
                        They asked for: {selected.requested_resource_type.replaceAll("_", " ").toLowerCase()}{" "}
                        <code>{selected.requested_resource_id.slice(0, 8)}</code>
                      </p>
                    ) : (
                      <p className="muted-line">
                        {picked.length === 1
                          ? "Access will be granted to the one evidence item chosen above."
                          : "Choose exactly one evidence item above to grant access to it."}
                      </p>
                    )}
                    <input
                      value={grantReason}
                      onChange={(event) => setGrantReason(event.target.value)}
                      placeholder="Why this task needs to read it"
                    />
                    <button
                      disabled={
                        grantReason.trim().length < 3 ||
                        !(
                          (selected.requested_resource_type && selected.requested_resource_id) ||
                          picked.length === 1
                        )
                      }
                      onClick={() =>
                        action("Access grant", () =>
                          api(`/requests/${selected.id}/grant-access`, {
                            method: "POST",
                            body: JSON.stringify({
                              resource_type: selected.requested_resource_type ?? "EVIDENCE",
                              resource_id: selected.requested_resource_id ?? picked[0].id,
                              reason: grantReason,
                              permission: "READ",
                            }),
                          }),
                        )
                      }
                    >
                      Grant access to this task
                    </button>
                  </div>

                  <div className="option">
                    <h4>4 · Reject</h4>
                    <p className="grant-note">
                      A reason is required; the origin task becomes blocked, not
                      quietly in progress.
                    </p>
                    <textarea
                      value={reason}
                      onChange={(event) => setReason(event.target.value)}
                      placeholder="Why you cannot answer this"
                    />
                    <button
                      className="ghost"
                      disabled={reason.trim().length < 3}
                      onClick={() =>
                        action("Rejection", () =>
                          api(`/requests/${selected.id}/reject`, {
                            method: "POST",
                            body: JSON.stringify({ reason }),
                          }),
                        )
                      }
                    >
                      Reject
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <h4>Outcome</h4>
                  <p>
                    <span className="badge good">{selected.state.toLowerCase()}</span>
                  </p>
                  <p className="muted-line">
                    {resolution.data?.resolution_text ??
                      resolution.data?.rejection_reason ??
                      "—"}
                  </p>
                  <h4>Access this created</h4>
                  {!resolution.data?.grants_created.length ? (
                    <p className="muted-line">
                      None. Nothing was granted to resolve this request.
                    </p>
                  ) : (
                    <ul className="grant-list">
                      {resolution.data.grants_created.map((grant) => (
                        <li key={grant.id}>
                          <strong>{grant.resource_type}</strong> · {grant.permission} ·
                          expires {new Date(grant.expires_at).toLocaleString()}
                          {grant.revoked_at ? " · revoked" : ""}
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </div>
          )}
        </section>
      </div>
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
