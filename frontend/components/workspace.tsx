"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, clearSession, login, token } from "@/lib/api";
import { explainPermissionError } from "@/lib/permission-roles";
import { screens, type ScreenKey } from "@/lib/screens";
import { Approvals } from "@/components/screens/approvals";
import { Clarifications } from "@/components/screens/clarifications";
import { Copilot } from "@/components/screens/copilot";
import { Knowledge } from "@/components/screens/knowledge";
import { Intake } from "@/components/screens/intake";
import { Matrices } from "@/components/screens/matrices";
import { MyWork } from "@/components/screens/mywork";
import { Readiness } from "@/components/screens/readiness";
import { Release } from "@/components/screens/release";
import { Requests } from "@/components/screens/requests";
import { Activity, Bell } from "@/components/notifications";
import { PermissionsProvider } from "@/components/gate";

type Row = Record<string, unknown>;
// `permissions` was already on the wire and missing from this type, so every
// guarded control was rendered blind and only the server ever said no. See
// components/gate.tsx.
type Me = {
  user: { display_name: string; email: string };
  roles: string[];
  permissions: string[];
};
type Bid = Row & { id: string; state: string };

/**
 * Screens still served by the generic table.
 *
 * The rest own their own components, because a table of whatever JSON an
 * endpoint returns cannot express a drill-through, a two-axis breakdown, an
 * ordered approval chain or a chat. Those screens fetch what they need.
 */
const endpoints: Partial<Record<ScreenKey, (bidId: string) => string | null>> = {
  bids: () => "/bids",
};

const OWN_COMPONENT: ScreenKey[] = [
  "activity",
  "work",
  "intake",
  "knowledge",
  "clarifications",
  "matrices",
  "tasks",
  "requests",
  "approvals",
  "readiness",
  "release",
];

