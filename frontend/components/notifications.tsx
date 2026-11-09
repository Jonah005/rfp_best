"use client";

/**
 * The bell, its dropdown, and the full activity list.
 *
 * The product had every event and told nobody. A bid manager learned her
 * cross-department request had been answered by happening to ask the copilot a
 * second question about the same task; a contributor learned work had been
 * assigned to her by opening the task list on a hunch. Everything was recorded
 * and nothing arrived. A governed request that nobody is told about is only
 * better than a chat message if somebody happens to be looking.
 *
 * Two decisions are worth stating, because both could reasonably have gone the
 * other way.
 *
 * Clicking marks read and navigates in one motion, and the item leaves the
 * unread list. It is not deleted -- the activity list keeps every notification
 * a person has ever received, read or not, because "what was I told, and when"
 * is a question people ask after something goes wrong, and an inbox that
 * forgets cannot answer it.
 *
 * The sound is off until someone turns it on. A browser will not play audio
 * before the page has been interacted with anyway, so a sound that defaults to
 * on is a sound that works unpredictably -- and a bid tool that chimes at
 * people all day is a bid tool they mute at the operating system, which loses
 * the alerts that mattered along with the ones that did not.
 */

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export type Notification = {
  id: string;
  kind: string;
  title: string;
  body: string | null;
  screen: string;
  bid_id: string | null;
  focus_id: string | null;
  created_at: string;
  read_at: string | null;
};

// How often the badge asks. Short enough that an answer feels like it arrived,
// long enough that a signed-in tab is not a load generator.
const POLL_MS = 20_000;
const SOUND_KEY = "bidready.notify.sound";

/** Grouped for the activity list, and used to colour the dot. */
const FAMILY: Record<string, string> = {
  "request.created": "request",
  "request.acknowledged": "request",
  "request.answered": "request",
  "request.evidence_shared": "request",
  "request.access_granted": "request",
  "request.rejected": "request",
  "task.owner_assigned": "work",
  "task.blocked": "work",
  "approval.decided": "approval",
  "commitment.decided": "approval",
  "bid.state_changed": "bid",
};

const FAMILY_LABEL: Record<string, string> = {
  request: "Cross-department requests",
  work: "Work assigned to me",
  approval: "Approvals and decisions",
  bid: "Bid milestones",
  other: "Everything else",
};

export function family(kind: string): string {
  return FAMILY[kind] ?? "other";
}

/** Where a notification leads: the screen, the bid, and the record to open. */
export function href(item: Notification): string {
  const params = new URLSearchParams();
  if (item.bid_id) params.set("bid", item.bid_id);
  if (item.focus_id) params.set("focus", item.focus_id);
  const query = params.toString();
  return `/${item.screen}${query ? `?${query}` : ""}`;
}

export function when(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

/** A short tone, synthesised — no asset to ship and nothing to fetch. */
function chime() {
  try {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return;
    const context = new Ctor();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = "sine";
    oscillator.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.08, context.currentTime + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.28);
    oscillator.connect(gain).connect(context.destination);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.3);
    oscillator.onended = () => void context.close();
  } catch {
    // A browser that refuses to make a sound is not a reason to break the bell.
  }
}

