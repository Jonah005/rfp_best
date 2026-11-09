# BidReady — Phase I & K: Tasks, Participants and Cross-Department Requests

Status: **implemented**. Sections 1–7 are the plan and acceptance contract;
section 8 records the verification performed at close.

Phase H told a bid manager what state the bid is in. Phase I turns that state
into work someone owns, and Phase K is what happens when that work needs
something from another department.

## 1. The two rules this phase exists to protect

**Task isolation.** A task carries its own `retrieval_scope`. Two contributors
working the same bid see different context because their tasks say so, not
because the application remembers to filter. Isolation that depends on every
call site behaving is not isolation.

**A request is not a grant.** A cross-department request records what was asked
and what was decided. Authority lives only in `access_grant`, which is always
task-scoped, time-bounded and revocable. The request row never carries authority
itself.

## 2. Scope

In: task creation and generation from an assessed bid; grouping rationale;
owner and participants; task dependencies; the guarded task state machine; the
per-`(task, user)` conversation row; cross-department requests with three
resolutions and rejection; the task board and both sides of the request ledger.

Out: the copilot that actually uses a conversation (Phase J); drafting and
artifacts (Phase L).

## 3. Entities

Migration `0006_tasks_collaboration`. Phase E created a deliberately minimal
`task` and `task_participant` so `access_grant` had a real parent. Their table
names were already final, so this migration **grows them in place** — an
existing grant keeps its parent row and its foreign key throughout.

| Table | Purpose |
| --- | --- |
| `task` (extended) | Adds bid, description, priority, due date, enum state, `retrieval_scope`, blocker note, grouping rationale, state-change stamps. |
| `task_requirement` | Which requirements a task covers. |
| `task_dependency` | BLOCKS / WAITS_FOR edges between tasks. |
| `task_participant` (Phase E) | Everyone who is not the single accountable owner. |
| `task_conversation` | One dialogue per `(task, user)`. |
| `chat_message` | A turn, with citations and a `retrieval_trace`. |
| `conversation_summary` | A rollup of an explicit message range. |
| `cross_department_request` | The ask, and the decision about it. |
| `requirement_request` | Which requirements a request concerns. |

`access_grant.task_id` gets the composite tenant foreign key it could not have
in Phase E, which makes a grant scoped to another organization's task
unrepresentable rather than merely unlikely.

## 4. Retrieval scope: a ceiling, not an entitlement

`compute_retrieval_scope` derives the scope from the owning department plus the
categories of the requirements the task covers. HR, Legal, Finance and
Commercial carry a `RESTRICTED` ceiling; everything else `CONFIDENTIAL`.

Storing a scope entitles nobody to anything. The context builder (Phase J)
re-evaluates every element against live grants and roles on every message, so a
scope saying `RESTRICTED` still yields nothing to a user with no grant. The
stored value answers "what may this task *ever* see"; the live check answers
"what may this person see right now".

## 5. The task state machine

`ALLOWED_TRANSITIONS` is one table, so an illegal move fails identically in a
request, a worker or a script. `BLOCKED` and `REWORK_REQUIRED` require a reason,
and clearing a block clears its note — a stale blocker reads exactly like a live
one.

`AI_READY_FOR_REVIEW` is not `COMPLETED`. The system may have something worth
reading and a human may still not have read it; collapsing those two is how a
generated draft reaches a customer unreviewed.

`seed_initial_states` moves generated tasks to the state their evidence
justifies: all-ready requirements to `AI_READY_FOR_REVIEW`, a mandatory blocker
to `BLOCKED`, anything else to `NEEDS_INPUT`. Nothing there ever claims the work
is done.

`GET /tasks/{id}/transitions` publishes the machine's own answer about what it
will accept and which moves need a reason, so a caller never guesses.

## 6. Grouping, ownership and dependencies

`generate_tasks` groups by department and category and writes a
`grouping_rationale` on every task. Requirements already covered are skipped, so
it is safe to re-run after an amendment. A requirement whose category maps to no
department is left visibly ungrouped rather than filed somewhere plausible and
lost. `NOT_APPLICABLE` requirements are never grouped.

One accountable owner sits on the task; everyone else is a `task_participant`
with a role. Removal is recorded, not deleted: who could see a task last month
is a question an auditor is entitled to ask.

