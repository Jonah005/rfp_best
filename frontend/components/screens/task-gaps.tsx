"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

type Action = (label: string, work: () => Promise<unknown>) => Promise<void>;

export type TaskGap = {
  id: string;
  missing: string;
  why_needed: string | null;
  requirement_codes?: string[];
  /** Which piece of its requirement this is about, as the AI read it. */
  part?: string | null;
  /** Missing parts the AI judged to say the same thing, folded under this one. */
  similar?: { id: string; missing: string; why_needed: string | null; part: string | null }[];
  status: string;
  assumption: string | null;
  department: string | null;
  owner_name: string | null;
  attached_by: string;
  include_in_response: boolean;
  answer_source: string | null;
  answered_by: string | null;
  answered_at: string | null;
  requests: { id: string; department: string | null; state: string; question: string; reply: string | null; reviewed?: boolean }[];
};
type GapList = { checked: boolean; items: TaskGap[] };
type Proposal = {
  assumption: string;
  based_on: { ref: string; kind: string; text: string }[];
  caution: string;
};

/** The gap being worked on in the chat, and where its proposal has got to. */
export type Resolving = {
  gap: TaskGap;
  state: "drafting" | "ready" | "failed";
  proposal?: Proposal;
  error?: string;
  /** What the last change asked for in the chat did to the wording. */
  note?: string;
};

const STATE_LABEL: Record<string, string> = {
  OPEN: "Open",
  SENT_TO_CLIENT: "Open",
  ANSWERED: "Assumption recorded",
};

const SOURCE_LABEL: Record<string, string> = {
  person: "written by a person",
  model: "drafted by the AI",
  department: "from a department's reply",
};

const WAITING = new Set(["PENDING", "ACKNOWLEDGED"]);

/** Where a gap stands, for the colour of its label. */
function standing(gap: TaskGap): { label: string; cls: string } {
  if (gap.status === "ANSWERED") return { label: STATE_LABEL.ANSWERED, cls: "answered" };
  const waiting = gap.requests.filter((r) => WAITING.has(r.state));
  if (waiting.length) {
    return { label: `Asked ${waiting.map((r) => r.department).join(", ")} · waiting`, cls: "asked" };
  }
  return { label: STATE_LABEL[gap.status] ?? gap.status, cls: "open" };
}

/**
 * What the RFP leaves out that this task depends on.
 *
 * The AI decides which gaps belong here, once, the first time the task is opened;
 * the answer is kept. A person can say a gap does not belong, and it stays away.
 * Nothing here blocks the task: a gap is settled by recording the assumption the
 * response will rest on.
 */
