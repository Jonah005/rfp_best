"use client";

import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DraftEditor } from "./draft-editor";
import { InlineDiagram } from "./diagrams";
import { ProposalView } from "./proposal-view";
import { api, streamJob } from "@/lib/api";
import { TaskEvidence, TaskTimeline } from "./extras";
import { GapAnswerCard, GapCard, TaskGaps, type Resolving, type TaskGap } from "./task-gaps";
import { PositionCard } from "./position-card";
import { RichText } from "./rich-text";
import {
  type Decision,
  RequirementsTab,
  SectionsTab,
  focusKey,
  focusLabel,
  focusPayload,
  type Focus,
} from "./task-tabs";

/**
 * Task workspace and Copilot.
 *
 * Two things this screen is careful about.
 *
 * A conversation is created by an explicit POST, never by opening the page. A
 * read that writes makes conversation counts meaningless, so when none exists
 * the screen offers a button rather than quietly making one.
 *
 * Asking returns 202 with a job, and the answer arrives on an SSE stream. The
 * message is committed server-side before the job completes, so if the stream
 * drops the answer is still there on the next refetch -- the stream carries the
 * live update, not the result.
 */

type Task = {
  id: string;
  code: string;
  title: string;
  state: string;
  owner_user_id: string | null;
  // Who handed this task to the viewer by name, when someone did.
  assigned_by?: string | null;
};
/**
 * Who this viewer may put on this task, answered by the server.
 *
 * The rule needs the task's department, that department's head and its
 * membership -- and a department head may not read the department list at all
 * (`org:read` is an administrator's). Rather than re-derive it here, badly and
 * in a second place, the screen renders what /assignable says. An empty
 * candidate list is a normal answer: it means show the owner, offer nothing.
 */
type Assignable = {
  scope: "DEPARTMENT" | "UNHEADED" | "NONE";
  department: string | null;
  reason: string;
  candidates: {
    id: string;
    display_name: string;
    email: string;
    departments?: string[];
    roles?: string[];
    positions?: { department: string; position: "Head" | "Team member" }[];
  }[];
};
type Conversation = { id: string; task_id: string; message_count: number };
type Message = {
  id: string;
  role: string;
  content: string;
  cited_chunk_ids: string[] | null;
  cited_evidence_ids: string[] | null;
  cited_request_ids: string[] | null;
  model_id: string | null;
  created_at: string;
  retrieval_trace: {
    outcome?: string;
    suggested_departments?: Target[];
    request_options?: string[];
    // Every ref the answer actually cited, recorded since before the columns
    // existed. It is what lets an older message report its sourcing honestly.
    sources_cited?: string[];
    // Set when the reply was about a diagram: the picture is shown in the thread.
    diagram?: { action?: string; diagram_id?: string; version_no?: number };
    diagrams?: { diagram_id?: string; version_no?: number }[];
  } | null;
};
type Target = { id: string; code: string; name: string; has_members?: boolean; why?: string; question?: string; need?: string };
type Statement = {
  id: string;
  order_index: number;
  statement_class: string;
  text: string;
  approval_state: string;
  evidence_ids: string[] | null;
  source_locator: Record<string, unknown> | null;
  required_approver_role?: string | null;
  section?: string | null;
  diagram_id?: string | null;
  diagram_version_no?: number | null;
  out_of_date?: boolean;
  out_of_date_reason?: string | null;
  paragraph_no?: number | null;
  requirement_codes?: string[] | null;
};
type Writing = { done: number; total: number; failed: number; current?: string; position?: number };
// Where a task's write-up stands, asked of the server so a reload or a second tab agrees.
type WriteupState = {
  running: boolean;
  progress: Writing | null;
  last: { failed?: { section: string; error: string }[] } | null;
  has_writeup: boolean;
  missing: { diagram_id: string; title: string }[];
  finalized: { at: string; unchanged: boolean } | null;
  // Which diagrams are in the written response, which are left out, which await a choice.
  diagrams?: {
    diagram_id: string;
    title: string;
    state: "written" | "pending" | "left_out";
    version_no?: number;
    parts?: number;
  }[];
  requirements_written?: boolean;
  // For every requirement: does the draft really answer it, and if not, why not.
  coverage?: Coverage[];
  coverage_counts?: Record<string, number>;
  // Parts of the draft that are waiting on another department.
  holds?: Hold[];
};
type Hold = {
  id: string;
  requirement_code: string | null;
  section: string | null;
  department: string | null;
  note: string | null;
  reply_arrived: boolean;
  reply: string | null;
  placeholder: string;
};
type Coverage = {
  code: string;
  text: string;
  state: "answered" | "conditional" | "restated" | "unanswered" | "not_ours" | "set_aside";
  needs: string | null;
  sections: string[];
  diagram_ids?: string[];
};
type Version = {
  id: string;
  version_no: number;
  state: string;
  created_at: string;
  statements: Statement[];
};
type TaskDetail = {
  task: Task & { blocker_note: string | null; retrieval_scope: Record<string, unknown> };
  requirements: {
    requirement_id: string;
    code: string;
    text: string | null;
    bid_readiness: string | null;
    evidence_status: string | null;
    asked_departments?: string[];
    position_status?: string | null;
    position_text?: string | null;
    replies?: { request_id: string; department: string | null; state: string; text: string }[];
  }[];
  open_requests: number;
};

