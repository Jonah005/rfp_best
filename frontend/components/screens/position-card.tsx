"use client";

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

type Action = (label: string, work: () => Promise<unknown>) => Promise<void>;

type Option = { label: string; text: string; is_commitment: boolean; caution: string };
type Proposal = {
  found: "none" | "partial" | "full";
  summary: string;
  options: Option[];
  error: string | null;
  codes: string[];
};

const FOUND: Record<string, string> = {
  none: "No real information was found in the company's approved evidence for",
  partial: "Only part of this is covered by approved evidence for",
  full: "Approved evidence exists for",
};

/**
 * What to say about requirements the company has no evidence for, in the chat.
 *
 * It says what was found, offers a few honest ways to handle it, words the chosen
 * one formally, and a person submits it -- or discusses it, asks another
 * department, or sets the requirements aside. The AI never submits or sets aside;
 * the record of what it suggested stays with whatever the person decided.
 */
export function PositionCard({
  taskId,
  ids,
  codes,
  onClose,
  onDiscuss,
  onAsk,
  action,
  seedWords,
}: {
  taskId: string;
  ids: string[];
  codes: string[];
  /** A department's reply to word formally straight away, skipping the options. */
  seedWords?: string;
  onClose: () => void;
  onDiscuss: () => void;
  onAsk: () => void;
  action: Action;
}) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<"loading" | "options" | "formal" | "ignore">("loading");
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const [own, setOwn] = useState("");
  const [text, setText] = useState("");
  const [drafted, setDrafted] = useState("");
  const [fromOption, setFromOption] = useState<number | null>(null);
  const [instruction, setInstruction] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (seedWords) {
      void word(null, seedWords);
      return;
    }
    let live = true;
    api<Proposal>(`/tasks/${taskId}/positions/propose`, {
      method: "POST",
      body: JSON.stringify({ requirement_ids: ids }),
    })
      .then((p) => {
        if (!live) return;
        setProposal(p);
        setStep("options");
      })
      .catch((e) => {
        if (!live) return;
        setError(e instanceof Error ? e.message : "Could not look this up.");
        setStep("options");
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function word(option: number | null, words: string | null) {
    setBusy(true);
    setError("");
    try {
      const out = await api<{ text: string; from_option: number | null }>(
        `/tasks/${taskId}/positions/formalise`,
        {
          method: "POST",
          body: JSON.stringify({
            requirement_ids: ids,
            option,
            own_words: words,
            instruction: instruction.trim() || null,
          }),
        },
      );
      setText(out.text);
      setDrafted(out.text);
      setFromOption(out.from_option);
      setStep("formal");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not word that.");
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    await action("Answer submitted", async () => {
      await api(`/tasks/${taskId}/positions/submit`, {
        method: "POST",
        body: JSON.stringify({
          requirement_ids: ids,
          text: text.trim(),
          source: text.trim() === drafted ? "model" : "person",
          option: fromOption,
        }),
      });
      await queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] });
      await queryClient.invalidateQueries({ queryKey: ["positions", taskId] });
      onClose();
    });
  }

  async function setAside() {
    await action("Set aside", async () => {
      await api(`/tasks/${taskId}/positions/ignore`, {
        method: "POST",
        body: JSON.stringify({ requirement_ids: ids, reason: reason.trim() || null }),
      });
      await queryClient.invalidateQueries({ queryKey: ["task-detail", taskId] });
      await queryClient.invalidateQueries({ queryKey: ["positions", taskId] });
      onClose();
    });
  }

  const label = codes.join(", ");
  return (
    <article className="turn assistant gap-card">
      <header>
        <strong>copilot · {label}</strong>
      </header>

      {step === "loading" && <p className="live-status">Looking at what the company holds…</p>}
      {error && <p className="notice bad">{error}</p>}

      {step === "options" && proposal && (
        <>
          <p>
            <strong>
              {FOUND[proposal.found]} {label}.
            </strong>{" "}
            {proposal.summary}
          </p>
          {proposal.options.length > 0 ? (
            <>
              <p className="muted-line">Here is what we could say:</p>
              <ul className="option-list">
                {proposal.options.map((o, i) => (
                  <li
                    key={i}
                    className={picked === i ? "option on" : "option"}
                    onClick={() => setPicked(i)}
                  >
                    <input type="radio" checked={picked === i} onChange={() => setPicked(i)} />
                    <div>
                      <strong>{o.label}</strong>
                      <span>{o.text}</span>
                      {o.is_commitment && <em>A promise the company has not yet shown it can keep.</em>}
                      {o.caution && <em>Check first: {o.caution}</em>}
                    </div>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            !error && <p className="muted-line">The AI had no suggestion just now.</p>
          )}
          <label className="gap-edit">
            Or say what you would like to say, in your own words
            <textarea rows={2} value={own} onChange={(e) => setOwn(e.target.value)} />
          </label>
          <div className="gap-actions">
            <button
              disabled={busy || (picked === null && !own.trim())}
              onClick={() => void word(own.trim() ? null : picked, own.trim() || null)}
            >
              {busy ? "Working…" : own.trim() ? "Word it formally" : "Use this, worded formally"}
            </button>
            <button className="ghost" onClick={onDiscuss}>
              Discuss it in the chat
            </button>
            <button className="ghost" onClick={onAsk}>
              Ask another department
            </button>
            <button className="ghost" onClick={() => setStep("ignore")}>
              Ignore {ids.length === 1 ? "it" : "these"}
            </button>
            <button className="ghost" onClick={onClose}>
              Close
            </button>
          </div>
        </>
      )}

      {step === "formal" && (
        <>
          <label className="gap-edit">
            Our answer to {label} — edit it if you like, then submit
            <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <label className="gap-edit">
            Want it changed? Say how, and I will word it again
            <input
              value={instruction}
              placeholder="e.g. shorter, or mention the pilot phase"
              onChange={(e) => setInstruction(e.target.value)}
            />
          </label>
          <div className="gap-actions">
            <button disabled={!text.trim()} onClick={() => void submit()}>
              Submit as our answer
            </button>
            <button
              className="ghost"
              disabled={busy || !instruction.trim()}
              onClick={() => void word(fromOption, fromOption === null ? text : null)}
            >
              Word it again
            </button>
            <button className="ghost" onClick={() => setStep("options")}>
              Back
            </button>
          </div>
        </>
      )}

      {step === "ignore" && (
        <>
          <p>
            Set {label} aside? {ids.length === 1 ? "It" : "They"} will leave this list and the
            written response. The record stays under &ldquo;set aside&rdquo; with what the AI
            suggested, and you can restore {ids.length === 1 ? "it" : "them"} any time.
          </p>
          <label className="gap-edit">
            Why? (optional)
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <div className="gap-actions">
            <button onClick={() => void setAside()}>Set aside</button>
            <button className="ghost" onClick={() => setStep("options")}>
              Back
            </button>
          </div>
        </>
      )}
    </article>
  );
}
