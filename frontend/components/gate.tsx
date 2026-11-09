"use client";

import { createContext, useContext, type ReactNode } from "react";

import { explainMissingPermission } from "@/lib/permission-roles";

/**
 * Controls that know whether the signed-in account may use them.
 *
 * `/auth/me` has always returned the caller's full permission set. The UI
 * dropped it on the floor — the `Me` type did not even declare the field — so
 * every guarded button was offered to everyone, and the only thing that ever
 * said no was the server, after the click. That is the same defect this
 * codebase keeps producing in other places: a value that decides the outcome
 * sits right next to the thing that renders, and is never read.
 *
 * The symptom is worse than a wasted request. The refusal banner is at the top
 * of a long page, so someone who clicks a button in the middle of the evidence
 * list sees nothing move and concludes the button is broken. Measured on the
 * real page: banner pinned at y=0, button at y=337, scrolled 800px down. The
 * feedback was on screen and nowhere near the question.
 *
 * So the answer is given before the click, at the control, in the same words
 * the 403 would have used — `explainMissingPermission` and
 * `explainPermissionError` read one table, so they cannot name different
 * people for the same permission.
 *
 * Refused controls are disabled and explained, never hidden. Hiding would make
 * the product look like it lacks the feature; the truth is that the feature
 * belongs to somebody else, and that separation is the point of the system.
 */
const PermissionsContext = createContext<string[] | null>(null);

export const PermissionsProvider = PermissionsContext.Provider;

/**
 * Three-valued on purpose: `null` means "not loaded yet".
 *
 * Collapsing unknown into false would disable every guarded control for the
 * moment between mount and the /auth/me response, which reads as a permission
 * failure that then silently repairs itself. Callers must distinguish.
 */
export function useCan(permission: string): boolean | null {
  const held = useContext(PermissionsContext);
  if (held === null) return null;
  return held.includes(permission);
}

/**
 * Wrap any guarded control. Children are disabled and explained when the
 * account lacks `need`, and untouched otherwise.
 *
 * The disabling is done by a `fieldset`, which disables every form control
 * inside it, so this works on a button, a group of buttons or a whole form
 * without any of them knowing about permissions. A control that is not a form
 * element — a link styled as a button — is not covered, and must not be used
 * for a guarded action.
 */
export function Gated({
  need,
  children,
  note,
}: {
  need: string;
  children: ReactNode;
  /** Replaces the generated explanation where a screen has better wording. */
  note?: string;
}) {
  const can = useCan(need);
  // Unknown is treated as permitted: the server is still the authority, and a
  // brief window where a button works and is then refused is far less
  // confusing than one where it is dead and then heals.
  if (can !== false) return <>{children}</>;
  return (
    <div className="gated">
      <fieldset disabled>{children}</fieldset>
      <p className="gate-reason">{note ?? explainMissingPermission(need)}</p>
    </div>
  );
}
