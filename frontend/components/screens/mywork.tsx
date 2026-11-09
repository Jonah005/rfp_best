"use client";

import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * My work.
 *
 * The flat task list answered "what am I on" and left the person to work out
 * which of those needed them now. This is the same work in the columns they
 * think in: what the AI has prepared for their approval, what needs their
 * input, what is waiting on another department, what other departments are
 * asking them for, and what is done.
 */

type Dashboard = {
  counts: Record<string, number>;
  tasks: { task_id: string; bid_id: string; bid?: string; code: string; title: string; state: string; bucket: string }[];
  approvals: { approval_id: string; entity_type: string; required_role: string }[];
  incoming_requests: { request_id: string; state: string }[];
};

const COLUMNS: { key: string; label: string }[] = [
  { key: "ready_for_approval", label: "Ready for approval" },
  { key: "needs_my_input", label: "Needs my input" },
  { key: "waiting_on_another_department", label: "Waiting on another department" },
  { key: "completed", label: "Completed" },
];

type TodoItem = {
  id: string;
  title: string;
  detail: string;
  href: string;
  at: string | null;
  task_code: string | null;
  bid: string | null;
  request_id?: string;
  count?: number;
};
type TodoGroup = { key: string; title: string; hint: string; items: TodoItem[] };
type Todo = { groups: TodoGroup[]; counts: Record<string, number> };

/**
 * The to-do list: what to answer, review, finish and approve, in the order that
 * matters. Every row opens the task on the exact requirement or missing part, so a
 * person who thought they had finished can step straight back in.
 */
function TodoList() {
  const queryClient = useQueryClient();
  const todo = useQuery({ queryKey: ["todo"], queryFn: () => api<Todo>("/me/todo") });
  const groups = (todo.data?.groups ?? []).filter((g) => g.items.length > 0);
  const open = groups.filter((g) => g.key !== "waiting");
  const waiting = groups.find((g) => g.key === "waiting");
  async function dealtWith(requestId: string) {
    await api("/me/todo/reviewed", { method: "POST", body: JSON.stringify({ request_ids: [requestId] }) });
    await queryClient.invalidateQueries({ queryKey: ["todo"] });
  }
  const row = (item: TodoItem, groupKey: string) => (
    <li key={item.id} className={`todo-row ${groupKey}`}>
      <Link href={item.href}>
        <strong>{item.title}</strong>
        <span>
          {item.detail}
          {item.bid ? ` · ${item.bid}` : ""}
        </span>
      </Link>
      {groupKey === "review" && item.request_id && (
        <button className="ghost" onClick={() => void dealtWith(item.request_id!)}>
          Dealt with
        </button>
      )}
    </li>
  );
  return (
    <section className="panel todo">
      <div className="panel-head">
        <div>
          <p className="kicker">COME BACK TO THESE</p>
          <h2>To do</h2>
        </div>
        <span className="pill">{open.reduce((n, g) => n + g.items.length, 0)} open</span>
      </div>
      {todo.isLoading && <p className="muted-line">Loading your list…</p>}
      {todo.data && !open.length && !waiting && (
        <p className="muted-line">Nothing is waiting on you. New replies and requests will show up here.</p>
      )}
      {open.map((g) => (
        <div key={g.key} className="todo-group">
          <h3>
            {g.title} <span className="pill">{g.items.length}</span>
          </h3>
          <p className="muted-line">{g.hint}</p>
          <ul className="todo-list">{g.items.map((i) => row(i, g.key))}</ul>
        </div>
      ))}
      {waiting && (
        <details className="todo-group">
          <summary>
            {waiting.title} <span className="pill">{waiting.items.length}</span>
          </summary>
          <ul className="todo-list">{waiting.items.map((i) => row(i, "waiting"))}</ul>
        </details>
      )}
    </section>
  );
}

type BidTask = { id: string; code: string; title: string; state: string; owner_user_id: string | null };

export function MyWork({ bidId }: { bidId?: string }) {
  const bidTasks = useQuery({
    queryKey: ["bid-tasks-unassigned", bidId],
    queryFn: () => api<BidTask[]>(`/bids/${bidId}/tasks`),
    enabled: Boolean(bidId),
  });
  const unassigned = (bidTasks.data ?? []).filter((t) => !t.owner_user_id);
  const board = useQuery({ queryKey: ["my-work"], queryFn: () => api<Dashboard>("/me/dashboard") });
  const counts = board.data?.counts ?? {};

  return (
    <>
      <TodoList />
      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="kicker">WHAT IS WAITING ON YOU</p>
            <h2>My work</h2>
          </div>
        </div>
        <div className="metrics">
          {COLUMNS.map((column) => (
            <div key={column.key}>
              <small>{column.label}</small>
              <strong>{counts[column.key] ?? 0}</strong>
            </div>
          ))}
          <div>
            <small>Incoming requests</small>
            <strong>
              <Link href="/requests">{counts.incoming_requests ?? 0}</Link>
            </strong>
          </div>
          <div>
            <small>Approvals for my role</small>
            <strong>
              <Link href="/approvals">{counts.approvals_for_my_role ?? 0}</Link>
            </strong>
          </div>
        </div>
      </section>

      {COLUMNS.map((column) => {
        const rows = (board.data?.tasks ?? []).filter((task) => task.bucket === column.key);
        if (!rows.length) return null;
        return (
          <section className="panel" key={column.key}>
            <div className="panel-head">
              <h2>{column.label}</h2>
              <span className="pill">{rows.length}</span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Task</th>
                    <th>Bid</th>
                    <th>Title</th>
                    <th>State</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((task) => (
                    <tr key={task.task_id}>
                      <td>
                        <Link href={`/tasks?bid=${task.bid_id}&focus=${task.task_id}`}>{task.code}</Link>
                      </td>
                      <td>{task.bid ?? ""}</td>
                      <td>{task.title}</td>
                      <td>{task.state.replaceAll("_", " ").toLowerCase()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        );
      })}
      {unassigned.length > 0 && (
        <section className="panel">
          <div className="panel-head">
            <div>
              <p className="kicker">SELECTED BID · NO OWNER YET</p>
              <h2>Work packages waiting for an owner</h2>
            </div>
            <span className="pill">{unassigned.length}</span>
          </div>
          <p className="muted-line">
            The assessment grouped this bid&apos;s requirements into these packages. They reach someone&apos;s
            &quot;My work&quot; once a department head assigns an owner: open a package and pick one under Owner.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Title</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {unassigned.map((task) => (
                  <tr key={task.id}>
                    <td>
                      <Link href={`/tasks?bid=${bidId}&focus=${task.id}`}>{task.code}</Link>
                    </td>
                    <td>{task.title}</td>
                    <td>{task.state.replaceAll("_", " ").toLowerCase()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {board.isLoading && <p className="muted-line">Loading your work…</p>}
      {board.data && !board.data.tasks.length && (
        <p className="muted-line">Nothing is assigned to you yet.</p>
      )}
    </>
  );
}
