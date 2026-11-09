# BidReady — Phase J: The Task Copilot

Status: **implemented**. Sections 1–6 are the plan and acceptance contract;
section 7 records the verification performed at close.

This is the phase where a language model finally talks to a user, and it is the
phase with the least room for the model to decide anything. Everything that
matters — what it may see, what it may cite, what happens next — is computed
before and after the call.

## 1. The rule this phase exists to protect

> The model does not retrieve. It receives a bounded context block assembled
> against live permissions, and that block is the entirety of what it can draw
> on.

This is why prompt injection in an uploaded document cannot widen access.
Document text arrives as data inside an already-bounded block; nothing it says
can add a source to that block, because the block was closed before the model
saw it. The model has no tools, no query path and no write path. That is an
architectural guarantee, not a prompt instruction — and the difference matters,
because prompt instructions are exactly what an injected document attacks.

## 2. Scope

In: the context builder; per-`(task, user)` conversations; the three-layer
memory model; citation recording; the informative out-of-scope refusal;
conversation rollup; the 202-then-stream ask path; and the inspection routes
(`context-preview`, `transcript`, `retrieval-trace`).

Out: drafting statements and artifacts (Phase L). The copilot writes prose into
a conversation; it does not write a response document.

## 3. Context construction

`app/services/context_builder.py` is the security-critical module. Four rules
govern it:

**Membership first.** A person who is not the task's owner or an active
participant gets no context at all, whatever their organizational role. If any
bid manager could read every task's context, task isolation would be decoration.

**Nothing is trusted from cache.** The task's stored `retrieval_scope` is a
ceiling — what the task may *ever* see. What it sees on this message is that
ceiling narrowed by the caller's live roles and live grants, evaluated now,
from `expires_at`/`revoked_at` rather than from `materialized_state`. A grant
revoked a second ago is gone from this call.

**Refusal is informative.** When nothing admissible exists, the answer names the
departments that likely hold the material and offers the three request types in
least-privilege order. A contributor who cannot proceed still needs a route
forward; a flat "no" just stops the bid.

**Everything is traced.** The `retrieval_trace` records the query, the filters
applied, the count admissible before ranking, the live grants considered, and
which candidates survived. A leak that cannot be investigated afterwards is a
leak that gets denied rather than fixed.

The assembled block also carries the task's requirements with their **current
Phase H assessment**, and attached evidence **with its currentness and expiry**.
The two-axis distinction survives into the prompt rather than being flattened on
the way in, so the copilot cannot present an expired certificate as a live one.

## 4. Conversation memory, in three layers

1. **Recent turns, verbatim** — the last 12, so the dialogue is coherent.
2. **Rolled-up summaries** — older turns compressed into `conversation_summary`
   rows covering an **explicit message range**. A summary that cannot say what it
   replaces cannot be safely regenerated and quietly becomes a second source of
   truth. Nothing is deleted to make room.
3. **Reconstructed context, every message** — requirements, evidence, received
   answers and freshly retrieved chunks, rebuilt each time.

Layer 3 is the one that matters. A naive implementation appends retrieved
passages to a growing transcript, which means a passage retrieved while a grant
was active stays in the conversation after that grant is revoked. Rebuilding per
message makes revocation take effect on the next question.

Conversations are per `(task, user)` and private to their person — not even the
task owner reads someone else's working thread. Half-formed exploration is
exactly the material that should not travel.

## 5. Citations

The answer's cited refs are parsed out of the text and stored **separately from
what was offered**. An answer citing two of eight sources is a different thing
from one that had two available, and a reviewer needs to see which. An
uncited answer records no citations rather than having some attributed to it.

## 6. Asking: 202, then stream

`POST /conversations/{id}/messages` returns **202** with a job id and a stream
URL. A request that blocks for the length of a model call is a request that gets
cut by a load balancer.

The work runs in a worker thread with its own session and its own
transaction-local tenant binding, and it **commits the message and its trace
before marking the job done**. A dropped stream therefore costs the animation and
never the answer — `GET /conversations/{id}/messages` has it either way. The job
store is in-process and bounded because it holds fragments in flight, not
durable state; a job id is scoped to its organization on read, since it is
effectively a bearer token for whatever the job produced.

`GET /tasks/{id}/conversation` **does not create** a conversation. Creation is a
POST. A read that writes makes conversation counts meaningless and lets a
membership check be sidestepped by merely opening a page.

## 7. Verification

### 7.1 Result

- 311 tests pass (up from 289 at Phase I/K close). Ruff and mypy clean.
- OpenAPI surface: 91 paths (up from 83).

### 7.2 Where each rule is proven

| Rule | Proof |
| --- | --- |
| Membership gates context | `test_someone_not_on_the_task_gets_no_context_at_all` — the owner sees material, an outsider in the same tenant is refused. |
| Conversations are private | `test_a_conversation_is_private_to_its_person`. |
| Reads never create | `test_reading_a_task_never_creates_a_conversation`; over HTTP, `test_a_conversation_must_be_created_explicitly`. |
| The ceiling holds | `test_the_task_ceiling_keeps_restricted_material_out_of_the_prompt` — the roster's text is absent while the admissible certificate *is* present, so the exclusion is real rather than an empty prompt. |
| Refusal offers a route | `test_out_of_scope_offers_the_three_requests_in_least_privilege_order` — and the model is never called at all. |
| Revocation is immediate | `test_a_revoked_grant_takes_effect_on_the_next_question`. |
| Offered ≠ cited | `test_every_answer_records_what_was_offered_and_what_was_cited` and `test_an_uncited_answer_records_no_citations_rather_than_inventing_them`. |
| Long threads roll up losslessly | `test_a_long_thread_rolls_up_and_the_summary_says_what_it_covers`. |
| The copilot moves no workflow | `test_task_state_is_never_changed_by_asking` — an instruction to complete the task changes nothing. |
| 202-then-stream, durably | `test_asking_returns_202_and_the_answer_arrives_on_the_stream`, `test_a_job_can_be_polled_instead_of_streamed`. |
| Jobs are tenant-scoped | `test_another_tenant_cannot_read_a_job`. |
| The boundary is inspectable | `test_the_context_preview_shows_the_boundary_without_asking_the_model`, `test_the_retrieval_trace_is_reachable_for_any_answer`. |

### 7.3 Decision taken during implementation

**The job store is in-process, deliberately.** It could have been Redis-backed
to survive a restart, but the only thing it holds is a stream of events for an
answer that is already committed to Postgres. Making it durable would add
infrastructure to protect an animation. If a future requirement needs jobs to
survive a deploy, the answer is a Celery task with a database-backed status row,
not a bigger version of this store.

## 8. Next: Phase L — Typed statements, response artifacts, reviews and approvals
