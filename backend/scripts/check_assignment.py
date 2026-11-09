"""Watch delegated staffing work, and watch it refuse.

There is no assignment screen yet, so the only way to see scoped `task:assign`
behave is to drive the API the way the app will. This does that against the
running stack, as the seeded people, and says in plain language what each step
proved.

    python scripts/check_assignment.py

It is read-mostly: the only writes are task generation (idempotent -- it skips
bids that already have tasks) and assigning owners on that bid's tasks, which
the bid manager can undo by assigning someone else. Nothing here touches
evidence, approvals or release.

The rule it checks: **the system routes work to a department, and that
department's head names the person.** A headed department is the head's
exclusively -- not somewhere the bid manager may also reach. The bid manager
keeps the departments nobody heads, which is the fallback that stops work
becoming unassignable.

So, against whichever department this bid actually gave work to:

    1. The head holds the scoped permission and NOT the unscoped one.
    2. The head can put their own people on their own department's task.
    3. The head cannot put an outsider on it.
    4. The head cannot touch another department's task at all.
    5. The BID MANAGER is refused on a headed department, and told whose it is.
    6. The bid manager CAN staff a department with no head -- the fallback.
    7. Someone holding neither permission is refused before any of this runs.

A failure here is a real finding. Most of these expect a 403: a 200 in 3, 4 or
5 is a hole.
"""

from __future__ import annotations

import os
import sys

import httpx

BASE = os.environ.get("BIDREADY_API", "http://127.0.0.1:8000/api/v1")
PASSWORD = os.environ.get("BIDREADY_DEMO_PASSWORD", "ChangeMe-Dev-2026")

SARA = "sara.k@acme-cyber.example.com"  # bid manager, unscoped task:assign
AHMED = "ahmed.h@acme-cyber.example.com"  # head of Security
NADIA = "nadia.r@acme-cyber.example.com"  # in HR, not Security
# The department list is org:read, which is an administrator's, not a bid
# manager's -- Sara runs bids, she does not administer the organization. The
# script needs it only to look things up, so it signs in as the admin for that
# one read and does every assignment as the person whose authority is on trial.
ADMIN = "admin@acme-cyber.example.com"

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print((PASS if ok else FAIL) + "  " + message)
    if not ok:
        failures.append(message)


class Client:
    """One signed-in person."""

    def __init__(self, email: str):
        self.email = email
        r = httpx.post(
            f"{BASE}/auth/login", json={"email": email, "password": PASSWORD}, timeout=30
        )
        r.raise_for_status()
        self.token = r.json()["access_token"]
        me = self.get("/auth/me")
        self.user_id = me["user"]["id"]
        self.name = me["user"]["display_name"]
        self.roles = me["roles"]
        self.permissions = set(me["permissions"])

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **params):
        r = httpx.get(f"{BASE}{path}", headers=self._headers(), params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json=None) -> httpx.Response:
        """Returns the raw response -- half of what this script checks is a 403."""
        return httpx.post(f"{BASE}{path}", headers=self._headers(), json=json, timeout=120)


def assign(actor: Client, task: dict, owner_id: str) -> httpx.Response:
    return actor.post(f"/tasks/{task['id']}/owner", json={"owner_user_id": owner_id})


def detail(response: httpx.Response) -> str:
    try:
        return response.json().get("detail", response.text)[:150]
    except Exception:  # noqa: BLE001
        return response.text[:150]