export function Copilot({
  bidId,
  action,
  focusTaskId,
  openTarget,
}: {
  bidId: string;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
  /** Arrived from a notification: open this task rather than the first one. */
  focusTaskId?: string;
  /** From the to-do list: "req:<ids>", "gap:<id>" or "gaps" -- the exact part to open. */
  openTarget?: string;
}) {
  const queryClient = useQueryClient();
  const [taskId, setTaskId] = useState("");
  // A write-up running in the background: the Response draft fills in section by section.
  // Asked of the server, not remembered by this page, so a reload or a second tab agrees.
  const writeup = useQuery({
    queryKey: ["writeup", taskId],
    queryFn: () => api<WriteupState>(`/tasks/${taskId}/writeup`),
    enabled: Boolean(taskId),
    refetchInterval: (query) => (query.state.data?.running ? 1500 : false),
  });
  const writing: Writing | null = writeup.data?.running
    ? (writeup.data.progress ?? { done: 0, total: 0, failed: 0 })
    : null;
  const writeupSeen = `${writeup.data?.running ?? false}:${writeup.data?.progress?.done ?? 0}`;
  useEffect(() => {
    // Each finished section, and the moment the run ends, brings the draft up to date.
    void queryClient.invalidateQueries({ queryKey: ["artifact", taskId] });
    void queryClient.invalidateQueries({ queryKey: ["versions"] });
  }, [writeupSeen, taskId, queryClient]);
  // Which request the person clicked, and what to prefill it from. Null means
  // the dialog is closed.
  const [asking, setAsking] = useState<Seed | null>(null);
  // Which tab of the left panel is open, which requirements are ticked, and what the
  // chat is about. The focus is sent with every message, so the model is never left
  // to work out from the conversation which requirement or section is meant.
  const [tab, setTab] = useState<"requirements" | "sections">("requirements");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [focus, setFocus] = useState<Focus>({ kind: "task" });
  // Requirements being answered with no evidence behind them, shown as a card in the thread.
  const [drafting, setDrafting] = useState<{ ids: string[]; codes: string[]; seedWords?: string } | null>(null);
  // The missing part whose chat is open, and the wording the chat has drafted for each.
  const [selectedGap, setSelectedGap] = useState<string | null>(null);
  const [gapDrafts, setGapDrafts] = useState<
    Record<string, { proposal: NonNullable<Resolving["proposal"]>; note?: string }>
  >({});
  const openedFor = useRef("");
  // What the open assumption box says right now, so a change asked for in the chat starts from it.
  const draftText = useRef("");
  useEffect(() => {
    setSelectedGap(null);
    setGapDrafts({});
    draftText.current = "";
    setDrafting(null);
    setPicked(new Set());
    setFocus({ kind: "task" });
    setTab("requirements");
  }, [taskId]);
  // Opening a section shows its text in the written response.
  useEffect(() => {
    if (focus.kind !== "section") return;
    const target = Array.from(document.querySelectorAll<HTMLElement>("[data-section]")).find(
      (el) => el.dataset.section === focus.section,
    );
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [focus]);
  const tasks = useQuery({
    queryKey: ["bid-tasks", bidId],
    queryFn: () => api<Task[]>(`/bids/${bidId}/tasks/picker`),
    enabled: Boolean(bidId),
  });

  useEffect(() => {
    // A linked task wins over the first in the list, but only if it is on this
    // bid -- a stale link must fall back rather than select nothing and leave
    // the screen looking broken.
    if (focusTaskId && tasks.data?.some((task) => task.id === focusTaskId)) {
      setTaskId(focusTaskId);
      return;
    }
    if (!taskId && tasks.data?.[0]) setTaskId(tasks.data[0].id);
  }, [taskId, tasks.data, focusTaskId]);

  const detail = useQuery({
    queryKey: ["task-detail", taskId],
    queryFn: () => api<TaskDetail>(`/tasks/${taskId}`),
    enabled: Boolean(taskId),
  });

  // Arriving from the to-do list: open the exact requirement or missing part, once.
  useEffect(() => {
    if (!openTarget || !taskId || !detail.data) return;
    if (focusTaskId && focusTaskId !== taskId) return;
    const stamp = `${taskId}|${openTarget}`;
    if (openedFor.current === stamp) return;
    openedFor.current = stamp;
    if (openTarget.startsWith("req:")) {
      const ids = openTarget
        .slice(4)
        .split(",")
        .filter((id) => detail.data!.requirements.some((r) => r.requirement_id === id));
      if (!ids.length) return;
      const codes = detail.data.requirements
        .filter((r) => ids.includes(r.requirement_id))
        .map((r) => r.code);
      setTab("requirements");
      setPicked(new Set(ids));
      setFocus({ kind: "requirements", ids, label: `Requirements ${codes.join(", ")}` });
    } else {
      if (openTarget.startsWith("gap:")) setSelectedGap(openTarget.slice(4));
      setTimeout(
        () => document.querySelector(".cell-gaps")?.scrollIntoView({ behavior: "smooth", block: "start" }),
        300,
      );
    }
  }, [openTarget, taskId, detail.data, focusTaskId]);

  const gapList = useQuery({
    queryKey: ["task-gaps", taskId],
    queryFn: () => api<{ checked: boolean; items: TaskGap[] }>(`/tasks/${taskId}/gaps`),
    enabled: Boolean(taskId),
  });
  const chosenGap = gapList.data?.items.find((g) => g.id === selectedGap) ?? null;
  // The same three things happen whether a missing part is clicked in the list or under a requirement.
  const pickGap = (gap: TaskGap) => {
    draftText.current = "";
    setSelectedGap(gap.id);
  };
  const useGapReply = (gap: TaskGap, text: string, department: string | null) => {
    draftText.current = "";
    setSelectedGap(gap.id);
    setGapDrafts((prev) => ({
      ...prev,
      [gap.id]: {
        proposal: { assumption: text, based_on: [], caution: "" },
        note: `From ${department ?? "the department"}'s reply. Edit it, then record it.`,
      },
    }));
  };
  const askAboutGap = (gap: TaskGap) =>
    setAsking({
      kind: "REQUEST_ANSWER",
      question: `The RFP leaves this open: ${gap.missing} What should our response assume?`,
      reason: gap.why_needed ?? "",
      clarificationId: gap.id,
      departments: [],
    });
  const chosenDraft = chosenGap ? gapDrafts[chosenGap.id] : undefined;
  // The wording already recorded for the chosen missing part, kept as one object so the box
  // beside it does not reset what the person is typing.
  const recorded = useMemo(
    () =>
      chosenGap && chosenGap.status === "ANSWERED" && chosenGap.assumption
        ? { assumption: chosenGap.assumption, based_on: [], caution: "" }
        : null,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [chosenGap?.id, chosenGap?.status, chosenGap?.assumption],
  );

  const newReplies = (detail.data?.requirements ?? []).reduce((n, r) => n + (r.replies?.length ?? 0), 0);

  // The artifact is created on demand by the draft/submit buttons, so a 404
  // here is the normal "nothing drafted yet" state rather than an error.
  const artifact = useQuery({
    queryKey: ["artifact", taskId],
    queryFn: async () => {
      try {
        return await api<{ id: string }>(`/tasks/${taskId}/artifact`);
      } catch {
        return null;
      }
    },
    enabled: Boolean(taskId),
  });

  const versions = useQuery({
    queryKey: ["versions", artifact.data?.id],
    queryFn: () => api<Version[]>(`/artifacts/${artifact.data!.id}/versions`),
    enabled: Boolean(artifact.data?.id),
  });

  const decisions = useQuery({
    queryKey: ["positions", taskId],
    queryFn: () => api<Decision[]>(`/tasks/${taskId}/positions`),
    enabled: Boolean(taskId),
  });

  const targets = useQuery({
    queryKey: ["request-targets", taskId],
    queryFn: () => api<Target[]>(`/tasks/${taskId}/request-targets`),
    enabled: Boolean(taskId),
  });

  const assignable = useQuery({
    queryKey: ["assignable", taskId],
    queryFn: () => api<Assignable>(`/tasks/${taskId}/assignable`),
    enabled: Boolean(taskId),
  });

  // A 404 here means "no conversation yet", which is a normal state and not an
  // error: creation is an explicit action.
  const conversation = useQuery({
    queryKey: ["conversation", taskId],
    queryFn: async () => {
      try {
        return await api<Conversation>(`/tasks/${taskId}/conversation`);
      } catch {
        return null;
      }
    },
    enabled: Boolean(taskId),
  });

  const conversationId = conversation.data?.id;

  if (!bidId) return <Empty text="Select an active bid to open a task." />;

  const artifactId = detail.data?.task.id;

  return (
    <>
      <section className="toolbar">
        <label>
          Task
          <select value={taskId} onChange={(event) => setTaskId(event.target.value)}>
            <option value="">Select task</option>
            {tasks.data?.map((task) => (
              <option key={task.id} value={task.id}>
                {task.code} · {task.title}
                {task.assigned_by ? ` (assigned by ${task.assigned_by})` : ""}
              </option>
            ))}
          </select>
        </label>
        <Owner
          taskId={taskId}
          ownerId={detail.data?.task.owner_user_id ?? null}
          assignable={assignable.data}
          action={action}
        />
        <span className="spacer" />
        <button
          className="secondary"
          disabled={!artifactId}
          onClick={() =>
            action("DeepSeek draft", async () => {
              const artifact = await api<{ id: string }>(`/tasks/${taskId}/artifact`);
              return api(`/artifacts/${artifact.id}/generate-draft`, {
                method: "POST",
                body: JSON.stringify({
                  instruction:
                    "Draft a concise, compliant response using only the supplied requirements and current approved evidence.",
                }),
              });
            })
          }
        >
          Generate governed draft
        </button>
        <button
          className="ghost"
          disabled={!artifactId}
          onClick={() =>
            action("Artifact submission", async () => {
              const artifact = await api<{ id: string }>(`/tasks/${taskId}/artifact`);
              return api(`/artifacts/${artifact.id}/submit`, {
                method: "POST",
                body: JSON.stringify({ approval_roles: ["BID_MANAGER"] }),
              });
            })
          }
        >
          Submit for review
        </button>
      </section>

      {!taskId ? (
        <Empty text="Select a task to see its bounded context." />
      ) : (
        <>
          <div className="grid4">
            <section className="panel cell cell-gaps">
              <div className="panel-head slim">
                <div>
                  <p className="kicker">WHAT THE TENDER LEAVES OUT</p>
                  <h2>RFP missing parts</h2>
                </div>
              </div>
              <div className="cell-body">
                <TaskGaps
                    taskId={taskId}
                    selected={selectedGap}
                    onSelect={pickGap}
                    onUseReply={useGapReply}
                    onAsk={askAboutGap}
                    action={action}
                  />
              </div>
            </section>

            <ChatPane
              key={chosenGap?.id ?? "none"}
              className="cell-gaps-chat"
              kicker={chosenGap ? "CHAT ABOUT THIS MISSING PART" : "CHAT ABOUT THE MISSING PARTS"}
              title={chosenGap ? "Chat about this missing part" : "Chat for RFP missing parts"}
              taskId={taskId}
              conversationId={conversationId}
              focus={
                chosenGap
                  ? { kind: "gap", id: chosenGap.id, label: "This missing part" }
                  : { kind: "gaps" }
              }
              disabledText={
                chosenGap
                  ? undefined
                  : "Click a missing part on the left to chat about it. Each one has its own conversation, and coming back to one shows what you said before."
              }
              emptyText="Ask what we should assume, what the risk is, or whom to ask. Say “draft it” and the wording appears above to edit and record."
              placeholder={
                chosenDraft
                  ? "Tell me what to change in the wording above…"
                  : "Ask about this, or say “draft it”…"
              }
              setAsking={setAsking}
              action={action}
              pinned={
                chosenGap &&
                (chosenDraft ? (
                  <GapAnswerCard
                    key={`${chosenGap.id}-draft`}
                    taskId={taskId}
                    resolving={{
                      gap: chosenGap,
                      state: "ready",
                      proposal: chosenDraft.proposal,
                      note: chosenDraft.note,
                    }}
                    title={recorded ? "changing the recorded assumption" : "proposed assumption"}
                    onClose={() =>
                      setGapDrafts((prev) => {
                        const next = { ...prev };
                        delete next[chosenGap.id];
                        return next;
                      })
                    }
                    action={action}
                    onText={(t) => {
                      draftText.current = t;
                    }}
                  />
                ) : recorded ? (
                  <div className="recorded-bar">
                    <p>
                      <strong>Recorded:</strong> {recorded.assumption}
                    </p>
                    <button
                      className="ghost"
                      onClick={() =>
                        setGapDrafts((prev) => ({
                          ...prev,
                          [chosenGap.id]: { proposal: recorded, note: "Change it by hand or by telling the chat." },
                        }))
                      }
                    >
                      Change
                    </button>
                  </div>
                ) : (
                  <p className="muted-line">
                    {chosenGap.missing} Nothing is recorded for this yet. Talk it through below. When you want wording,
                    say “draft it”.
                  </p>
                ))
              }
              intercept={async (text) => {
                if (!chosenGap || !conversationId) return true;
                const out = await api<{
                  reply: string;
                  assumption: string | null;
                  based_on: NonNullable<Resolving["proposal"]>["based_on"];
                  caution: string;
                }>(`/tasks/${taskId}/gaps/${chosenGap.id}/chat`, {
                  method: "POST",
                  body: JSON.stringify({
                    message: text,
                    current: draftText.current,
                    conversation_id: conversationId,
                  }),
                });
                if (out.assumption) {
                  const assumption = out.assumption;
                  setGapDrafts((prev) => ({
                    ...prev,
                    [chosenGap.id]: {
                      proposal: { assumption, based_on: out.based_on, caution: out.caution },
                    },
                  }));
                }
                return true;
              }}
            />

            <section className="panel cell cell-tabs">
              <div className="tab-row" role="tablist">
                {(
                  [
                    ["requirements", "Requirements"],
                    ["sections", "Sections"],
                  ] as const
                ).map(([key, label]) => (
                  <button
                    key={key}
                    role="tab"
                    aria-selected={tab === key}
                    className={tab === key ? "on" : ""}
                    onClick={() => setTab(key)}
                  >
                    {label}
                  </button>
                ))}
                <span className="spacer" />
                {newReplies > 0 && (
                  <span className="pill reply-pill">
                    {newReplies} new repl{newReplies === 1 ? "y" : "ies"}
                  </span>
                )}
                {detail.data ? (
                  <span className="pill">{detail.data.task.state.replaceAll("_", " ")}</span>
                ) : null}
              </div>
              <div className="cell-body">
                {detail.data && (detail.data.task.blocker_note || detail.data.open_requests > 0) && (
                  <ul className="status-lines">
                    {detail.data.task.blocker_note && (
                      <li className="bad" title={detail.data.task.blocker_note}>
                        <span className="dot none" /> {detail.data.task.blocker_note}
                      </li>
                    )}
                    {detail.data.open_requests > 0 && (
                      <li>
                        <span className="dot waiting" /> {detail.data.open_requests} request(s) open with other departments
                      </li>
                    )}
                  </ul>
                )}
                {tab === "requirements" && (
                  <RequirementsTab
                    relatedCodes={chosenGap?.requirement_codes ?? []}
                    renderVoids={(code) => {
                      const mine = (gapList.data?.items ?? []).filter((g) =>
                        g.requirement_codes?.includes(code),
                      );
                      if (!mine.length) return null;
                      return (
                        <VoidList
                          gaps={mine}
                          taskId={taskId}
                          selected={selectedGap}
                          onSelect={pickGap}
                          onUseReply={useGapReply}
                          onAsk={askAboutGap}
                          action={action}
                        />
                      );
                    }}
                    rows={detail.data?.requirements ?? []}
                    picked={picked}
                    focusedIds={focus.kind === "requirements" ? focus.ids : []}
                    decisions={decisions.data ?? []}
                    onDraft={() => {
                      const rows = (detail.data?.requirements ?? []).filter((r) =>
                        picked.has(r.requirement_id),
                      );
                      setDrafting({
                        ids: rows.map((r) => r.requirement_id),
                        codes: rows.map((r) => r.code),
                      });
                    }}
                    onDraftFromReply={(row, text) => {
                      setDrafting({ ids: [row.requirement_id], codes: [row.code], seedWords: text });
                    }}
                    onReviewed={(requestIds) =>
                      void action("Reply marked as dealt with", async () => {
                        await api("/me/todo/reviewed", {
                          method: "POST",
                          body: JSON.stringify({ request_ids: requestIds }),
                        });
                        await queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] });
                        await queryClient.invalidateQueries({ queryKey: ["todo"] });
                      })
                    }
                    onRestore={(id) =>
                      void action("Requirement restored", async () => {
                        await api(`/tasks/${taskId}/positions/${id}/restore`, { method: "POST" });
                        await queryClient.invalidateQueries({ queryKey: ["positions", taskId] });
                      })
                    }
                    onToggle={(id) =>
                      setPicked((prev) => {
                        const next = new Set(prev);
                        if (next.has(id)) next.delete(id);
                        else next.add(id);
                        return next;
                      })
                    }
                    onClear={() => setPicked(new Set())}
                    onChat={() => {
                      const rows = (detail.data?.requirements ?? []).filter((r) =>
                        picked.has(r.requirement_id),
                      );
                      setFocus({
                        kind: "requirements",
                        ids: rows.map((r) => r.requirement_id),
                        label: `Requirements ${rows.map((r) => r.code).join(", ")}`,
                      });
                    }}
                    onAsk={() => {
                      const rows = (detail.data?.requirements ?? []).filter((r) =>
                        picked.has(r.requirement_id),
                      );
                      setAsking({
                        kind: "REQUEST_ANSWER",
                        question: `For ${rows.map((r) => r.code).join(", ")}: `,
                        departments: [],
                        requirementIds: rows.map((r) => r.requirement_id),
                      });
                    }}
                  />
                )}
                {tab === "sections" && (
                  <SectionsTab
                    statements={versions.data?.length ? versions.data[versions.data.length - 1].statements : []}
                    writing={writing}
                    active={focus.kind === "section" ? focus.section : null}
                    onPick={(section) => setFocus({ kind: "section", section })}
                    onGenerate={() =>
                      action("Writing the response", async () => {
                        await api(`/tasks/${taskId}/writeup/all`, { method: "POST" });
                        await queryClient.invalidateQueries({ queryKey: ["writeup", taskId] });
                      })
                    }
                  />
                )}
              </div>
            </section>

            <ChatPane
              className="cell-this-chat"
              kicker="ANSWERS ONLY FROM AUTHORIZED CONTEXT"
              title="Chat for this"
              taskId={taskId}
              conversationId={conversationId}
              focus={focus}
              onBackToTask={() => setFocus({ kind: "task" })}
              onPictureSent={() => setFocus({ kind: "task" })}
              emptyText={
                focus.kind === "task"
                  ? "Ask something answerable from this task's requirements and the company evidence it is entitled to see."
                  : `This chat is about ${focusLabel(focus)} only. Ask what you need to know or change about it.`
              }
              placeholder={
                focus.kind === "task"
                  ? "Ask about this task, or ask for a diagram…"
                  : `Ask about ${focusLabel(focus).toLowerCase()}…`
              }
              allowPicture
              quick={[
                "Which diagrams does this task need?",
                "Draw the process flow",
                "Draw the solution architecture",
                "Write up the diagrams in the response",
              ]}
              setAsking={setAsking}
              action={action}
              cards={
                drafting && (
                  <PositionCard
                    key={drafting.ids.join(",") + (drafting.seedWords ?? "")}
                    seedWords={drafting.seedWords}
                    taskId={taskId}
                    ids={drafting.ids}
                    codes={drafting.codes}
                    onClose={() => setDrafting(null)}
                    onDiscuss={() => {
                      setFocus({
                        kind: "requirements",
                        ids: drafting.ids,
                        label: `Requirements ${drafting.codes.join(", ")}`,
                      });
                      setDrafting(null);
                    }}
                    onAsk={() =>
                      setAsking({
                        kind: "REQUEST_ANSWER",
                        question: `For ${drafting.codes.join(", ")}: `,
                        departments: [],
                        requirementIds: drafting.ids,
                      })
                    }
                    action={action}
                  />
                )
              }
            />
          </div>

          <div className="draft-below">
            <Draft
              versions={versions.data}
              loading={versions.isLoading}
              artifactId={artifact.data?.id}
              taskId={taskId}
              action={action}
              writing={writing}
              writeup={writeup.data}
              onAskSection={(section) =>
                setAsking({
                  kind: "REQUEST_ANSWER",
                  question: `For the "${section}" part of the response: `,
                  departments: [],
                  section,
                })
              }
            />
          </div>
        </>
      )}

      {asking && (
        <RaiseRequest
          taskId={taskId}
          targets={targets.data ?? []}
          seed={asking}
          onClose={() => setAsking(null)}
          action={action}
        />
      )}
      {taskId && (
        <TaskEvidence
          taskId={taskId}
          onRedraft={() =>
            action("Redraft", async () => {
              const artifact = await api<{ id: string }>(`/tasks/${taskId}/artifact`);
              return api(`/artifacts/${artifact.id}/generate-draft`, {
                method: "POST",
                body: JSON.stringify({ instruction: "Redraft using the newly approved evidence." }),
              });
            })
          }
        />
      )}
      {taskId && <TaskTimeline taskId={taskId} />}
    </>
  );
}

