# BidReady — Phase D: API Contract

> Historical target contract. Phase D was approved before implementation began.
> Phases E and F have since implemented a verified subset of this surface; those
> handlers are real, not stubs. Routes for Phase G and later remain target
> contracts until their implementation reports and generated OpenAPI surface say
> otherwise. For the executable current contract, use `backend/openapi.json`.

FastAPI, OpenAPI 3.1. Every target route below has a planned Pydantic request and response model.

## 1. Conventions

**Base** `/api/v1`. JSON in, JSON out, UTF-8.

**Authentication** `Authorization: Bearer <access_token>`. Tokens carry `sub` (user id),
`org` (tenant id) and `jti`. The tenancy middleware resolves `org` into the session GUC and the
SQLAlchemy filter before any router runs; no handler reads `org_id` from the request body or
path, ever.

**Authorization** a `require(permission, resource_loader)` dependency runs before each handler
and delegates to `PermissionEvaluator`. A route that returns object collections applies the
same evaluator as a filter, so a user never sees a row they could not open.

**Errors** RFC 7807 problem details:

```json
{ "type": "https://bidready/errors/permission_denied",
  "title": "Permission denied",
  "status": 403,
  "detail": "Task SEC-03 retrieval scope does not include HR records.",
  "instance": "/api/v1/tasks/.../messages",
  "request_id": "0192f...",
  "remediation": { "action": "cross_department_request",
                   "target_department": "HR", "request_types": ["REQUEST_ANSWER"] } }
```

`remediation` is what lets the Copilot offer the request flow instead of a dead end.
Status codes: 400 validation, 401 unauthenticated, 403 denied, 404 not found *or* not visible
to this tenant (never distinguished), 409 state conflict / illegal transition, 422 schema,
429 rate limited, 502 model provider failure, 503 dependency unavailable.

**Pagination** cursor-based: `?limit=50&cursor=<opaque>`, response
`{ "items": [...], "next_cursor": "...", "total_estimate": 210 }`.

**Filtering and sorting** explicit query parameters per route; no generic query language.

**Long-running work** any operation that enqueues a Celery job returns `202 Accepted` with
`{ "job_id": "...", "status_url": "/api/v1/jobs/{job_id}", "stream_url": "/api/v1/jobs/{job_id}/events" }`.
`GET /jobs/{id}` returns `{state, progress, steps[], result_ref, error}`;
the stream is SSE.

**Idempotency** `Idempotency-Key` header honoured on all POSTs that create or transition.

**Concurrency** `If-Match` with an entity `version`/`etag` on mutating routes; mismatch → 409.

**Audit** every mutating route writes an `audit_event` in the same transaction. Routes accepting
a `reason` pass it through to the audit record; transitions that require a reason reject
requests without one.

## 2. Auth, Identity, Admin

```
POST   /auth/login                      {email, password}     -> {access, refresh, user}
POST   /auth/refresh                    {refresh}             -> {access}
POST   /auth/logout
GET    /auth/me                                               -> {user, roles[], departments[], permissions[]}
GET    /auth/oidc/authorize | /auth/oidc/callback             -- reserved seam, 501 in MVP

GET    /admin/users            ?department&role&active        -> paged
POST   /admin/users                     {email, display_name, roles[], departments[]}
PATCH  /admin/users/{id}                {display_name?, is_active?, roles?, departments?}
GET    /admin/departments      | POST | PATCH
GET    /admin/roles            | GET /admin/permissions
GET    /admin/organization     | PATCH  {retention_days?, residency_region?, default_language?}
```

## 3. Company Knowledge