def main() -> int:
    print(f"api: {BASE}\n")

    try:
        sara = Client(SARA)
    except httpx.HTTPError as exc:
        print(f"Could not sign in as {SARA}: {exc}")
        print("Is the stack running? Start it with:  python run_local.py")
        return 1

    try:
        ahmed, nadia = Client(AHMED), Client(NADIA)
        admin = Client(ADMIN)
    except httpx.HTTPStatusError:
        print(
            "One of the demo accounts is missing. Re-seed:\n"
            "  backend\\.venv\\Scripts\\python -m app.db.seed"
        )
        return 1

    # --- 1. what each person actually holds ---------------------------------
    print("WHO HOLDS WHAT")
    print(f"  {ahmed.name:16} roles={','.join(ahmed.roles)}")
    check("DEPARTMENT_HEAD" in ahmed.roles, "Ahmed holds DEPARTMENT_HEAD")
    check(
        "task:assign_in_department" in ahmed.permissions,
        "Ahmed holds the scoped task:assign_in_department",
    )
    check(
        "task:assign" not in ahmed.permissions,
        "Ahmed does NOT hold unscoped task:assign (the scoped role must not carry it)",
    )
    check("task:assign" in sara.permissions, "Sara still holds unscoped task:assign")
    check(
        "task:assign" not in nadia.permissions
        and "task:assign_in_department" not in nadia.permissions,
        "Nadia holds neither, so she is the control case",
    )

    # --- 2. find a department that has a head AND work ----------------------
    #
    # Hardcoding one was a mistake worth not repeating: which departments a bid
    # produces tasks for depends on the tender, and the first real one here made
    # work for PMO, Commercial, Legal, Bid Management and Quality and none at
    # all for Security. A script that always looks at Security then reports
    # "nothing to staff" tests nothing. So pick the pairing this data actually
    # supports, and say which one was picked.
    departments = {d["id"]: d for d in admin.get("/admin/departments")}
    people = {u["id"]: u for u in admin.get("/admin/users")}

    headed = {d["id"]: d for d in departments.values() if d.get("head_user_id")}
    check(bool(headed), "at least one department names a head")
    unheaded = [d for d in departments.values() if not d.get("head_user_id")]
    check(bool(unheaded), "at least one department has no head, to test the refusal against")

    bids = sara.get("/bids")
    if not bids:
        print("\nNo bids yet. Create one in the app (Intake) and run this again.")
        return 1

    # With more than one bid, picking the first is a coin toss -- and generating
    # tasks on the wrong one leaves the screen you are looking at empty while
    # the script reports success. So show them all, and pick the one that has
    # something to staff.
    print("\nBIDS")
    catalogue = []
    for candidate in bids:
        existing = sara.get(f"/bids/{candidate['id']}/tasks")
        requirements = sara.get(f"/bids/{candidate['id']}/requirements")
        catalogue.append((candidate, existing, len(requirements)))
        print(
            f"  {candidate['id'][:8]}  {candidate['state']:22} "
            f"{len(requirements):3} requirements  {len(existing):3} tasks"
        )

    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    if wanted:
        chosen = [row for row in catalogue if row[0]["id"].startswith(wanted)]
        if not chosen:
            print(f"\nNo bid id starts with {wanted}. Pick one from the list above.")
            return 1
        bid, tasks, requirement_count = chosen[0]
    else:
        # Newest first. Sorting by "most tasks" picked whichever bid had the
        # longest history, which is precisely the OLD one -- so a fresh tender
        # was analysed, the script ran, and every result described the previous
        # customer's bid. Recency is what "the one I am working on" means.
        catalogue.sort(key=lambda row: row[0].get("created_at") or "", reverse=True)
        bid, tasks, requirement_count = catalogue[0]
        if len(catalogue) > 1:
            print("  (newest bid; pass an id to choose another)")

    print(f"\nUSING  {bid['id']}  ({bid['state']})")

    if not tasks:
        if requirement_count == 0:
            print(
                "\nThis bid has no requirements, so there is nothing to group into tasks.\n"
                "Analyse its tender documents first (Intake)."
            )
            return 1
        result = sara.post(f"/bids/{bid['id']}/tasks/generate")
        result.raise_for_status()
        print(f"  generated {result.json()['created']} tasks from {requirement_count} requirements")
        tasks = sara.get(f"/bids/{bid['id']}/tasks")

    by_department: dict[str, list] = {}
    for task in tasks:
        by_department.setdefault(str(task.get("department_id")), []).append(task)
    print(f"  {len(tasks)} tasks")
    for dept_id, rows in by_department.items():
        name = departments.get(dept_id, {}).get("name", "?")
        head_id = departments.get(dept_id, {}).get("head_user_id")
        head = people.get(head_id, {}).get("display_name") if head_id else None
        print(f"    {name:18} {len(rows):3}  {'head: ' + head if head else 'no head'}")

    staffed = [(d, by_department[d["id"]]) for d in headed.values() if by_department.get(d["id"])]
    if not staffed:
        print(
            "\nNo department both names a head and has work on this bid, so the\n"
            "delegated path cannot be exercised here. Name a head for one of the\n"
            "departments listed above in app/db/seed.py, re-seed, and run again."
        )
        return 1

    department, department_tasks = staffed[0]
    head_user = people[department["head_user_id"]]
    head = Client(head_user["email"])
    task = department_tasks[0]
    print(f"\nDEMONSTRATING WITH  {head.name}, head of {department['name']}")

    # An insider and an outsider, taken from the server's own answer rather than
    # assumed -- the endpoint that shapes the screen is the thing under test.
    offer = head.get(f"/tasks/{task['id']}/assignable")
    check(offer["scope"] == "DEPARTMENT", f"the head is scoped to {department['name']} alone")
    insiders = {c["id"] for c in offer["candidates"]}
    check(bool(insiders), f"{department['name']} has someone to assign to")
    outsider = next(
        (u for u in people.values() if u["id"] not in insiders and u.get("is_active", True)), None
    )

    other_task = None
    for dept in unheaded:
        if by_department.get(dept["id"]):
            other_task, other_dept = by_department[dept["id"]][0], dept
            break

    # --- 3. the bid manager, who is NOT a second head -----------------------
    #
    # This section asserted the opposite until the rule changed: `task:assign`
    # used to open every door. It no longer does. The system routes work to a
    # department and that department's head names the person, so a headed
    # department is the head's exclusively -- and the bid manager keeps only
    # the departments nobody heads, which is the fallback that stops work
    # becoming unassignable.
    print("\nSARA (unscoped task:assign)")
    manager_offer = sara.get(f"/tasks/{task['id']}/assignable")
    check(
        manager_offer["scope"] == "NONE" and not manager_offer["candidates"],
        f"is offered NOBODY on {task['code']} -- {department['name']} has a head",
    )
    response = assign(sara, task, next(iter(insiders)))
    check(
        response.status_code == 403 and "headed by" in detail(response),
        f"REFUSED assigning {task['code']} ({department['name']}) -- that is "
        f"{head.name}'s to decide, and the refusal says so",
    )
    if response.status_code != 403:
        print(f"        got {response.status_code}: {detail(response)}")

    if other_task is not None and outsider is not None:
        response = assign(sara, other_task, outsider["id"])
        check(
            response.status_code == 200,
            f"assigns {other_task['code']} ({other_dept['name']}) -- no head there, so the "
            "fallback keeps the work movable",
        )

    # --- 4. the head, within their own department ---------------------------
    print(f"\n{head.name.upper()} (head of {department['name']})")
    for candidate in offer["candidates"]:
        response = assign(head, task, candidate["id"])
        check(
            response.status_code == 200,
            f"puts {candidate['display_name']} on {task['code']} -- offered, and accepted",
        )

    # --- 5. and the refusals ------------------------------------------------
    if outsider is not None:
        response = assign(head, task, outsider["id"])
        check(
            response.status_code == 403 and "not in" in detail(response),
            f"REFUSED putting {outsider['display_name']} (outside {department['name']}) on it -- "
            "a head cannot route work, and its evidence, to anyone in the org",
        )
        if response.status_code != 403:
            print(f"        got {response.status_code}: {detail(response)}")

    if other_task is not None:
        response = assign(head, other_task, outsider["id"] if outsider else head.user_id)
        # The refusal wording differs by WHY, and both are correct here: a
        # department with another head says "headed by X"; one with no head at
        # all says "has no head", because that one belongs to the bid manager.
        message = detail(response)
        check(
            response.status_code == 403 and ("headed by" in message or "has no head" in message),
            f"REFUSED touching {other_dept['name']}'s task at all",
        )
        if response.status_code != 403:
            print(f"        got {response.status_code}: {detail(response)}")
        blind = head.get(f"/tasks/{other_task['id']}/assignable")
        check(
            blind["candidates"] == [],
            "and is offered nobody on it, so the screen shows no control",
        )

    print("\nNADIA (neither permission)")
    response = assign(nadia, task, nadia.user_id)
    check(response.status_code == 403, "REFUSED at the route, before any scoped logic runs")

    # Leave the board sensible: the head's own first pick.
    assign(sara, task, offer["candidates"][0]["id"])

    print("\n" + ("-" * 70))
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("All checks passed. Delegated staffing works, and it refuses in all three")
    print("directions: wrong owner, wrong department, no permission.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