function ChatPane({
  className,
  kicker,
  title,
  taskId,
  conversationId,
  focus,
  onBackToTask,
  onPictureSent,
  emptyText,
  placeholder,
  allowPicture,
  quick,
  cards,
  pinned,
  disabledText,
  setAsking,
  action,
  intercept,
}: {
  className: string;
  kicker: string;
  title: string;
  taskId: string;
  conversationId: string | undefined;
  focus: Focus;
  onBackToTask?: () => void;
  onPictureSent?: () => void;
  emptyText: string;
  placeholder: string;
  allowPicture?: boolean;
  quick?: string[];
  cards?: ReactNode;
  /** Kept above the conversation, so the chat under it stays in view. */
  pinned?: ReactNode;
  /** When set, the chat is off and this says what to do to turn it on. */
  disabledText?: string;
  /** Gets first look at a typed message; returns true when it dealt with it itself. */
  intercept?: (text: string) => Promise<boolean>;
  setAsking: (seed: Seed) => void;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
}) {
  const queryClient = useQueryClient();
  const [question, setQuestion] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [attached, setAttached] = useState<File | null>(null);
  const [visionOk, setVisionOk] = useState(false);
  const [liveStatus, setLiveStatus] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const thread = focusKey(focus);

  const messages = useQuery({
    queryKey: ["messages", conversationId, thread],
    queryFn: () =>
      api<Message[]>(
        `/conversations/${conversationId}/messages?focus=${encodeURIComponent(thread)}`,
      ),
    enabled: Boolean(conversationId),
  });

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.data?.length, liveStatus]);

  async function sendPicture() {
    if (!conversationId || !attached) return;
    // A picture is filed under the task as a whole, so show that thread.
    onPictureSent?.();
    const form = new FormData();
    form.append("file", attached);
    form.append("note", question.trim());
    form.append("use_vision", visionOk ? "true" : "false");
    setQuestion("");
    setStreaming(true);
    setLiveStatus("Reading your picture…");
    try {
      await api(`/conversations/${conversationId}/attachments`, { method: "POST", body: form });
      setAttached(null);
      setVisionOk(false);
    } catch (caught) {
      setLiveStatus(caught instanceof Error ? caught.message : "The picture could not be sent.");
    } finally {
      setStreaming(false);
      await queryClient.invalidateQueries({ queryKey: ["messages", conversationId] });
      setTimeout(() => setLiveStatus(""), 4000);
    }
  }

  async function ask() {
    if (!conversationId || !question.trim()) return;
    const text = question.trim();
    setQuestion("");
    setStreaming(true);
    setLiveStatus("Sending…");
    if (intercept) {
      let handled = false;
      try {
        setLiveStatus("Thinking…");
        handled = await intercept(text);
      } catch (caught) {
        setLiveStatus(caught instanceof Error ? caught.message : "That could not be changed.");
        handled = true;
      }
      if (handled) {
        setStreaming(false);
        await queryClient.invalidateQueries({ queryKey: ["messages", conversationId] });
        setTimeout(() => setLiveStatus(""), 4000);
        return;
      }
      setLiveStatus("Sending…");
    }
    try {
      const job = await api<{ stream_url: string }>(
        `/conversations/${conversationId}/messages`,
        {
          method: "POST",
          body: JSON.stringify({ content: text, focus: focusPayload(focus) }),
        },
      );
      await streamJob(job.stream_url, (event, data) => {
        if (event === "status") setLiveStatus("Building authorized context…");
        if (event === "error") setLiveStatus(`Failed: ${String(data.detail ?? "")}`);
        if (event === "done") {
          setLiveStatus("");
          if (typeof data.writeup_job_id === "string") {
            void queryClient.invalidateQueries({ queryKey: ["writeup", taskId] });
          }
        }
      });
    } catch (caught) {
      setLiveStatus(caught instanceof Error ? caught.message : "The stream dropped.");
    } finally {
      setStreaming(false);
      // The answer is durable regardless of the stream, so always refetch.
      await queryClient.invalidateQueries({ queryKey: ["messages", conversationId] });
      // The reply may have drawn or changed a diagram.
      await queryClient.invalidateQueries({ queryKey: ["diagrams", taskId] });
      setTimeout(() => setLiveStatus(""), 4000);
      await queryClient.invalidateQueries({ queryKey: ["writeup", taskId] });
    }
  }


  useEffect(() => {
    if (cards) endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [cards]);

  return (
    <section className={`panel chat cell ${className}`}>
      <div className="panel-head slim">
        <div>
          <p className="kicker">{kicker}</p>
          <h2>{title}</h2>
        </div>
      </div>
      {disabledText ? (
        <div className="empty">
          <p>{disabledText}</p>
        </div>
      ) : !conversationId ? (
        <div className="empty">
          <p>No conversation on this task yet. Conversations are created explicitly, never by opening the page.</p>
          <button
            onClick={() =>
              action("Conversation", async () => {
                await api(`/tasks/${taskId}/conversations`, { method: "POST", body: "{}" });
                await queryClient.invalidateQueries({ queryKey: ["conversation", taskId] });
              })
            }
          >
            Start a conversation
          </button>
        </div>
      ) : (
        <>
          {pinned && <div className="pinned">{pinned}</div>}
          <div className="thread">
            {!messages.data?.length && <p className="muted-line">{emptyText}</p>}
            {messages.data?.map((message) => (
                    <article key={message.id} className={`turn ${message.role.toLowerCase()}`}>
                      <header>
                        <strong>{message.role.replaceAll("_", " ").toLowerCase()}</strong>
                        {message.model_id && <small>{message.model_id}</small>}
                      </header>
                      {message.role.toLowerCase() === "user" ? (
                        <p style={{ whiteSpace: "pre-wrap" }}>{message.content}</p>
                      ) : (
                        <RichText text={message.content} />
                      )}
                      {(message.retrieval_trace?.diagrams ?? []).map(
                        (d, i) =>
                          d.diagram_id &&
                          d.version_no && (
                            <InlineDiagram key={`${d.diagram_id}-${d.version_no}-${i}`} diagramId={d.diagram_id} versionNo={d.version_no} />
                          ),
                      )}
                      {ladder(message).length > 0 && (
                        <div className="ladder">
                          {ladder(message).map((kind) => (
                            <button
                              className="secondary"
                              key={kind}
                              onClick={() =>
                                setAsking({
                                  kind,
                                  // The routing call writes the question to send; the
                                  // contributor's own words are the fallback.
                                  question:
                                    message.retrieval_trace?.suggested_departments?.[0]?.question ||
                                    askedBefore(messages.data ?? [], message),
                                  departments: message.retrieval_trace?.suggested_departments ?? [],
                                })
                              }
                            >
                              {LADDER_LABEL[kind] ?? kind}
                            </button>
                          ))}
                        </div>
                      )}
                      {message.role === "ASSISTANT" && focus.kind !== "gap" && (
                        <footer>
                          {citations(message)}
                          <Trace messageId={message.id} />
                        </footer>
                      )}
                    </article>
            ))}
            {cards}
            {liveStatus && <p className="live-status">{liveStatus}</p>}
            <div ref={endRef} />
          </div>
          {attached && (
            <div className="attach-strip">
              <span>Picture: {attached.name}</span>
              <label className="muted-line">
                <input type="checkbox" checked={visionOk} onChange={(e) => setVisionOk(e.target.checked)} /> It may be
                sent to the vision model outside the platform to be read
              </label>
              <button type="button" className="ghost" onClick={() => setAttached(null)}>
                Remove
              </button>
            </div>
          )}
          {onBackToTask && focus.kind !== "task" && (
            <div className="focus-chip">
              <span>
                Talking about: <strong>{focusLabel(focus)}</strong>
              </span>
              <button className="ghost" onClick={onBackToTask}>
                Back to the whole task
              </button>
            </div>
          )}
          <form
            className="ask"
            onSubmit={(event) => {
              event.preventDefault();
              if (attached) void sendPicture();
              else void ask();
            }}
          >
            <input
              value={question}
              placeholder={placeholder}
              onChange={(event) => setQuestion(event.target.value)}
              disabled={streaming}
            />
            <button disabled={streaming || (!attached && !question.trim())}>
              {streaming ? "Working…" : attached ? "Send picture" : "Ask"}
            </button>
          </form>
          {(allowPicture || quick?.length) && (
            <div className="quick-asks">
              {allowPicture && (
                <label className="ghost attach-button">
                  Attach a picture of a diagram
                  <input
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    hidden
                    onChange={(e) => {
                      setAttached(e.target.files?.[0] ?? null);
                      e.target.value = "";
                    }}
                  />
                </label>
              )}
              {(quick ?? []).map((text) => (
                <button key={text} type="button" className="ghost" disabled={streaming} onClick={() => setQuestion(text)}>
                  {text}
                </button>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}

/** What the person clicked, and what to prefill the dialog from. */
type Seed = {
  kind: string;
  question: string;
  departments: Target[];
  /** Set when the request is about a gap in the tender, so the reply comes back to it. */
  reason?: string;
  clarificationId?: string;
  /** Requirements ticked on the screen, so the request says which it is about. */
  requirementIds?: string[];
  /** A section of the draft that is waiting on the answer. */
  section?: string;
};

const LADDER_LABEL: Record<string, string> = {
  REQUEST_ANSWER: "Ask another department",
};

/** Whether to offer the "Ask another department" button under this message. */
function ladder(message: Message): string[] {
  if (message.role !== "ASSISTANT") return [];
  // One way to ask: an answer, which may come with the evidence behind it.
  // Access requests are not offered -- nothing here can name the item to grant.
  const options = message.retrieval_trace?.request_options ?? [];
  return options.length ? ["REQUEST_ANSWER"] : [];
}

/** The question this refusal was answering -- the user turn just before it. */
function askedBefore(messages: Message[], refusal: Message): string {
  const index = messages.findIndex((m) => m.id === refusal.id);
  for (let i = index - 1; i >= 0; i -= 1) {
    if (messages[i].role === "USER") return messages[i].content;
  }
  return "";
}

/**
 * Raising a cross-department request, from the refusal that prompted it.
 *
 * Every refusal named three options in prose and the product had no way to take
 * any of them: nothing created a request, so the Requests inbox could never
 * fill. Describing a route out and not building it is worse than not offering
 * one.
 *
 * It opens prefilled because the refusal already knows everything the form
 * needs -- which departments might hold the material, and what was asked. Making
 * someone retype their own question one line below where it appears is the kind
 * of friction that stops people using the governed path and start them
 * messaging a colleague instead, which is exactly what this exists to replace.
 *
 * The business reason is the one field left blank on purpose. Somebody on the
 * other side is deciding whether to hand over material, and a reason the system
 * wrote for them is not a reason.
 */
function RaiseRequest({
  taskId,
  targets,
  seed,
  onClose,
  action,
}: {
  taskId: string;
  targets: Target[];
  seed: Seed;
  onClose: () => void;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
}) {
  // The suggested departments, each with its own question, asked one after
  // another. The chat can name two departments with two different questions;
  // the first version of this dialog offered only the first and hid every other
  // department, so the second question had nowhere to go.
  const suggested = seed.departments.filter((d) => targets.some((t) => t.id === d.id));
  const others = targets.filter((t) => !suggested.some((d) => d.id === t.id));
  const options = [...suggested, ...others];
  const [step, setStep] = useState(0);
  const current = suggested[step];

  const [department, setDepartment] = useState(options[0]?.id ?? "");
  const kind = "REQUEST_ANSWER";
  const [question, setQuestion] = useState(current?.question || seed.question);
  const [reason, setReason] = useState(current?.need ?? seed.reason ?? "");
  const [sent, setSent] = useState<string[]>([]);

  function goTo(next: number) {
    const target = suggested[next];
    if (!target) {
      onClose();
      return;
    }
    setStep(next);
    setDepartment(target.id);
    setQuestion(target.question || seed.question);
    setReason(target.need ?? "");
  }

  // REQUEST_ACCESS is refused by the server unless it names the resource it
  // wants, and this dialog has no way to name one: the whole reason you are
  // asking is that you cannot see what the other department holds. That is the
  // ladder working, not a missing picker -- an answer or a shared item tells
  // you what exists, and only then is there something to ask access TO. So the
  // rung is shown, explained, and not submittable from here.
  const ready = department && question.trim() && reason.trim();

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <div className="panel-head">
          <div>
            <p className="kicker">
              LEAST PRIVILEGE FIRST
              {suggested.length > 1 ? ` · REQUEST ${step + 1} OF ${suggested.length}` : ""}
            </p>
            <h2>Ask another department</h2>
            {sent.length > 0 && <p className="muted-line">Sent to {sent.join(", ")}.</p>}
          </div>
          <button className="linklike" onClick={onClose}>
            close
          </button>
        </div>

        <p className="muted-line">
          They reply in writing and can attach the approved evidence that backs the answer. Nothing
          else is shared and no access is granted.
        </p>

        <label className="stacked">
          Department
          <select
            value={department}
            onChange={(e) => {
              setDepartment(e.target.value);
              const picked = options.find((t) => t.id === e.target.value);
              if (picked?.question) setQuestion(picked.question);
              if (picked?.need) setReason(picked.need);
            }}
          >
            <option value="">Select</option>
            {options.map((t) => {
              const empty = targets.find((x) => x.id === t.id)?.has_members === false;
              return (
                <option key={t.id} value={t.id} disabled={empty}>
                  {t.name}
                  {suggested.some((d) => d.id === t.id) ? " (suggested)" : ""}
                  {empty ? " (nobody in it yet)" : ""}
                </option>
              );
            })}
          </select>
        </label>
        {(() => {
          const why = suggested.find((t) => t.id === department)?.why;
          return why ? <p className="muted-line">Suggested: {why.replace(/\.$/, "")}.</p> : null;
        })()}

        <label className="stacked">
          What you need
          <input value={question} onChange={(e) => setQuestion(e.target.value)} />
        </label>
        <label className="stacked">
          Why this task needs it
          <input
            value={reason}
            autoFocus
            placeholder="Which requirement this answers, e.g. R-061 needs the licence terms."
            onChange={(e) => setReason(e.target.value)}
          />
        </label>

        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            {sent.length ? "Done" : "Cancel"}
          </button>
          {step + 1 < suggested.length && (
            <button className="ghost" onClick={() => goTo(step + 1)}>
              Skip to next
            </button>
          )}
          <button
            disabled={!ready}
            onClick={async () => {
              let raised = false;
              await action("Request", async () => {
                const created = await api(`/requests`, {
                  method: "POST",
                  body: JSON.stringify({
                    origin_task_id: taskId,
                    target_department_id: department,
                    request_type: kind,
                    request_text: question.trim(),
                    business_reason: reason.trim(),
                    ...(seed.clarificationId ? { clarification_id: seed.clarificationId } : {}),
                    ...(seed.requirementIds?.length ? { requirement_ids: seed.requirementIds } : {}),
                    ...(seed.section ? { section: seed.section } : {}),
                  }),
                });
                raised = true;
                return created;
              });
              if (!raised) return; // the error is already on screen; stay on this request
              const name = options.find((t) => t.id === department)?.name ?? "department";
              setSent((list) => [...list, name]);
              goTo(step + 1);
            }}
          >
            {step + 1 < suggested.length ? "Raise request and next" : "Raise request"}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * The draft the model wrote, shown as what it actually is.
 *
 * "DeepSeek draft completed" and then nowhere to read it was the gap: the
 * artifact existed, versioned and audited, and no screen displayed a word of it.
 *
 * It is deliberately NOT rendered as prose. A response's sentences are typed,
 * and the type is the whole point of the drafting model: "we hold ISO 27001" is
 * a fact somebody can check, "we will staff two testers" is a promise the
 * company has not made yet, and "we assume the client provides VPN access" is
 * neither. Flowing them together into a paragraph is how a reviewer approves a
 * commitment believing it was a fact -- so each sentence keeps its class, its
 * backing and its approval state, and the ones that bind the company are the
 * ones that stand out.
 */
function Draft({
  versions,
  loading,
  artifactId,
  taskId,
  action,
  writing,
  writeup,
  onAskSection,
}: {
  onAskSection?: (section: string) => void;
  versions: Version[] | undefined;
  loading: boolean;
  artifactId?: string;
  taskId?: string;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
  writing?: Writing | null;
  writeup?: WriteupState;
}) {
  const queryClient = useQueryClient();
  const latest = versions?.length ? versions[versions.length - 1] : null;
  const [editing, setEditing] = useState(false);
  // A write-up reads best as a document; the sentence-by-sentence view is for checking.
  const [view, setView] = useState<"reading" | "sentences">("reading");
  const holds = writeup?.holds ?? [];
  const [pickSection, setPickSection] = useState("");
  const sectionTitles = [
    ...new Set((latest?.statements ?? []).map((st) => st.section).filter(Boolean) as string[]),
  ];
  const heldCodes: Record<string, string> = {};
  const heldSections: Record<string, string> = {};
  for (const h of holds) {
    if (h.requirement_code) heldCodes[h.requirement_code] = h.department ?? "another department";
    if (h.section) heldSections[h.section] = h.department ?? "another department";
  }
  const hasSections = Boolean(latest?.statements.some((st) => st.section));
  const readingView = view === "reading" && hasSections;
  // A version in review cannot be edited: the server refuses, so the button is
  // not offered rather than offered and refused.
  const editable =
    latest !== null &&
    !["IN_REVIEW", "SUBMITTED_FOR_REVIEW"].includes(latest.state) &&
    Boolean(artifactId) &&
    !writing;

  if (loading) return null;
  if (!latest) {
    return (
      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="kicker">NOT YET DRAFTED</p>
            <h2>Response draft</h2>
          </div>
        </div>
        <Empty text="Use Generate governed draft to write one from this task's authorized context." />
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">TYPED, NOT PROSE</p>
          <h2>Response draft</h2>
        </div>
        <span className="pill">
          v{latest.version_no} · {latest.state.replaceAll("_", " ").toLowerCase()}
        </span>
        {hasSections && !editing && (
          <span className="view-toggle" role="group" aria-label="How to show the response">
            <button
              type="button"
              className={readingView ? "on" : ""}
              onClick={() => setView("reading")}
            >
              Reading view
            </button>
            <button
              type="button"
              className={!readingView ? "on" : ""}
              onClick={() => setView("sentences")}
            >
              Sentence by sentence
            </button>
          </span>
        )}
        {writeup?.finalized && (
          <span className={`pill${writeup.finalized.unchanged ? "" : " warn"}`}>
            {writeup.finalized.unchanged
              ? `finalized ${new Date(writeup.finalized.at).toLocaleString()}`
              : "edited since it was finalized"}
          </span>
        )}
        {editable && !editing && writeup?.has_writeup && !writeup.finalized?.unchanged && (
          <button
            className="secondary"
            type="button"
            title="Keep this write-up, and the diagrams it explains, exactly as they are for the final report"
            onClick={() =>
              action("Finalizing the write-up", async () => {
                await api(`/tasks/${taskId}/writeup/finalize`, { method: "POST" });
                await queryClient.invalidateQueries({ queryKey: ["writeup", taskId] });
              })
            }
          >
            {writeup.finalized ? "Finalize again" : "Finalize write-up"}
          </button>
        )}
        {editable && !editing && (
          <button className="secondary" type="button" onClick={() => setEditing(true)}>
            Edit response
          </button>
        )}
      </div>
      {editing && artifactId ? (
        <DraftEditor
          artifactId={artifactId}
          taskId={taskId}
          statements={latest.statements}
          savesAsNewVersion={latest.state !== "DRAFT"}
          action={action}
          onDone={() => setEditing(false)}
        />
      ) : (
      <div className="context-body">
        {answersNothing(latest) && (
          <div className="notice bad">
            Every statement here restates the tender. A response that only repeats the
            customer&apos;s own clauses answers nothing and commits to nothing — regenerate,
            or write the acceptance yourself.
          </div>
        )}
        {writing && (
          <div className="notice writing" role="status">
            <span className="dots" aria-hidden="true" />
            {writing.total
              ? `Writing section ${Math.min((writing.position ?? writing.done + 1), writing.total)} of ${writing.total}${writing.current ? `: ${writing.current}` : ""}. Each section appears here as soon as it is done.`
              : "Starting the write-up…"}
          </div>
        )}
        {!writing && writeup?.last?.failed?.length ? (
          <div className="notice bad">
            Could not write:{" "}
            {writeup.last.failed.map((f) => `${f.section} (${f.error.slice(0, 220)})`).join("; ")}.
            Use the buttons below, or ask in the chat to try again.
          </div>
        ) : null}
        {onAskSection && sectionTitles.length > 0 && (
          <div className="hold-ask">
            <label>
              This part needs another department:{" "}
              <select value={pickSection} onChange={(e) => setPickSection(e.target.value)}>
                <option value="">choose a section…</option>
                {sectionTitles.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="ghost"
              disabled={!pickSection}
              onClick={() => onAskSection(pickSection)}
            >
              Ask a department
            </button>
            <small className="muted-line">
              Write something in the meantime if you like: the part stays marked until you press
              Fixed.
            </small>
          </div>
        )}
        {holds.length > 0 && (
          <HoldsPanel holds={holds} taskId={taskId} action={action} />
        )}
        {!writing && writeup && (
          <WriteupPanel
            writeup={writeup}
            taskId={taskId}
            artifactId={artifactId}
            editable={editable}
            action={action}
            statements={latest.statements}
          />
        )}
        {latest.statements.length === 0 ? (
          <Empty text={writing ? "The first section is being written…" : "This version has no statements."} />
        ) : readingView ? (
          <ProposalView
            statements={latest.statements}
            artifactId={artifactId}
            taskId={taskId}
            editable={editable && latest.state === "DRAFT"}
            heldCodes={heldCodes}
            heldSections={heldSections}
            onChanged={() => {
              void queryClient.invalidateQueries({ queryKey: ["artifact", taskId] });
              void queryClient.invalidateQueries({ queryKey: ["versions"] });
            }}
          />
        ) : (
          latest.statements
            .slice()
            .sort((a, b) => a.order_index - b.order_index)
            .map((statement, index, all) => (
              <Fragment key={statement.id}>
                {statement.section && statement.section !== all[index - 1]?.section && (
                  <div
                    id={statement.diagram_id ? `sec-${statement.diagram_id}` : undefined}
                    data-section={statement.section}
                    className={`draft-section${statement.out_of_date ? " stale" : ""}`}
                  >
                    <h3>{statement.section}</h3>
                    {statement.out_of_date && (
                      <span className="badge warn" title="The picture this was written from has changed">
                        out of date — {statement.out_of_date_reason}
                      </span>
                    )}
                    <small className="muted-line">
                      {statement.out_of_date
                        ? "Say “update the write-up” in the chat to rewrite this part."
                        : "Not right? Tell the chat what to change in this part."}
                    </small>
                  </div>
                )}
              <div
                className={`statement${(statement.requirement_codes ?? []).some((c) => heldCodes[c]) || (statement.section && heldSections[statement.section]) ? " held" : ""}`}
                id={`st-${statement.id}`}
                key={statement.id}
              >
                <header>
                  <span className={`tag ${statement.statement_class.toLowerCase()}`}>
                    {CLASS_LABEL[statement.statement_class] ?? statement.statement_class}
                  </span>
                  <small>{BACKING[statement.statement_class] ?? ""}</small>
                  <small className="spacer" />
                  <small>{statement.approval_state.toLowerCase()}</small>
                </header>
                <p>{statement.text}</p>
                <small className="muted-line">
                  {statement.evidence_ids?.length
                    ? `${statement.evidence_ids.length} evidence item(s) cited`
                    : statement.source_locator
                      ? "cites the tender"
                      : statement.statement_class === "DIAGRAM_DESCRIPTION"
                        ? "describes a diagram"
                        : "no backing recorded"}
                  {statement.requirement_codes?.length
                    ? ` · answers ${statement.requirement_codes.join(", ")}`
                    : ""}
                </small>
              </div>
              </Fragment>
            ))
        )}
      </div>
      )}
    </section>
  );
}

/** The RFP's missing parts that bear on one requirement, shown under it. */
function VoidList({
  gaps,
  taskId,
  selected,
  onSelect,
  onUseReply,
  onAsk,
  action,
}: {
  gaps: TaskGap[];
  taskId: string;
  selected: string | null;
  onSelect: (gap: TaskGap) => void;
  onUseReply: (gap: TaskGap, text: string, department: string | null) => void;
  onAsk: (gap: TaskGap) => void;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
}) {
  const [open, setOpen] = useState(true);
  const waiting = gaps.filter((g) => g.status !== "ANSWERED").length;
  return (
    <div className="void-list" onClick={(e) => e.stopPropagation()}>
      <button type="button" className="ghost void-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "Hide" : "Show"} {gaps.length} {gaps.length === 1 ? "void" : "voids"} in the RFP
        {waiting ? ` · ${waiting} open` : " · all have an assumption"}
      </button>
      {open &&
        gaps.map((gap) => (
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
    </div>
  );
}

/**
 * Parts of the draft that are waiting on another department.
 *
 * Whatever the writer put in a part stays; this list is the reminder that it is
 * provisional. An empty part shows its placeholder here. When the department has
 * replied the row says so, and the part stays marked until the writer presses
 * "Fixed" -- the reply arriving does not clear it, only a person does.
 */
function HoldsPanel({
  holds,
  taskId,
  action,
}: {
  holds: Hold[];
  taskId?: string;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
}) {
  const queryClient = useQueryClient();
  return (
    <div className="holds" role="region" aria-label="Parts waiting on another department">
      <h4>Waiting on other departments ({holds.length})</h4>
      <ul>
        {holds.map((h) => (
          <li key={h.id} className={h.reply_arrived ? "arrived" : ""}>
            <div>
              <strong>{h.requirement_code ?? h.section}</strong>
              <span className="chip asked">{h.placeholder}</span>
              {h.reply_arrived && <span className="chip reply">reply arrived — fix this part</span>}
            </div>
            {h.note && <p className="muted-line">Asked: {h.note}</p>}
            {h.reply && (
              <p>
                <strong>Their reply:</strong> {h.reply}
              </p>
            )}
            <div className="gap-actions">
              <button
                type="button"
                onClick={() =>
                  void action("Marked as fixed", async () => {
                    await api(`/tasks/${taskId}/holds/${h.id}/fixed`, { method: "POST" });
                    await queryClient.invalidateQueries({ queryKey: ["writeup", taskId] });
                  })
                }
              >
                Fixed
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

const COVERAGE_LABEL: Record<Coverage["state"], string> = {
  answered: "answered",
  conditional: "waiting on the customer",
  restated: "only repeats the tender",
  unanswered: "no answer yet",
  not_ours: "the customer's to do",
  set_aside: "set aside",
};

/** Scroll to a part of the draft and flash it, so "where is this?" has an answer. */
function showInDraft(elementId: string) {
  const el = document.getElementById(elementId);
  if (!el) return false;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.add("flash");
  window.setTimeout(() => el.classList.remove("flash"), 2200);
  return true;
}

/**
 * Which diagrams are in the written response, and whether the tender's requirements
 * are really answered in it.
 *
 * Diagrams carry the same Figure number as in the draft, so a row here and a figure
 * there are the same thing, and "Show in the draft" jumps to it. The person decides which
 * diagrams are in: a duplicate or a working sketch can stay on the task and out of the
 * response. Requirement coverage is counted from the draft's own sentences, not from what a
 * model says it wrote.
 */
function WriteupPanel({
  writeup,
  taskId,
  artifactId,
  editable,
  action,
  statements,
}: {
  writeup: WriteupState;
  taskId?: string;
  artifactId?: string;
  editable: boolean;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
  statements: Statement[];
}) {
  const queryClient = useQueryClient();
  const ordered = statements.slice().sort((a, b) => a.order_index - b.order_index);
  // The draft numbers its figures in reading order; the list here uses the same numbers.
  const figureOf = new Map<string, number>();
  for (const st of ordered) {
    if (st.diagram_id && !figureOf.has(st.diagram_id)) figureOf.set(st.diagram_id, figureOf.size + 1);
  }
  const diagrams = (writeup.diagrams ?? [])
    .slice()
    .sort(
      (a, b) =>
        (figureOf.get(a.diagram_id) ?? 1000 + (a.state === "left_out" ? 500 : 0)) -
        (figureOf.get(b.diagram_id) ?? 1000 + (b.state === "left_out" ? 500 : 0)),
    );
  const ours = (writeup.coverage ?? []).filter((c) => c.state !== "not_ours");
  const answered = ours.filter((c) => c.state === "answered").length;
  const condition = ours.filter((c) => c.state === "conditional").length;
  const missing = ours.filter((c) => c.state === "unanswered" || c.state === "restated").length;
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["writeup", taskId] });
    if (artifactId) await queryClient.invalidateQueries({ queryKey: ["versions", artifactId] });
  };
  const post = (label: string, path: string) =>
    action(label, async () => {
      await api(`/tasks/${taskId}/writeup/${path}`, { method: "POST" });
      await refresh();
    });
  const [note, setNote] = useState<string | null>(null);
  const where = (elementId: string, none: string) =>
    setNote(showInDraft(elementId) ? null : none);
  if (!diagrams.length && !ours.length) return null;
  const undecided = diagrams.filter((d) => d.state === "pending").length;
  return (
    <div className="writeup-panel">
      {note && <small className="muted-line">{note}</small>}
      {diagrams.length > 0 && (
        <div className="wp-block">
          <h4>Diagrams in this response</h4>
          {undecided > 0 && writeup.has_writeup && (
            <small className="muted-line">
              {undecided === 1 ? "One diagram has" : `${undecided} diagrams have`} no section yet.
              Include the ones the response needs and leave out the rest; a left-out diagram stays on
              the task.
            </small>
          )}
          <ul>
            {diagrams.map((d) => {
              const figure = figureOf.get(d.diagram_id);
              return (
                <li key={d.diagram_id}>
                  <span>
                    {figure && <strong>Figure {figure}. </strong>}
                    {d.title}
                    <small className="muted-line">
                      {" "}
                      version {d.version_no ?? 1}
                      {d.parts ? `, ${d.parts} boxes` : ""}
                    </small>
                  </span>
                  <span className={`chip ${d.state}`}>
                    {d.state === "written"
                      ? "in the response"
                      : d.state === "left_out"
                        ? "left out"
                        : "not chosen yet"}
                  </span>
                  <span className="wp-actions">
                    {d.state === "written" && (
                      <button
                        type="button"
                        className="secondary"
                        onClick={() =>
                          where(`sec-${d.diagram_id}`, "Switch to Reading view or Sentence by sentence to find it.")
                        }
                      >
                        Show in the draft
                      </button>
                    )}
                    <button
                      type="button"
                      className="secondary"
                      disabled={!editable}
                      title={
                        d.state === "written"
                          ? "Write this section again, in the current style, from the diagram as it is now"
                          : undefined
                      }
                      onClick={() =>
                        post(
                          d.state === "written" ? "Rewriting that section" : "Writing that section",
                          `diagrams/${d.diagram_id}/include`,
                        )
                      }
                    >
                      {d.state === "written"
                        ? "Rewrite"
                        : d.state === "left_out"
                          ? "Put back in"
                          : "Include"}
                    </button>
                    {d.state !== "left_out" && (
                      <button
                        type="button"
                        className="secondary"
                        disabled={!editable}
                        onClick={() => post("Leaving it out", `diagrams/${d.diagram_id}/leave-out`)}
                      >
                        Leave out
                      </button>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {ours.length > 0 && (
        <div className="wp-block">
          <h4>Tender requirements</h4>
          <p className="wp-summary">
            {`${answered} of ${ours.length} answered in the response`}
            {condition ? `, ${condition} waiting for the customer to clarify them` : ""}
            {missing ? `, ${missing} with no answer yet` : ""}.{" "}
            {missing + condition > 0 && (
              <button
                type="button"
                className="secondary"
                disabled={!editable}
                onClick={() => post("Answering the requirements", "requirements")}
              >
                {writeup.requirements_written
                  ? "Answer the remaining ones again"
                  : "Answer the remaining requirements"}
              </button>
            )}
          </p>
          <small className="muted-line">
            Requirements a diagram delivers are answered in that diagram&apos;s explanation; only
            the rest get a section of their own.
          </small>
          <details open>
            <summary>Each requirement</summary>
            <ul>
              {ours.map((c) => {
                const first = ordered.find((st) => st.requirement_codes?.includes(c.code));
                const figures = (c.diagram_ids ?? [])
                  .map((id) => figureOf.get(id))
                  .filter((n): n is number => n !== undefined)
                  .sort((a, b) => a - b);
                const inOwn = c.sections.includes("Response to the tender requirements");
                const where_ = [
                  ...figures.map((n) => `Figure ${n}`),
                  ...(inOwn ? ["the requirements section"] : []),
                ].join(", ");
                return (
                  <li key={c.code}>
                    <span>
                      <strong>{c.code}</strong> {c.text}
                      {c.needs && <small className="muted-line"> Assessment: {c.needs}.</small>}
                      {where_ && <small className="muted-line"> Answered in {where_}.</small>}
                    </span>
                    <span className={`chip ${c.state}`}>{COVERAGE_LABEL[c.state]}</span>
                    <span className="wp-actions">
                      <button
                        type="button"
                        className="secondary"
                        disabled={!first}
                        title={first ? "Jump to the first sentence that answers this" : "Nothing in the draft answers this yet"}
                        onClick={() => first && where(`st-${first.id}`, "Not visible in this view; try the other view.")}
                      >
                        Show where
                      </button>
                    </span>
                  </li>
                );
              })}
            </ul>
          </details>
        </div>
      )}
    </div>
  );
}

/**
 * A draft that only quotes the tender back at the customer.
 *
 * The first real draft did exactly this: three contractual requirements, three
 * statements, each a near-verbatim copy of the clause it was answering. It
 * passed every rule the generator enforces -- only authorized sources, a T-ref
 * on each -- because copying the clause is the cheapest fully-compliant output.
 *
 * The prompt now forbids it, but a prompt is guidance rather than a guarantee,
 * so the screen says it too. Cheap to compute, and it fails in the safe
 * direction: an empty draft is not flagged, because "nothing yet" is already
 * obvious and does not need a warning.
 */
function answersNothing(version: Version): boolean {
  return (
    version.statements.length > 0 &&
    version.statements.every((s) => s.statement_class === "TENDER_DERIVED")
  );
}

// What each class means to the person reading it, rather than the enum name.
const CLASS_LABEL: Record<string, string> = {
  EVIDENCE_BACKED_FACT: "Fact",
  TENDER_DERIVED: "From the tender",
  PROPOSED_COMMITMENT: "Commitment",
  ASSUMPTION: "Assumption",
  HUMAN_PROVIDED_INPUT: "Written by a person",
  DIAGRAM_DESCRIPTION: "Describes the diagram",
};

// And what has to be true for it to survive review.
const BACKING: Record<string, string> = {
  EVIDENCE_BACKED_FACT: "checkable against approved evidence",
  TENDER_DERIVED: "restates what the tender asked",
  PROPOSED_COMMITMENT: "binds the company — needs an approver",
  ASSUMPTION: "true only if the client agrees",
  HUMAN_PROVIDED_INPUT: "not from the corpus",
  DIAGRAM_DESCRIPTION: "says what the picture shows — the diagram is what is approved",
};

/**
 * Why a given answer said what it did, fetched rather than linked.
 *
 * This was an <a href> to the API. It could never have worked: the API is
 * bearer-token only, and a new browser tab carries no Authorization header, so
 * the one control for auditing an answer always returned 401. A reader who
 * clicked it learned nothing except that something was broken.
 *
 * The trace is the audit surface for a governed answer -- which chunks were
 * admissible before ranking, which filters applied, which grants were live, and
 * why each candidate matched. It belongs in the conversation next to the answer
 * it explains, not in a tab.
 */
/**
 * What an answer actually leaned on, counted by kind.
 *
 * "no sources cited" appeared under an answer that cited [E1], [E2] and [E3] in
 * its second paragraph -- because only [S#] was ever counted. An answer grounded
 * in approved evidence displaying as ungrounded is the worst direction for this
 * to fail in: it is the reviewer's one at-a-glance signal, and it was lying in
 * favour of distrust.
 *
 * Evidence and corpus chunks are counted separately because they are not the
 * same claim. Evidence passed an approval gate; a chunk is raw company text the
 * task happens to be entitled to read.
 */
/**
 * What an answer rested on, in the reader's terms.
 *
 * Three kinds, counted separately, because they are not interchangeable: a
 * corpus chunk is retrieved text, an evidence item passed an approval gate,
 * and a cross-department answer is a named colleague's word given for a stated
 * reason on a date. Only counting two of them is what made an answer built
 * entirely out of HR's reply -- citing [R1] four times -- report "no sources
 * cited", which reads as the attribution failing at the moment it worked.
 */
function citations(message: Message): string {
  const evidence = message.cited_evidence_ids?.length ?? 0;
  const chunks = message.cited_chunk_ids?.length ?? 0;
  // Messages written before `cited_request_ids` existed have it null, and
  // re-reading one must not claim it rested on nothing: the trace recorded
  // every ref at the time, so fall back to counting [R#] there. Same fact,
  // written down twice -- not a guess about what an old answer might have used.
  const answers =
    message.cited_request_ids?.length ??
    // [R-061] is a requirement, [R2] a department's answer: only the latter counts.
    (message.retrieval_trace?.sources_cited?.filter((ref) => /^R\d+$/.test(ref)).length || 0);
  if (!evidence && !chunks && !answers) return "no sources cited";
  const parts: string[] = [];
  if (evidence) parts.push(`${evidence} approved evidence item(s)`);
  if (chunks) parts.push(`${chunks} corpus chunk(s)`);
  if (answers) parts.push(`${answers} cross-department answer(s)`);
  return `cites ${parts.join(", ")}`;
}

function Trace({ messageId }: { messageId: string }) {
  const [open, setOpen] = useState(false);
  const trace = useQuery({
    queryKey: ["trace", messageId],
    queryFn: () => api<Record<string, unknown>>(`/messages/${messageId}/retrieval-trace`),
    enabled: open,
  });

  return (
    <>
      <button className="linklike" onClick={() => setOpen((was) => !was)}>
        {open ? "hide retrieval trace" : "retrieval trace"}
      </button>
      {open && (
        <pre className="trace">
          {trace.isLoading
            ? "Loading…"
            : trace.error
              ? (trace.error as Error).message
              : JSON.stringify(trace.data, null, 2)}
        </pre>
      )}
    </>
  );
}

/**
 * The owner of a task, and -- for whoever may change it -- a way to.
 *
 * Three states, and the middle one is the point of the whole feature:
 *
 *   NONE        the viewer may not staff this task. Show the owner, plainly --
 *               which for the bid manager is now the usual case, because a
 *               headed department belongs to its head.
 *   DEPARTMENT  a department head, on their own department. Their own people
 *               and nobody else.
 *   UNHEADED    nobody heads this department yet, so it falls back to the bid
 *               manager. The label says so, because a control that appears on
 *               some tasks and not others should explain which it is.
 *
 * The candidate list comes from the server and is never filtered here. If this
 * component ever decided for itself who belongs in a department, it would be a
 * second copy of a permission rule -- and the copy that is wrong is always the
 * one nobody tests. Assigning still goes through the API, which re-checks.
 */
// "Sara K. (Finance)": where a person sits, so a list of names is not a guess. Someone in
// no department is shown by role, and someone with neither is said to have none.
function whoLabel(person: Assignable["candidates"][number]): string {
  const where = person.positions?.length
    ? person.positions.map((p) => `${p.department} · ${p.position}`).join(", ")
    : person.departments?.length
      ? person.departments.join(", ")
      : person.roles?.length
        ? person.roles.join(", ")
        : "no department";
  return `${person.display_name} (${where})`;
}

function Owner({
  taskId,
  ownerId,
  assignable,
  action,
}: {
  taskId: string;
  ownerId: string | null;
  assignable: Assignable | undefined;
  action: (label: string, work: () => Promise<unknown>) => Promise<void>;
}) {
  if (!taskId || !assignable) return null;

  const owner = assignable.candidates.find((person) => person.id === ownerId);
  const ownerLabel = owner ? whoLabel(owner) : (ownerId ? "Assigned" : "Unassigned");

  if (assignable.candidates.length === 0) {
    return (
      <label title={assignable.reason}>
        Owner
        <select disabled value="">
          <option value="">{ownerLabel}</option>
        </select>
      </label>
    );
  }

  return (
    <label
      title={`${assignable.reason} Reassigning does not delete the previous owner's conversation.`}
    >
      Owner
      <select
        value={ownerId ?? ""}
        onChange={(event) => {
          const next = event.target.value;
          if (!next || next === ownerId) return;
          action("Assignment", () =>
            api(`/tasks/${taskId}/owner`, {
              method: "POST",
              body: JSON.stringify({ owner_user_id: next }),
            }),
          );
        }}
      >
        <option value="">Unassigned</option>
        {assignable.candidates.map((person) => (
          <option key={person.id} value={person.id}>
            {whoLabel(person)}
          </option>
        ))}
      </select>
      <small>
        {assignable.scope === "DEPARTMENT"
          ? `${assignable.department} only`
          : "no head named"}
      </small>
    </label>
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
