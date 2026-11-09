"""Move a bid along its workflow states, one legal step at a time.

The bid state machine is real and enforced, but nothing in the UI drives it:
the bid dashboard is a table, and the release screen assumes a bid that already
reached ASSEMBLY. So a bid sits in INTAKE_IN_PROGRESS and the last five screens
of the product cannot be reached at all -- not because they are broken, because
nothing moves the bid.

This does that, as the bid manager, through the same endpoint the UI would use.

    python scripts/advance_bid.py                     # show where it is
    python scripts/advance_bid.py ASSEMBLY            # walk there, one step at a time
    python scripts/advance_bid.py ASSEMBLY --apply    # actually do it

The route is computed from `bids.ALLOWED_TRANSITIONS` -- the same table the
server enforces -- rather than a path written down here, so it cannot drift and
it cannot invent a shortcut the server would reject. Two states are excluded
from any route it plans: NO_BID and CANCELLED are decisions about the bid, not
steps on the way somewhere, and a script must never walk through them to reach
something else.

RELEASED is refused as a destination. Releasing goes through the release screen
with its validation gates and a second person's approval, and a script that
skipped to it would be skipping the only part that matters.
"""

from __future__ import annotations

import os
import sys
from collections import deque

import httpx

from app.models.enums import BidState
from app.services.bids import ALLOWED_TRANSITIONS, REASON_REQUIRED

BASE = os.environ.get("BIDREADY_API", "http://127.0.0.1:8000/api/v1")
PASSWORD = os.environ.get("BIDREADY_DEMO_PASSWORD", "ChangeMe-Dev-2026")
BID_MANAGER = "sara.k@acme-cyber.example.com"

# Never routed through. Both are terminal judgements about the bid, and both
# need a reason a script has no business inventing.
NEVER_ROUTE = {BidState.NO_BID, BidState.CANCELLED}
# Never a destination: it has its own gates and its own approver.
NEVER_TARGET = {BidState.RELEASED, BidState.ARCHIVED}


def route(start: BidState, goal: BidState) -> list[BidState] | None:
    """Shortest legal path, read from the server's own transition table."""
    if start is goal:
        return []
    seen = {start}
    queue: deque[tuple[BidState, list[BidState]]] = deque([(start, [])])
    while queue:
        state, path = queue.popleft()
        for nxt in sorted(ALLOWED_TRANSITIONS.get(state, frozenset()), key=lambda s: s.value):
            if nxt in seen or nxt in NEVER_ROUTE:
                continue
            if nxt is goal:
                return [*path, nxt]
            seen.add(nxt)
            queue.append((nxt, [*path, nxt]))
    return None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv

    session = httpx.Client(base_url=BASE, timeout=60)
    try:
        auth = session.post("/auth/login", json={"email": BID_MANAGER, "password": PASSWORD})
        auth.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Could not sign in as {BID_MANAGER}: {exc}")
        print("Is the stack running?")
        return 1
    session.headers["Authorization"] = f"Bearer {auth.json()['access_token']}"

    bids = session.get("/bids").json()
    if not bids:
        print("No bids.")
        return 1
    # The one with requirements is the one being worked on.
    bids.sort(key=lambda b: len(session.get(f"/bids/{b['id']}/requirements").json()), reverse=True)
    bid = bids[0]
    state = BidState(bid["state"])

    print(f"bid    {bid['id']}")
    print(f"state  {state.value}")
    onward = sorted(s.value for s in ALLOWED_TRANSITIONS.get(state, frozenset()))
    print(f"next   {', '.join(onward) or 'nothing'}")

    if not args:
        print("\nPass a destination to plan a route, e.g.:")
        print("  python scripts/advance_bid.py ASSEMBLY")
        return 0

    try:
        goal = BidState(args[0].upper())
    except ValueError:
        print(f"\nUnknown state {args[0]}. Known: {', '.join(s.value for s in BidState)}")
        return 1

    if goal in NEVER_TARGET:
        print(
            f"\n{goal.value} is not a destination for this script. Releasing runs through\n"
            "the release screen: assemble, validate, submit, and a second person approves.\n"
            "Skipping to it would skip the only part worth demonstrating."
        )
        return 1

    steps = route(state, goal)
    if steps is None:
        print(f"\nNo legal route from {state.value} to {goal.value}.")
        return 1
    if not steps:
        print(f"\nAlready in {goal.value}.")
        return 0

    print(f"\nroute  {' -> '.join(s.value for s in steps)}  ({len(steps)} step(s))")
    blocked = [s for s in steps if s in REASON_REQUIRED]
    if blocked:
        print(f"  {', '.join(s.value for s in blocked)} needs a reason a person should write.")
        return 1

    if not apply:
        print("\nDry run. Re-run with --apply to make these transitions.")
        return 0

    for step in steps:
        response = session.post(
            f"/bids/{bid['id']}/state", json={"to_state": step.value, "reason": None}
        )
        if response.status_code != 200:
            detail = response.json().get("detail", response.text)
            print(f"  FAILED at {step.value}: {detail}")
            return 1
        print(f"  -> {step.value}")

    print(f"\nNow in {goal.value}. Every step is in the audit trail as bid.state_changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