```
POST   /knowledge/documents                 multipart: file, type?, confidentiality, owner
                                            -> 202 job (scan -> parse -> block -> chunk -> embed
                                                       -> extract knowledge)
GET    /knowledge/documents     ?type&owner&classification&status&q            -> paged
GET    /knowledge/documents/{id}                                               -> doc + versions
GET    /knowledge/documents/{id}/versions/{vid}/blocks   ?block_type&page      -> paged
GET    /knowledge/documents/{id}/versions/{vid}/download                       -> signed URL
DELETE /knowledge/documents/{id}            {reason}      -- withdraws; never hard-deletes

GET    /knowledge/entities      ?entity_type&status&q&valid_on                 -> paged
POST   /knowledge/entities                  {entity_type, canonical_name, aliases[], attributes}
GET    /knowledge/entities/{id}                    -> entity + facts + relations + evidence refs
PATCH  /knowledge/entities/{id}             {canonical_name?, aliases?, attributes?, reason}
POST   /knowledge/entities/{id}/merge       {into_entity_id, reason}
GET    /knowledge/entities/{id}/graph       ?depth=1&predicates[]              -> nodes + edges

POST   /knowledge/facts                     {entity_id, predicate, value_*, unit?,
                                             valid_from?, valid_to?, evidence_ids[]}
PATCH  /knowledge/facts/{id}                {..., reason}   -- creates a superseding row
POST   /knowledge/facts/{id}/verify         {reason}
POST   /knowledge/facts/{id}/supersede      {replacement, reason}

POST   /knowledge/relations                 {subject_entity_id, predicate, object_entity_id,
                                             attributes, valid_from?, valid_to?, evidence_ids[]}
PATCH  /knowledge/relations/{id}            {..., reason}
POST   /knowledge/relations/{id}/verify | /supersede

GET    /knowledge/evidence      ?status&expiring_before&classification&q       -> paged
GET    /knowledge/evidence/{id}                        -> evidence + source locator + links
POST   /knowledge/evidence/{id}/approve     {reason}
POST   /knowledge/evidence/{id}/reject      {reason}
POST   /knowledge/evidence/{id}/reverify    {new_expiry?, reason}

GET    /knowledge/review-queue  ?kind=CONFLICT|ENTITY_MATCH|EXPIRING|PROPOSED -> paged
POST   /knowledge/review-queue/{id}/resolve {decision, target_id?, reason}

POST   /knowledge/search                    {query, entity_types[]?, predicates[]?,
                                             valid_on?, limit}
                                            -> {results[] with source_locator, scores, why_matched}
```

`/knowledge/search` is the human-facing view of the same Retrieval Service the Copilot uses,
running through the same permission filters — so what a user finds here and what the assistant
can cite are the same set by construction.

## 4. Opportunities, Bids, Tender Intelligence

```
POST   /opportunities                       {customer_name, tender_name, tender_reference,
                                             bid_manager_id, account_manager_id, deadline,
                                             currency, region, estimated_value}
GET    /opportunities           ?stage&owner&q                                 -> paged
POST   /opportunities/{id}/promote                                             -> creates Bid

GET    /bids                    ?state&owner&deadline_before&q                 -> paged
GET    /bids/{id}                                    -> bid + readiness summary + counters
PATCH  /bids/{id}                           {submission_instructions?, language?, reason}
POST   /bids/{id}/state                     {to_state, reason}                 -> guarded transition

POST   /bids/{id}/documents                 multipart: file, document_type?, issue_date?
                                            -> 202 job
GET    /bids/{id}/documents     ?document_type&state                           -> paged
POST   /bids/{id}/documents/{vid}/classify  {document_type, reason}             -- human override

POST   /bids/{id}/precedence/resolve                                           -> 202 job
GET    /bids/{id}/precedence    ?clause_ref&unresolved_only
                                -> [{clause_ref, winning_version, competing[], method,
                                     confidence, rationale}]
POST   /bids/{id}/precedence/{clause_ref}/override
                                            {winning_document_version_id, reason}

POST   /bids/{id}/analyze                                                      -> 202 job
                                            (tender facts, requirements, conditions,
                                             deliverables, ambiguity, scope confidence)
GET    /bids/{id}/tender-facts                       -> customer, deadline, contacts,
                                                        evaluation criteria + weights,
                                                        submission rules, required forms
GET    /bids/{id}/scope-confidence                   -> level, per-category, blocking reasons

GET    /bids/{id}/requirements  ?category&mandatory&applicability&readiness&owner&q -> paged
GET    /requirements/{id}                            -> requirement + current version +
                                                        assessment + tasks + artifacts + actions
GET    /requirements/{id}/versions                                             -> history
PATCH  /requirements/{id}                   {category?, is_mandatory?, risk?,
                                             recommended_department_id?, reason}
POST   /requirements/{id}/applicability     {applicability, reason}
POST   /requirements/{id}/confirm           {reason}
POST   /requirements                        {bid_id, text, category, ...}      -- manual addition

GET    /bids/{id}/clarifications ?severity&status                              -> paged
POST   /bids/{id}/clarifications            {issue_type, description, sources, severity,
                                             recommended_question}
PATCH  /clarifications/{id}                 {status?, client_response?, reason}

GET    /bids/{id}/deliverables  | POST | PATCH /deliverables/{id}
```