export function Workspace({ screen }: { screen: ScreenKey }) {
  const queryClient = useQueryClient();
  const [authenticated, setAuthenticated] = useState(false);
  const [bidId, setBidId] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  // A notification links to /<screen>?bid=<id>&focus=<record>. Read both once,
  // on mount: `bid` wins over the remembered one, because arriving from a
  // notification about another bid and landing on the last bid you looked at
  // is how someone answers the wrong question confidently.
  const [focus, setFocus] = useState("");
  // The exact part of a task to open: "req:<ids>", "gap:<id>" or "gaps".
  const [openTarget, setOpenTarget] = useState("");
  useEffect(() => {
    setAuthenticated(Boolean(token()));
    const params = new URLSearchParams(window.location.search);
    const linked = params.get("bid") ?? "";
    setFocus(params.get("focus") ?? "");
    setOpenTarget(params.get("open") ?? "");
    setBidId(linked || localStorage.getItem("bidready.bid") || "");
  }, []);

  const me = useQuery({ queryKey: ["me", authenticated], queryFn: () => api<Me>("/auth/me"), enabled: authenticated });
  const bids = useQuery({ queryKey: ["bids", authenticated], queryFn: () => api<Bid[]>("/bids"), enabled: authenticated });
  useEffect(() => {
    if (!bidId && bids.data?.[0]) setBidId(bids.data[0].id);
  }, [bidId, bids.data]);
  useEffect(() => { if (bidId) localStorage.setItem("bidready.bid", bidId); }, [bidId]);

  const endpoint = endpoints[screen]?.(bidId) ?? null;
  const data = useQuery({ queryKey: [screen, endpoint], queryFn: () => api<unknown>(endpoint!), enabled: authenticated && Boolean(endpoint) });
  const rows = useMemo(() => normalizeRows(data.data), [data.data]);

  if (!authenticated) return <Login onDone={() => setAuthenticated(true)} />;

  /**
   * Run one guarded action and say what it did.
   *
   * `describe` exists because "completed" is not a result. Several of these
   * endpoints return exactly the numbers a person needs in order to know
   * whether to act again — the batch approval returns {approved, skipped}, and
   * the vector backfill returns {embedded, remaining} specifically so it can be
   * pressed until remaining is zero. Both were thrown away here, and both left
   * someone reading the database by hand to find out whether a button worked.
   *
   * A caller that has nothing to report omits it and gets the old message.
   */
  async function action(
    label: string,
    work: () => Promise<unknown>,
    describe?: (result: never) => string,
  ) {
    setError(""); setNotice(`${label}…`);
    try {
      const result = await work();
      const summary = describe ? describe(result as never) : "";
      setNotice(summary ? `${label}: ${summary}` : `${label} completed.`);
      await queryClient.invalidateQueries();
    }
    catch (caught) {
      setNotice("");
      // A request that never reaches the server leaves no server-side log, so
      // the browser is the only witness. `fetch` rejects with a bare
      // "Failed to fetch" in that case, which tells the reader nothing about
      // where to look — name the likely causes here instead.
      if (caught instanceof TypeError) {
        setError(
          `${label} could not reach the API. The request never left the browser — ` +
            "the server may have stopped, or the page may be open on a different " +
            "port than the one it is running on. Check the terminal running " +
            "run_local.py, then reload this page.",
        );
        return;
      }
      const message = caught instanceof Error ? caught.message : "Action failed.";
      // A permission failure is usually the wrong account rather than a bug:
      // roles are deliberately separated. The earlier version of this message
      // named two fixed roles whatever the missing permission was, so an
      // evidence:approve failure sent the reader to a BID_MANAGER account that
      // refuses it for exactly the same reason. Look the permission up instead.
      setError(
        message.includes("permission") ? explainPermissionError(message) : message,
      );
      // Put the reader where the answer is.
      //
      // The banner is sticky at the top of the page, and a screen like Knowledge
      // is several thousand pixels long. Clicking a button in the middle of it
      // produced a correct, well-worded refusal 800px above the pointer, nothing
      // moved nearby, and the button read as dead. Feedback nobody sees is the
      // same as no feedback.
      //
      // Only on failure: yanking the page on every success would be its own
      // annoyance, and a success needs no reading.
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  const meta = screens[screen];
  return (
    <PermissionsProvider value={me.data?.permissions ?? null}>
    <div className="app-shell">
      <aside>
        <Link className="brand" href="/bids"><span>BR</span><strong>BidReady</strong></Link>
        <p className="side-label">OPERATING SYSTEM</p>
        <nav>{Object.entries(screens).map(([key, item]) => <Link className={key === screen ? "active" : ""} href={`/${key}`} key={key}><i>{item.eyebrow.replace("Screen ", "")}</i>{item.title}</Link>)}</nav>
        <div className="identity"><span className="avatar">{me.data?.user.display_name?.[0] ?? "U"}</span><div><strong>{me.data?.user.display_name ?? "Signed in"}</strong><small>{me.data?.roles?.join(" · ") ?? "Loading role"}</small></div></div>
      </aside>
      <main>
        <header><div><small>{meta.eyebrow} · GOVERNED WORKFLOW</small><h1>{meta.title}</h1><p>{meta.description}</p></div><div className="header-actions"><select aria-label="Active bid" value={bidId} onChange={(e) => setBidId(e.target.value)}><option value="">Select active bid</option>{bids.data?.map((bid) => <option key={bid.id} value={bid.id}>{String(bid.tender_facts && (bid.tender_facts as Row).tender_name || bid.id.slice(0, 8))} · {bid.state}</option>)}</select><Bell enabled={authenticated} /><button className="ghost" onClick={() => { clearSession(); location.reload(); }}>Sign out</button></div></header>
        {notice && <div className="notice good">{notice}</div>}{error && <div className="notice bad">{error}</div>}
        <section className="metrics"><Metric label="Active bids" value={bids.data?.length ?? "—"}/><Metric label="Visible records" value={rows.length}/><Metric label="Current stage" value={String(bids.data?.find((b) => b.id === bidId)?.state ?? "No bid")}/><Metric label="API state" value={data.isFetching ? "Refreshing" : data.error ? "Attention" : "Connected"}/></section>
        {screen === "intake" && <Intake action={action} bidId={bidId} onBidSelected={setBidId} />}
        {screen === "knowledge" && <Knowledge action={action} />}
        {screen === "clarifications" && <Clarifications bidId={bidId} action={action} />}
        {screen === "matrices" && <Matrices bidId={bidId} action={action} />}
        {screen === "tasks" && <Copilot bidId={bidId} action={action} focusTaskId={focus} openTarget={openTarget} />}
        {screen === "requests" && <Requests action={action} focusRequestId={focus} />}
        {screen === "activity" && <Activity />}
        {screen === "work" && <MyWork bidId={bidId} />}
        {screen === "approvals" && <Approvals action={action} bidId={bidId} />}
        {screen === "readiness" && <Readiness bidId={bidId} />}
        {screen === "release" && <Release bidId={bidId} action={action} />}
        {!OWN_COMPONENT.includes(screen) && <DataPanel screen={screen} rows={rows} loading={data.isLoading} error={data.error} />}
      </main>
    </div>
    </PermissionsProvider>
  );
}

function Login({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("sara.k@acme-cyber.example.com");
  const [password, setPassword] = useState("ChangeMe-Dev-2026");
  const mutation = useMutation({ mutationFn: () => login(email, password), onSuccess: onDone });
  return <div className="login"><section><div className="logo">BR</div><small>CONTROLLED BID OPERATIONS</small><h1>Make every claim defensible.</h1><p>Turn tender documents, company evidence and accountable human review into a release-ready response.</p><form onSubmit={(e) => { e.preventDefault(); mutation.mutate(); }}><label>Email<input value={email} onChange={(e) => setEmail(e.target.value)} type="email" required/></label><label>Password<input value={password} onChange={(e) => setPassword(e.target.value)} type="password" required/></label>{mutation.error && <div className="notice bad">{mutation.error.message}</div>}<button disabled={mutation.isPending}>{mutation.isPending ? "Signing in…" : "Enter BidReady"}</button></form><small>Prefilled with the seeded <b>bid manager</b>, who can drive the whole workflow. The seeded <code>admin@</code> account administers users and roles and deliberately cannot write bids — separation of duties is the point. Change all credentials before deployment.</small></section><div className="login-art"><div className="signal"><span>01</span><strong>Tender truth</strong><p>Precedence and provenance are explicit.</p></div><div className="signal"><span>02</span><strong>Company truth</strong><p>Every fact remains governed and evidenced.</p></div><div className="signal"><span>03</span><strong>Release truth</strong><p>Hard blockers have no exception path.</p></div></div></div>;
}

function DataPanel({ screen, rows, loading, error }: { screen: ScreenKey; rows: Row[]; loading: boolean; error: Error | null }) {
  const columns = useMemo(() => visibleColumns(rows), [rows]);
  return <section className="panel"><div className="panel-head"><div><p className="kicker">LIVE GOVERNED DATA</p><h2>{screens[screen].title}</h2></div><div className="panel-actions">{screen === "knowledge" && <span className="pill">Provenance required</span>}</div></div>{loading ? <Empty text="Loading controlled records…"/> : error ? <Empty text={error.message}/> : rows.length === 0 ? <Empty text="No records yet. Use the workflow actions to create the first one."/> : <div className="table-wrap"><table><thead><tr>{columns.map((c) => <th key={c}>{c.replaceAll("_", " ")}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={String(row.id ?? index)}>{columns.map((column) => <td key={column}>{display(row[column])}</td>)}</tr>)}</tbody></table></div>}</section>;
}

function Metric({ label, value }: { label: string; value: unknown }) { return <div><small>{label}</small><strong>{String(value)}</strong></div>; }
function Empty({ text }: { text: string }) { return <div className="empty"><span>◌</span><p>{text}</p></div>; }
function normalizeRows(value: unknown): Row[] { if (Array.isArray(value)) return value as Row[]; if (value && typeof value === "object") return Object.entries(value as Row).map(([key, item]) => ({ metric: key, value: item })); return []; }
function visibleColumns(rows: Row[]) { const priority = ["code", "title", "name", "display_name", "canonical_name", "state", "status", "category", "readiness", "risk", "due_date", "created_at"]; const found = new Set(rows.flatMap((row) => Object.keys(row).filter((key) => !["org_id", "password_hash"].includes(key)))); return [...priority.filter((key) => found.has(key)), ...[...found].filter((key) => !priority.includes(key))].slice(0, 7); }
function display(value: unknown): string { if (value === null || value === undefined || value === "") return "—"; if (typeof value === "object") return JSON.stringify(value).slice(0, 120); return String(value); }