export function TaskGaps({
  taskId,
  onSelect,
  onUseReply,
  onAsk,
  action,
  selected,
}: {
  taskId: string;
  /** The person clicked a missing part: its chat opens beside the list. */
  onSelect: (gap: TaskGap) => void;
  /** Put a department's reply into the answer box as the wording to record. */
  onUseReply: (gap: TaskGap, text: string, department: string | null) => void;
  onAsk: (gap: TaskGap) => void;
  action: Action;
  selected?: string | null;
}) {
  const queryClient = useQueryClient();
  const key = ["task-gaps", taskId];
  const gaps = useQuery({ queryKey: key, queryFn: () => api<GapList>(`/tasks/${taskId}/gaps`) });
  const [checking, setChecking] = useState(false);
  useEffect(() => {
    if (!selected || !gaps.data) return;
    document
      .querySelector(`[data-gap="${selected}"]`)
      ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [selected, gaps.data]);
  const [note, setNote] = useState("");
  const checked = gaps.data?.checked;
  useEffect(() => {
    // First time this task is opened: ask the AI once, then keep the answer.
    if (checked !== false || checking) return;
    setChecking(true);
    api(`/tasks/${taskId}/gaps/check`, { method: "POST" })
      .catch(() => setNote("The AI could not check this task just now. It will try again next time."))
      .finally(() => {
        setChecking(false);
        void queryClient.invalidateQueries({ queryKey: key });
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checked, taskId]);

  const items = gaps.data?.items ?? [];
  const open = items.filter((g) => g.status !== "ANSWERED").length;
  // A part the AI tied to a requirement is shown under that requirement; the rest stay here.
  const [showLinked, setShowLinked] = useState(false);
  const linked = items.filter((g) => (g.requirement_codes?.length ?? 0) > 0);
  const visible = showLinked ? items : items.filter((g) => !g.requirement_codes?.length);

  async function recheck() {
    setChecking(true);
    setNote("");
    try {
      await api(`/tasks/${taskId}/gaps/check?force=true`, { method: "POST" });
    } catch {
      setNote("The AI could not check this task just now.");
    } finally {
      setChecking(false);
      await queryClient.invalidateQueries({ queryKey: key });
    }
  }

  return (
    <div className="task-gaps" id="task-gaps">
      <div className="task-gaps-head">
        <h4>
          Missing from the RFP
          {items.length ? ` (${open} open of ${items.length})` : ""}
        </h4>
        <button className="ghost" disabled={checking} onClick={() => void recheck()}>
          {checking ? "Checking…" : "Re-check"}
        </button>
      </div>
      {note && <p className="muted-line">{note}</p>}
      {checking && !items.length ? (
        <p className="muted-line">Reading the RFP&apos;s gaps against this task…</p>
      ) : !items.length ? (
        <p className="muted-line">
          Nothing in the RFP&apos;s gaps affects this task.
        </p>
      ) : (
        <>
        {linked.length > 0 && (
          <p className="gap-more">
            {linked.length} more {linked.length === 1 ? "sits" : "sit"} under the requirement{" "}
            {linked.length === 1 ? "it affects" : "they affect"}.{" "}
            <button className="ghost void-toggle" onClick={() => setShowLinked((v) => !v)}>
              {showLinked ? "Show only the others here" : "Show them here too"}
            </button>
          </p>
        )}
        <ul className="gap-list">
          {visible.map((gap) => (
            <GapCard
              key={gap.id}
              taskId={taskId}
              gap={gap}
              selected={selected}
              onSelect={onSelect}
              onUseReply={onUseReply}
              onAsk={onAsk}
              action={action}
            />
          ))}
        </ul>
        </>
      )}
    </div>
  );
}

/**
 * One missing part of the RFP, with everything a person can do about it.
 *
 * The same card is used in the list of missing parts and, for a part the AI linked to a
 * requirement, under that requirement -- so the person can chat about it, ask another
 * department or record an assumption from either place.
 */
export function GapCard({
  taskId,
  gap,
  selected,
  onSelect,
  onUseReply,
  onAsk,
  action,
}: {
  taskId: string;
  gap: TaskGap;
  selected?: string | null;
  onSelect: (gap: TaskGap) => void;
  onUseReply: (gap: TaskGap, text: string, department: string | null) => void;
  onAsk: (gap: TaskGap) => void;
  action: Action;
}) {
  const queryClient = useQueryClient();
  const key = ["task-gaps", taskId];
  async function settle(gap: TaskGap, status: string, label: string) {
    await action(label, async () => {
      await api(`/tasks/${taskId}/gaps/${gap.id}/settle`, {
        method: "POST",
        body: JSON.stringify({ status }),
      });
      await queryClient.invalidateQueries({ queryKey: key });
    });
  }

  async function setInclude(gap: TaskGap, include: boolean) {
    await action(include ? "Included in the response" : "Left out of the response", async () => {
      await api(`/tasks/${taskId}/gaps/${gap.id}/include`, {
        method: "POST",
        body: JSON.stringify({ include }),
      });
      await queryClient.invalidateQueries({ queryKey: key });
    });
  }

  async function separate(otherId: string) {
    await action("Shown separately", async () => {
      await api(`/tasks/${taskId}/gaps/${otherId}/separate`, { method: "POST" });
      await queryClient.invalidateQueries({ queryKey: key });
    });
  }

  async function notHere(gap: TaskGap) {
    await action("Gap removed from this task", async () => {
      await api(`/tasks/${taskId}/gaps/${gap.id}/detach`, { method: "POST" });
      await queryClient.invalidateQueries({ queryKey: key });
    });
  }

  const where = standing(gap);
            const editing = false;
            const replies = gap.requests.filter((r) => r.reply);
  return (
              <li
                key={gap.id}
                data-gap={gap.id}
                className={`${gap.status === "ANSWERED" ? "gap done" : "gap"}${selected === gap.id ? " selected" : ""}`}
                onClick={(e) => {
                  if ((e.target as HTMLElement).closest("button,input,label,textarea,a")) return;
                  onSelect(gap);
                }}
              >
                <div className="gap-top">
                  <strong>What is missing</strong>
                  <span className={`chip ${where.cls}`}>{where.label}</span>
                </div>
                {gap.part && <p className="gap-part">About: {gap.part}</p>}
                <p>{gap.missing}</p>
                {!!gap.similar?.length && (
                  <details className="gap-similar" onClick={(e) => e.stopPropagation()}>
                    <summary>
                      {gap.similar.length} similar {gap.similar.length === 1 ? "part" : "parts"} merged
                      into this one
                    </summary>
                    {gap.similar.map((x) => (
                      <div key={x.id}>
                        <p>{x.missing}</p>
                        <button className="ghost" onClick={() => void separate(x.id)}>
                          Not the same — show separately
                        </button>
                      </div>
                    ))}
                  </details>
                )}
                {gap.why_needed && (
                  <p className="gap-why">
                    <strong>Why this task needs it:</strong> {gap.why_needed}
                  </p>
                )}
                {gap.requests.map((r) => (
                  <div key={r.id} className="gap-asked">
                    <p>
                      <strong>Asked {r.department}:</strong> {r.question}
                    </p>
                    {r.reply ? (
                      <p>
                        <strong>Their reply:</strong> {r.reply}
                        {!r.reviewed && <span className="chip reply"> new</span>}
                      </p>
                    ) : (
                      <p className="muted-line">
                        {WAITING.has(r.state) ? "Waiting for their reply." : `Request ${r.state.toLowerCase()}.`}
                      </p>
                    )}
                  </div>
                ))}
                {gap.status === "ANSWERED" && gap.assumption && !editing && (
                  <div className="gap-assumption">
                    <p>
                      <strong>Our assumption:</strong> {gap.assumption}
                    </p>
                    <p className="gap-owner">
                      Recorded by {gap.answered_by ?? "someone"}
                      {gap.answer_source ? ` · ${SOURCE_LABEL[gap.answer_source] ?? gap.answer_source}` : ""}
                    </p>
                    <label className="gap-include">
                      <input
                        type="checkbox"
                        checked={gap.include_in_response}
                        onChange={(e) => void setInclude(gap, e.target.checked)}
                      />
                      Include in the response
                    </label>
                  </div>
                )}
                <p className="gap-owner">
                  {gap.department ?? "No department"}
                  {gap.owner_name ? ` · ${gap.owner_name}` : ""}
                </p>
                {!editing && (
                  <div className="gap-actions">
                    <button className={selected === gap.id ? "" : "ghost"} onClick={() => onSelect(gap)}>
                      {selected === gap.id ? "Chatting about this" : "Chat about this"}
                    </button>
                    <button className="ghost" onClick={() => onAsk(gap)}>
                      Ask another department
                    </button>
                    {replies.map((r) => (
                      <button
                        key={r.id}
                        className="ghost"
                        onClick={() => onUseReply(gap, r.reply ?? "", r.department)}
                      >
                        Use {r.department}&apos;s reply
                      </button>
                    ))}
                    {gap.status !== "ANSWERED" && (
                      <button
                        className="ghost"
                        onClick={() => void settle(gap, "CLOSED", "Gap closed — no impact")}
                      >
                        No impact — close
                      </button>
                    )}
                    <button className="ghost" onClick={() => void notHere(gap)}>
                      Not for this task
                    </button>
                  </div>
                )}
              </li>
  );
}

/**
 * The Copilot's proposed assumption for one gap, in the chat thread.
 *
 * The wording is the AI's draft and the person's to change. Nothing is recorded
 * until they press the button.
 */
export function GapAnswerCard({
  taskId,
  resolving,
  onClose,
  action,
  onText,
  title = "copilot · proposed assumption",
}: {
  taskId: string;
  resolving: Resolving;
  title?: string;
  onClose: () => void;
  action: Action;
  /** Tells the chat what the wording says now, so a request to change it starts from that. */
  onText?: (text: string) => void;
}) {
  const queryClient = useQueryClient();
  const [text, setText] = useState(resolving.proposal?.assumption ?? "");
  useEffect(() => {
    onText?.(text);
  }, [text, onText]);
  const [include, setInclude] = useState(true);
  useEffect(() => {
    if (resolving.proposal) setText(resolving.proposal.assumption);
  }, [resolving.proposal]);

  async function record() {
    await action("Assumption recorded", async () => {
      await api(`/tasks/${taskId}/gaps/${resolving.gap.id}/settle`, {
        method: "POST",
        body: JSON.stringify({
          status: "ANSWERED",
          assumption: text.trim(),
          // The AI's wording as drafted is "model"; once a person changes it, it is theirs.
          answer_source: text.trim() === resolving.proposal?.assumption ? "model" : "person",
          include_in_response: include,
        }),
      });
      await queryClient.invalidateQueries({ queryKey: ["task-gaps", taskId] });
      onClose();
    });
  }

  return (
    <article className="turn assistant gap-card">
      <header>
        <strong>{title}</strong>
      </header>
      <p className="gap-card-gap">
        <strong>Missing from the RFP:</strong> {resolving.gap.missing}
      </p>
      {resolving.state === "drafting" && (
        <p className="live-status">Drafting from this task&apos;s approved evidence…</p>
      )}
      {resolving.state === "failed" && <p className="notice bad">{resolving.error}</p>}
      {resolving.state === "ready" && resolving.proposal && (
        <>
          <label className="gap-edit">
            Edit the wording, then record it as the company&apos;s assumption
            <textarea rows={4} value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <p className="muted-line">
            Or tell the chat below what to change, for example &quot;say we translate the Arabic
            ourselves&quot;, and this wording will be rewritten.
          </p>
          <label className="gap-include">
            <input type="checkbox" checked={include} onChange={(e) => setInclude(e.target.checked)} />
            Include in the response
          </label>
          {resolving.note && (
            <p className="gap-note">
              <strong>Changed:</strong> {resolving.note}
            </p>
          )}
          {resolving.proposal.based_on.length > 0 ? (
            <details className="gap-evidence">
              <summary>
                <strong>Based on</strong> {resolving.proposal.based_on.length} approved source
                {resolving.proposal.based_on.length === 1 ? "" : "s"}
              </summary>
              <ul>
                {resolving.proposal.based_on.map((b) => (
                  <li key={b.ref}>
                    [{b.ref}] {b.text.replace(/\s+/g, " ").slice(0, 140)}
                    {b.text.length > 140 ? "…" : ""}
                  </li>
                ))}
              </ul>
            </details>
          ) : (
            <p className="gap-evidence">
              <strong>Based on:</strong> no approved evidence in this task covers it.
            </p>
          )}
          {resolving.proposal.caution && (
            <p className="gap-evidence">
              <strong>Check first:</strong> {resolving.proposal.caution}
            </p>
          )}
        </>
      )}
      <div className="gap-actions">
        <button disabled={resolving.state !== "ready" || !text.trim()} onClick={() => void record()}>
          Record as our assumption
        </button>
        <button className="ghost" onClick={onClose}>
          Dismiss
        </button>
      </div>
    </article>
  );
}