## 5. Assessment, Matrices, Readiness

```
POST   /bids/{id}/assess                    {requirement_ids[]? , force?}      -> 202 job
GET    /requirements/{id}/assessment                 -> current assessment + evidence + rationale
GET    /requirements/{id}/assessment/history                                   -> versions
PATCH  /requirements/{id}/assessment        {evidence_status?, bid_readiness?, reason}
                                            -- HUMAN_ADJUSTED; reason mandatory
POST   /requirements/{id}/assessment/evidence      {evidence_id, role, weight?}
DELETE /requirements/{id}/assessment/evidence/{eid} {reason}

GET    /bids/{id}/matrices/eligibility   ?format=json|xlsx
GET    /bids/{id}/matrices/technical     ?format=json|xlsx
GET    /bids/{id}/matrices/submission    ?format=json|xlsx
GET    /bids/{id}/matrices/contract      ?format=json|xlsx

GET    /bids/{id}/readiness              -> { requirement_coverage, evidence_readiness,
                                              task_completion, approval_completion,
                                              package_readiness,
                                              counts_by_readiness{},
                                              critical_blockers, clarifications_open,
                                              waiting_dependencies, revalidation_required }
GET    /bids/{id}/readiness/{metric}/items      -- the drill-through behind every number
GET    /bids/{id}/bid-no-bid             -> current decision + factors
POST   /bids/{id}/bid-no-bid             {result, factors, rationale}
```

## 6. Tasks and Copilot

```
POST   /bids/{id}/tasks/plan                {strategy?}                        -> 202 job
                                            (department assignment + work-package grouping)
GET    /bids/{id}/tasks         ?department&owner&state&due_before             -> paged
POST   /bids/{id}/tasks                     {title, department_id, requirement_ids[],
                                             owner_id?, participants[]?, due_date?, priority}
                                            -- owner_id is the single accountable owner;
                                            -- participants[] = [{user_id, participant_role}]
GET    /tasks/{id}                          -> task + requirements + evidence + draft +
                                               dependencies + grants + blockers
PATCH  /tasks/{id}                          {title?, owner_id?, due_date?,
                                             priority?, requirement_ids?, reason}
GET    /tasks/{id}/participants                                                 -> list
POST   /tasks/{id}/participants             {user_id, participant_role}
DELETE /tasks/{id}/participants/{pid}       {reason}
POST   /tasks/{id}/state                    {to_state, reason}                 -> guarded
POST   /tasks/{id}/dependencies             {to_task_id, dependency_type, note}
DELETE /tasks/{id}/dependencies/{did}
GET    /tasks/{id}/retrieval-scope          -> the effective scope, human-readable
GET    /me/work                 ?bucket=needs_input|ready_for_approval|in_progress|
                                        waiting|incoming|completed                -> paged

GET    /tasks/{id}/conversations                     -> conversations visible to this caller.
                                                        READ-ONLY: never creates a row. Returns
                                                        an empty list if none exists yet.
POST   /tasks/{id}/conversations                    {}   -> 201 (or 200 if one already exists)
                                                        Explicit creation. Also created by the
                                                        assignment service when a user becomes
                                                        owner or participant, so the common path
                                                        never needs this call.
POST   /tasks/{id}/conversations/{cid}/messages     {content}
                                            -> 202 {user_message_id, assistant_message_id,
                                                    stream_url}
                                               The assistant message row is created immediately
                                               in a PENDING state; generation runs server-side
                                               and is not tied to this connection.
GET    /tasks/{id}/conversations/{cid}/messages/{mid}/stream
                                            -> text/event-stream of tokens for that message.
                                               Reconnectable: supports Last-Event-ID to resume
                                               from an offset, and returns the completed message
                                               immediately if generation already finished.
                                               Losing the connection never loses the answer.
GET    /tasks/{id}/conversations/{cid}/messages     ?before&limit                -> paged history
POST   /tasks/{id}/conversations/{cid}/retry        {message_id}
GET    /tasks/{id}/conversations/{cid}/context-preview
                                            -> what would be sent: base context summary,
                                               retrieved candidates, filters applied
                                               (debug/transparency; permission-checked)

-- A task's draft IS its ResponseArtifact's newest ArtifactVersion while that version is in
-- state DRAFT. These routes are addressing conveniences over that object, not a second model;
-- each resolves the task's artifact and its current DRAFT version, creating a new DRAFT version
-- if the newest one has already left DRAFT.
POST   /tasks/{id}/draft/generate           {requirement_ids[]?, instructions?}  -> 202 job
GET    /tasks/{id}/draft                    -> {artifact_id, version_id, version_no, state,
                                                statements[] with class, provenance, approval}
PUT    /tasks/{id}/draft/statements         {statements: [{id?, class, text, order_index,
                                                           source_locator?, evidence_ids?,
                                                           commitment?, assumption?}]}
                                            409 if the current version is no longer DRAFT
POST   /tasks/{id}/draft/statements/{sid}/approve   {reason?}
DELETE /tasks/{id}/draft/statements/{sid}
POST   /tasks/{id}/evidence                 {evidence_id}      -- attach existing
POST   /tasks/{id}/upload                   multipart          -- new supporting evidence
POST   /tasks/{id}/submit-for-review        {reviewer_id?, note?}
```