`add_dependency` walks the existing edges and refuses a cycle. Two contributors
each waiting on the other is a bid that silently stops moving until the deadline
arrives.

## 7. Cross-department requests

The problem is concrete. A Security contributor drafting a staffing answer needs
to know whether a named engineer is available. Giving Security read access to HR
dissolves the boundary; refusing outright stops the bid. So the ask is explicit,
and the person who owns the information chooses how much to give:

| Resolution | What it creates |
| --- | --- |
| **Provide Answer** | Nothing. The answer text enters the requester's task context; the underlying record does not. |
| **Share Evidence** | One narrow, task-scoped, expiring READ grant per shared item. |
| **Grant Access** | The broader task-scoped grant that was asked for — still scoped and still expiring. |
| **Reject** | Nothing, and a mandatory reason. |

A `business_reason` is mandatory on every request. It is not paperwork: it is
what lets the person on the other side decide how much to give, and what an
auditor reads afterwards.

The origin task goes visibly to `WAITING_FOR_DEPENDENCY` or `WAITING_FOR_ACCESS`
while it waits, and is released only when no other request against it is still
open. A **rejected** request leaves the task `BLOCKED`, not quietly in progress —
the contributor still has a gap, and pretending otherwise is how an
unsubstantiated claim reaches a customer. An **expired** request releases
nothing, because silence resolved nothing.

`GET /requests/{id}/resolution` returns what was asked, what was decided, and
every grant it created. "Resolved" tells a reviewer nothing about whether the
boundary held; the grant list does.

## 8. Verification

### 8.1 Result

- 289 tests pass (up from 259 at Phase H close). Ruff and mypy clean.
- Migration chain 0001→0006 applies and downgrades cleanly against a fresh
  database.
- OpenAPI surface: 83 paths (up from 62).

### 8.2 Where each rule is proven

| Rule | Proof |
| --- | --- |
| Task isolation is structural | `test_a_task_carries_its_own_retrieval_ceiling` — two tasks on one bid hold different scopes, and HR's ceiling is stricter. |
| Grouping is explainable | `test_generated_tasks_explain_why_their_requirements_are_together`; re-running produces no duplicates. |
| Inapplicable work is never assigned | `test_a_requirement_that_does_not_apply_is_never_given_to_anyone`. |
| The system never claims completion | `test_initial_state_follows_the_evidence_and_never_claims_completion`. |
| Illegal moves fail loudly | `test_an_illegal_transition_is_refused_and_says_what_is_allowed`; over HTTP, `test_an_illegal_transition_over_http_is_a_problem_document`. |
| Blockers carry reasons and are cleared | `test_blocking_a_task_requires_a_reason_and_keeps_it`. |
| Conversations are private per person | `test_joining_a_task_creates_exactly_one_private_conversation`. |
| Participation history survives removal | `test_removing_a_participant_records_it_rather_than_deleting_the_row`. |
| Dependency cycles are refused | `test_a_dependency_cycle_is_refused`. |
| An answer grants nothing | `test_an_answer_resolves_a_request_without_granting_any_access`, and again over HTTP. |
| Shared evidence dies with the task | `test_sharing_evidence_creates_narrow_task_scoped_expiring_grants`. |
| Rejection blocks rather than resumes | `test_a_rejected_request_leaves_the_task_blocked_not_quietly_in_progress`. |
| One resolution does not release a task waiting on two | `test_a_task_waiting_on_two_requests_is_not_released_by_one`. |
| Silence resolves nothing | `test_an_expired_request_does_not_release_the_task`. |
| The board agrees with the list | `test_the_board_and_the_task_list_agree`. |
| Tenant isolation | `test_tasks_and_requests_do_not_cross_tenants`, plus the API-level tenant test. |

### 8.3 Defect found and fixed outside the phase scope

Migration `0001_foundation` built its tables from the *current* SQLAlchemy
metadata. That was fine while `task` was a stub, but the moment Phase I gave the
model a `bid_id`, revision 0001 began trying to create a foreign key to a table
three revisions in its own future, and the whole chain failed from empty. The
migration chain had quietly stopped being a history and become a snapshot.

Fixed by writing the Phase E `task` out explicitly in 0001 — the shape that
revision actually created — and letting 0006 grow it. The lesson generalises:
any table a later phase reshapes must be written literally in the revision that
creates it, not taken from live metadata.