export function Bell({ enabled }: { enabled: boolean }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [sound, setSound] = useState(false);
  const previous = useRef<number | null>(null);

  useEffect(() => {
    try {
      setSound(localStorage.getItem(SOUND_KEY) === "on");
    } catch {
      // Private browsing. The bell still works; the preference does not persist.
    }
  }, []);

  const count = useQuery({
    queryKey: ["notification-count"],
    queryFn: () => api<{ unread: number }>("/notifications/count"),
    enabled,
    refetchInterval: POLL_MS,
  });

  const unread = count.data?.unread ?? 0;

  useEffect(() => {
    // Only a rise counts. Marking things read lowers the number and must not
    // ring, and the first load establishes a baseline rather than announcing
    // everything that accumulated while the person was away.
    if (previous.current !== null && unread > previous.current && sound) chime();
    previous.current = unread;
  }, [unread, sound]);

  const items = useQuery({
    queryKey: ["notifications", "unread"],
    queryFn: () => api<Notification[]>("/notifications?unread_only=true&limit=8"),
    enabled: enabled && open,
  });

  const readAll = useMutation({
    mutationFn: () => api("/notifications/read-all", { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notification-count"] }),
  });

  return (
    <div className="bell-wrap">
      <button
        className="bell"
        aria-label={unread ? `${unread} unread notifications` : "Notifications"}
        onClick={() => setOpen((was) => !was)}
      >
        <span aria-hidden>◔</span>
        {unread > 0 && <em>{unread > 99 ? "99+" : unread}</em>}
      </button>

      {open && (
        <>
          <div className="bell-scrim" onClick={() => setOpen(false)} />
          <div className="bell-panel">
            <header>
              <strong>Notifications</strong>
              <label className="sound-toggle">
                <input
                  type="checkbox"
                  checked={sound}
                  onChange={(event) => {
                    setSound(event.target.checked);
                    try {
                      localStorage.setItem(SOUND_KEY, event.target.checked ? "on" : "off");
                    } catch {
                      /* not persisted */
                    }
                    if (event.target.checked) chime();
                  }}
                />
                sound
              </label>
            </header>

            {items.isLoading ? (
              <p className="muted-line">Loading…</p>
            ) : !items.data?.length ? (
              <p className="muted-line">Nothing waiting on you.</p>
            ) : (
              <ul className="bell-list">
                {items.data.map((item) => (
                  <li key={item.id}>
                    <Item item={item} onNavigate={() => setOpen(false)} />
                  </li>
                ))}
              </ul>
            )}

            <footer>
              <Link href="/activity" onClick={() => setOpen(false)}>
                All activity
              </Link>
              <button
                className="linklike"
                disabled={!unread || readAll.isPending}
                onClick={() => readAll.mutate()}
              >
                Mark all read
              </button>
            </footer>
          </div>
        </>
      )}
    </div>
  );
}

/** One row. Clicking marks it read and goes where it points. */
export function Item({
  item,
  onNavigate,
}: {
  item: Notification;
  onNavigate?: () => void;
}) {
  const queryClient = useQueryClient();
  return (
    <Link
      className={`notification ${item.read_at ? "read" : "unread"}`}
      href={href(item)}
      onClick={() => {
        onNavigate?.();
        if (item.read_at) return;
        // Fire and forget: a failed mark-read must not swallow the navigation,
        // and the next poll corrects the badge either way.
        void api(`/notifications/${item.id}/read`, { method: "POST" })
          .then(() => queryClient.invalidateQueries({ queryKey: ["notification-count"] }))
          .catch(() => undefined);
      }}
    >
      <span className={`dot ${family(item.kind)}`} aria-hidden />
      <span className="body">
        <strong>{item.title}</strong>
        {item.body && <small>{item.body}</small>}
        <i>{when(item.created_at)}</i>
      </span>
    </Link>
  );
}

/**
 * Everything this person has been told, grouped by what kind of thing it was.
 *
 * Read items stay. "What was I told, and when" is a question people ask after
 * something has gone wrong, and an inbox that empties itself cannot answer it.
 */
export function Activity() {
  const all = useQuery({
    queryKey: ["notifications", "all"],
    queryFn: () => api<Notification[]>("/notifications?limit=100"),
  });

  const rows = all.data ?? [];
  const groups = ["request", "work", "approval", "bid", "other"].map((key) => ({
    key,
    label: FAMILY_LABEL[key],
    rows: rows.filter((row) => family(row.kind) === key),
  }));

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <p className="kicker">EVERYTHING YOU WERE TOLD</p>
          <h2>Activity</h2>
        </div>
        <span className="pill">{rows.filter((r) => !r.read_at).length} unread</span>
      </div>

      {all.isLoading ? (
        <p className="muted-line pad">Loading…</p>
      ) : !rows.length ? (
        <p className="muted-line pad">
          Nothing yet. Requests you raise, work assigned to you, decisions on your drafts and
          milestones on your bids all arrive here.
        </p>
      ) : (
        groups
          .filter((group) => group.rows.length > 0)
          .map((group) => (
            <div className="activity-group" key={group.key}>
              <p className="kicker">{group.label}</p>
              <ul className="bell-list">
                {group.rows.map((item) => (
                  <li key={item.id}>
                    <Item item={item} />
                  </li>
                ))}
              </ul>
            </div>
          ))
      )}
    </section>
  );
}