`context-preview` exists deliberately: being able to show a user (and an auditor) exactly what
the assistant was allowed to see for a given turn is what makes the isolation claim checkable
rather than asserted.

## 7. Cross-Department Requests and Access Grants

```
POST   /requests                            {origin_task_id, request_type, target_department_id,
                                             target_user_id?, requirement_ids[], request_text,
                                             business_reason,
                                             requested_resource_type?, requested_resource_id?}
GET    /requests                ?direction=incoming|outgoing&state&department    -> paged
GET    /requests/{id}                                -> request + origin task + requirements
POST   /requests/{id}/acknowledge
POST   /requests/{id}/answer                {answer_text, evidence_ids[]?}
POST   /requests/{id}/share-evidence        {evidence_ids[], expires_at?}
                                            -- creates one narrow task-scoped READ AccessGrant
                                            -- per evidence id, over those ids only
POST   /requests/{id}/grant-access          {resource_type, resource_id, permission,
                                             expires_at, reason}
                                            -- creates the broader requested task-scoped grant
POST   /requests/{id}/reject                {reason}                            -- reason required
POST   /requests/{id}/cancel                {reason}                            -- requester only

GET    /grants                  ?task_id&grantee&resource_type&effective_state   -> paged
                                -- effective_state filter is evaluated against
                                -- (revoked_at IS NULL AND expires_at > now()), not a column
GET    /grants/{id}
POST   /grants/{id}/revoke                  {reason}
GET    /me/grants                                    -> what I currently have access to and why
```

Three distinct endpoints rather than one polymorphic resolve, because the three outcomes have
genuinely different consequences, and the API surface should make the least-privilege choice
visible in both the route and the audit log:

| Route | Grant created | Scope |
|---|---|---|
| `answer` | none | nothing new becomes readable; the answer text is attached to the task |
| `share-evidence` | one per evidence id | task-scoped, `READ`, limited to exactly the shared evidence ids, expiring |
| `grant-access` | one | task-scoped, expiring, revocable, over the requested resource |

`GET /grants` and `GET /me/grants` report effective state computed live from `expires_at` and
`revoked_at`; no endpoint returns a stored state value as authoritative.

## 8. Artifacts and Approvals

```
POST   /tasks/{id}/artifact                 {title}    -- promotes approved draft to artifact v1
GET    /artifacts              ?bid_id&task_id&state&requirement_id             -> paged
GET    /artifacts/{id}                               -> artifact + current version + statements
GET    /artifacts/{id}/versions                      -> full version history
POST   /artifacts/{id}/versions             {statements[], change_reason}
GET    /artifacts/{id}/versions/{vno}                -> immutable snapshot

GET    /approvals              ?entity_type&state&mine=true                     -> paged
POST   /approvals/{id}/decide               {decision, comment?}
GET    /approvals/chain        ?entity_type&entity_id     -> ordered steps, who, when, outcome
POST   /reviews                             {entity_type, entity_id, decision, comment}

GET    /bids/{id}/commitments   ?approval_status         | POST /commitments/{id}/approve
GET    /bids/{id}/assumptions   ?status                  | PATCH /assumptions/{id}
GET    /bids/{id}/actions       ?status&owner&department | POST | PATCH /actions/{id}
```

## 9. Assembly, Validation, Release

```
GET    /bids/{id}/structure                          -> ProposalStructure + section tree
POST   /bids/{id}/structure/derive          {source_document_version_id?}       -> 202 job
PATCH  /bids/{id}/structure/sections/{sid}  {title?, order_index?, target_document_id?,
                                             required_format?, is_mandatory?, reason}
POST   /sections/{sid}/artifacts            {artifact_id, order_index, inclusion_mode,
                                             excerpt_statement_ids?}
DELETE /sections/{sid}/artifacts/{aid}
GET    /bids/{id}/structure/coverage        -> sections with no artifact; artifacts unmapped

POST   /bids/{id}/assemble                  {output_mode}                        -> 202 job
GET    /bids/{id}/generated-documents                -> documents + versions + formats + stream
GET    /generated-documents/{id}/versions/{vno}/download                         -> signed URL

POST   /bids/{id}/packages                  {output_mode}                        -> creates package
GET    /bids/{id}/packages                                                       -> paged
GET    /packages/{id}                                -> package + items + manifest + state
POST   /packages/{id}/validate                                                   -> 202 job
GET    /packages/{id}/validation            ?severity&waivability
                                            -> findings grouped: non_waivable[], waivable[],
                                               warnings[]
POST   /findings/{id}/exception             {justification}
POST   /exceptions/{id}/decide              {decision, comment}
                                            -- 409 if the finding is NON_WAIVABLE
POST   /packages/{id}/submit-for-release    {note?}                              -- maker
POST   /packages/{id}/approve-release       {comment?}                           -- checker,
                                            409 if the same user as the maker
GET    /packages/{id}/manifest              ?format=json|csv
POST   /packages/{id}/archive               {reason}

POST   /bids/{id}/outcome                   {result, technical_score?, commercial_score?,
                                             award_value?, competitor?, loss_reason?,
                                             evaluator_feedback?}
GET    /bids/{id}/outcome
GET    /knowledge/update-candidates ?state                                       -> paged
POST   /knowledge/update-candidates/{id}/decide     {decision, comment}
```

## 10. Audit, Jobs, Analytics, Health

```
GET    /audit                   ?bid_id&entity_type&entity_id&actor&action&from&to  -> paged
GET    /audit/{entity_type}/{entity_id}              -> full trail for one object

GET    /jobs/{id}                                    -> {state, progress, steps[], result_ref, error}
GET    /jobs/{id}/events                             -> SSE progress stream
POST   /jobs/{id}/cancel

GET    /analytics/portfolio     ?from&to             -> volume, turnaround, reuse rate,
                                                        bottlenecks, win/loss, efficiency
GET    /analytics/bids/{id}/timeline                 -> stage durations and delays

GET    /health/live | /health/ready                  -> db, redis, storage, llm, embeddings
GET    /system/model-usage      ?from&to&call_type   -> tokens, latency, failures by call type
```

## 11. What Phase D Produces for Review

1. `openapi.json` generated from the stubs, covering every route above.
2. A route-to-screen matrix confirming every wireframe element in `04` has an endpoint behind it.
3. A route-to-transition matrix confirming every state transition in `05` has exactly one route
   that can cause it, with its guard and required permission named.
4. A permissions matrix: route × role × object condition, which becomes the test fixture for the
   permission suite in Phase E.

At Phase D approval, handlers were intentionally unimplemented. That statement is historical:
Phases E and F now provide implemented handlers for their delivered scope, while later-phase
routes remain targets.
