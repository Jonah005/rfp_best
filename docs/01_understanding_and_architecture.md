# BidReady — Phase A (Revised): Product Understanding, Architecture, Workflow

Revision 2 — incorporates the 13 architecture corrections from review.
Status: Phase A approved in substance; this revision is the corrected baseline that Phases B/C/D build on.

## Changelog vs Revision 1

| # | Correction | Where it lands |
|---|---|---|
| 1 | Knowledge layer becomes a graph-like model (`KnowledgeEntity`/`KnowledgeFact`/`KnowledgeRelation`/`Evidence`/`EvidenceLink`) | Sec 4.2, Sec 5, doc 02 Sec 10 |
| 2 | Explicit document ingestion entities (`Document`…`SourceLocator`) | Sec 4.3, Sec 5, doc 02 Sec 10 |
| 3 | `LLMProvider` and `EmbeddingProvider` are separate interfaces | Sec 5, Sec 7 |
| 4 | `RequirementAssessment` + `AssessmentEvidence` replace `ComplianceAssessment` | Sec 4.5, doc 02 Sec 10 |
| 5 | `Task` 1—* `TaskConversation` (per-user isolation) | Sec 6, Sec 9 |
| 6 | Context Builder = fixed base context + permission-aware query-time retrieval | Sec 9 (rewritten) |
| 7 | Explicit `AccessGrant` objects | Sec 4.8, Sec 8 |
| 8 | Four drafting input classes, not "approved evidence only" | Sec 7.3 (new) |
| 9 | Document precedence resolved per clause, not hardcoded | Sec 3, Sec 6 |
| 10 | Requirement lifecycle simplified; coverage lives in assessment/task/artifact | Sec 6 |
| 11 | `ProposalStructure`/`ProposalSection`/`SectionArtifactMap`/`GeneratedDocument(+Version)` | Sec 4.11, doc 02 Sec 10 |
| 12 | Waivable business exceptions vs non-waivable security/system blockers | Sec 6, Sec 7.4 |
| 13 | Phase B now includes UI wireframes and user flows before the schema is locked | doc 02 Sec 11 |

---

## 1. Product Understanding

BidReady is an **AI-assisted Bid / RFP Operating System**. The LLM assists inside a deterministic workflow; the workflow, permissions, knowledge governance, approvals and release gates are application logic and must remain correct even if the model is wrong or switched off.

The distinction the whole design hangs on:

- **Tender-side documents** (RFP, RFQ, amendments, clarifications, BOQ, client templates) are the only source of *requirements*.
- **Company-side documents** (profile, CVs, certificates, past proposals, methodologies, rate cards) are *evidence* — what the bidder knows or can prove. Evidence answers requirements; it never redefines them.

End to end, the system turns an RFP into a validated, approved, packaged submission while keeping every statement traceable to a typed, governed source; every workflow transition auditable; and every human approval a real gate.

Business outcome (BRD): complete, compliant, consistent, professionally formatted responses faster, with humans retaining control over all claims and commitments.

## 2. Actors

| Actor | Concern |
|---|---|
| Organization Admin | Tenant setup, users, roles, retention policy |
| Bid Manager | Owns a bid end-to-end: intake, assignment, readiness, release |
| Account Manager / Sales | Opportunity context, client relationship |
| Technical Contributor / Solution Architect | Technical requirements, architecture responses |
| Project Manager | Implementation plan, staffing, schedule |
| Security | Security questionnaires, certificates, VAPT methodology |
| HR | CVs, certifications, resource availability |
| Commercial / Finance | Pricing, BOQ, AMC, tax, warranty |
| Legal | Declarations, terms, deviations, subcontractor and legal data |
| Reviewer (per-domain) | Technical / Security / Legal / Commercial review gates |
| Executive Approver | Final sign-off, release authority, exception authority |
| Knowledge Administrator | Approves canonical knowledge changes and supersessions |
| Auditor | Read-only traceability across everything |

All actors belong to exactly one Organization (tenant). A user may hold multiple roles and belong to multiple departments. The AI Copilot is a capability, never an actor with authority — it has no user identity and cannot hold a permission.

## 3. End-to-End Workflow

```
Company Knowledge (governed, ongoing)
  documents ingested -> blocks/chunks/embeddings -> entities/facts/relations -> evidence-linked, governed
        |
Opportunity created -> Bid -> tender package uploaded
        |
Ingestion + classification (per document, per version)
        |
Precedence resolution PER CLAUSE  (issue date, version, explicit supersession,
   affected clauses, customer-stated precedence  ->  ClauseResolution)
        |
Tender Understanding (customer, deadline, contacts, submission rules, evaluation)
        |
Atomic Requirement Extraction  (+ Ambiguity Engine  + Scope Confidence Gate)
        |
Deliverable Register extraction
        |
Requirement Assessment  (hybrid retrieval over knowledge graph + chunks, reranking)
   -> RequirementAssessment (evidence_status x bid_readiness) + AssessmentEvidence
        |
Matrices (Eligibility, Technical Compliance, Submission Checklist, Contract Register)
        |
Bid / No-Bid Gate (GREEN / AMBER / RED)
        |
Ownership recommended -> requirements grouped into Work Packages (Tasks) per department
        |
Task Workspace: per-user Copilot conversation
   base context + permission-aware query-time retrieval
        |
Human contribution -> Cross-department requests when info is outside scope
   REQUEST ANSWER | REQUEST EVIDENCE | REQUEST ACCESS -> AccessGrant when granted
        |
Draft assembled from FOUR typed statement classes
   evidence-backed fact | tender-derived | proposed commitment | assumption/human input
        |
Approval chain -> ResponseArtifact (immutable, versioned)
        |
Amendment arrives at any time -> per-clause re-resolution -> impact analysis
   -> affected assessments/tasks/artifacts marked REVALIDATION_REQUIRED
        |
Proposal Assembly: ProposalStructure -> ProposalSection -> SectionArtifactMap -> artifacts
        |
Cross-Section Consistency Check -> ResolutionTask on conflict
        |
Validation Engine -> ValidationFinding(severity, waivability)
        |
Release Gate:  NON-WAIVABLE findings block absolutely
               WAIVABLE findings block until an authorized exception is approved
        |
GeneratedDocument(+Version) -> Package -> Manifest (Review mode / Submission mode)
        |
Maker-checker Release -> immutable -> Archive
        |
Win/Loss Outcome -> KnowledgeUpdateCandidate -> Knowledge Admin approval -> canonical knowledge
```

Two loops must be first-class, not bolted on: **amendments** (can invalidate approved work at any point after intake) and **learning** (approved corrections *propose*, never silently apply, canonical knowledge changes).

## 4. Major Subsystems

**4.1 Identity, Tenancy & Permissions** — organizations, users, departments, roles, RBAC, object-level permissions, `AccessGrant` evaluation, one `PermissionEvaluator` used by every read path.

**4.2 Knowledge Graph** — `KnowledgeEntity` (Person, Project, Certification, Client, Product, Policy, LegalEntity, RateCard, …), `KnowledgeFact` (attribute assertions), `KnowledgeRelation` (typed edges such as `HAS_CERTIFICATION`, `WORKED_ON`, `EMPLOYED_BY`, `DELIVERED_FOR`), each carrying temporal validity, currentness, approval, supersession and provenance through `EvidenceLink → Evidence`. Held in PostgreSQL; no graph database for MVP.

**4.3 Document Ingestion & Search Layer** — `Document` → `DocumentVersion` → `DocumentBlock` (structural: heading, paragraph, table, table row, list item, figure caption) → `DocumentChunk` (retrieval unit) → `Embedding`, with a `SourceLocator` capturing page, section path, heading, table name, row/column index and character offsets. This is what retrieval actually searches and what every citation points at. Serves both tender-side and company-side documents through one pipeline.

**4.4 RFP Intelligence** — tender fact extraction, atomic requirement extraction, requirement classification, conditional-requirement evaluation, ambiguity engine, scope confidence gate, deliverable register, and the per-clause precedence resolver.

**4.5 Assessment Engine** — hybrid retrieval, reranking, and production of `RequirementAssessment` (one current assessment per requirement, versioned) with many `AssessmentEvidence` links. Matrices and readiness metrics are deterministic aggregations over assessments.

**4.6 Task & Collaboration Workflow** — ownership recommendation, work-package grouping, task state machine, dependencies, personal dashboards. A task has exactly one accountable owner and any number of `TaskParticipant`s (contributor, reviewer, approver, observer).

**4.7 Task Copilot & Context Builder** — per-user, per-task conversations; a deterministic context builder that assembles base context and performs permission-aware query-time retrieval.

**4.8 Cross-Department Requests & Access Grants** — three request types, resolution back into the origin task, and `AccessGrant` records for genuine access delegation with scope, expiry and revocation.

**4.9 Response Artifacts & Approvals** — typed statements, artifact versioning and immutability, generic approval engine reused across tasks, artifacts and packages. A task's working draft is not a separate model: it is the artifact's newest `ArtifactVersion` while that version is in state `DRAFT`.

**4.10 Amendment Impact** — document diffing, per-clause re-resolution, requirement versioning, and propagation of `REVALIDATION_REQUIRED` to assessments, tasks and artifacts.

**4.11 Proposal Assembly & Generation** — `ProposalStructure` (derived from the tender's requested structure) → `ProposalSection` tree → `SectionArtifactMap` (many-to-many with `ResponseArtifact`) → `GeneratedDocument` → `GeneratedDocumentVersion`; DOCX/XLSX/PDF rendering; technical/commercial separation.

**4.12 Validation & Release** — rule engine producing `ValidationFinding`s classified by severity and waivability; maker-checker release; manifest; immutability; archive and retention.

**4.13 Audit** — append-only `AuditEvent` for every meaningful transition, sufficient to answer "why does this number say what it says".

**4.14 Model Gateway** — two independent provider interfaces (LLM and Embedding), per-call-type prompts, structured-output validation, usage logging.

**4.15 Analytics & Learning** — outcomes, portfolio metrics, knowledge update candidates.

## 5. Logical Architecture

```
+---------------------------- Frontend (Next.js / React / TypeScript) ----------------------------+
| Company Knowledge | Bid Dashboard | Matrices | My Work | Task Workspace + Copilot |             |
| Incoming Requests | Approvals | Bid Readiness | Release & Package | Admin / RBAC                |
+-------------------------------------------+----------------------------------------------------+
                                            | REST/JSON + SSE (chat, job progress)
                                            | every request tenant-scoped and RBAC-checked
+-------------------------------------------v----------------------------------------------------+
| API Layer (FastAPI + Pydantic)                                                                  |
|   AuthN -> tenant scoping middleware -> RBAC dependency -> per-module routers                   |
+-------------------------------------------------------------------------------------------------+
| Domain Services (deterministic - the "operating system" layer)                                   |
|   Bid Lifecycle | Ingestion Orchestrator | Precedence Resolver | Requirement Register            |
|   Knowledge Graph Service | Evidence Governance | Retrieval Service | Assessment Engine          |
|   Task Workflow | Cross-Dept Requests | Access Grant Service | Approval Engine                   |
|   Response Artifact Store | Amendment Impact | Assembly & Generation | Validation Engine         |
|   Package & Release Manager | Audit Logger | Permission Evaluator                                |
+-------------------------------------------------------------------------------------------------+
| AI Context Builder  (the ONLY path from a conversation to retrieval)                             |
|   base context assembly -> permission + scope filter -> Retrieval Service -> prompt assembly     |
+-------------------------------------------------------------------------------------------------+
| Model Gateway (two independent interfaces)                                                       |
|   LLMProvider        -> DeepSeekProvider        (chat, generate, generate_structured)            |
|   EmbeddingProvider  -> LocalEmbeddingProvider  (multilingual sentence embeddings)               |
|   shared: call-type registry, prompt versioning, schema validation + repair, retries,            |
|           timeouts, token accounting, request IDs, usage logging                                 |
+-------------------------------------------------------------------------------------------------+
| Background Workers (Celery + Redis)                                                              |
|   virus scan | parse | block/chunk | embed | extract knowledge | extract requirements            |
|   assess | amendment impact | assemble | validate | generate | export | retention                |
+------------------+--------------------------------------+---------------------------------------+
                   |                                      |
     +-------------v-------------+        +---------------v---------------+
     | PostgreSQL + pgvector     |        | Object Storage (S3-compatible)|
     | system of record:         |        | MinIO in dev; provider-       |
     | all entities, knowledge   |        | abstracted, UAE-ready in prod |
     | graph, chunks, vectors,   |        | raw uploads, generated docs,  |
     | audit log                 |        | packages                      |
     +---------------------------+        +-------------------------------+
```

Architectural rules:

1. The Model Gateway is the only component that talks to a model provider, and it is called only for named call types. No inline prompts anywhere else.
2. `LLMProvider` and `EmbeddingProvider` are separate interfaces with separate configuration and separate failure handling. Embeddings run locally (multilingual model) so that re-embedding the corpus never depends on an external vendor and no company text needs to leave the deployment for indexing.
3. Every domain service call carries `(org_id, actor_user_id, …)` and enforces tenant scope in middleware, not per-endpoint discipline.
4. The Copilot never queries storage directly. It goes through the AI Context Builder, which goes through the Retrieval Service, which goes through the Permission Evaluator — the same evaluator that serves UI reads.
5. Background workers write through the same tenant-aware repository and domain services as the API. They open their own transaction, set the tenant context transaction-locally, and are subject to the same RLS policies, permission checks, transition guards and audit writes. There is no privileged worker write path.

## 6. Workflow State Machines (revised)

**Document / precedence** — precedence is *resolved*, not assumed.

```
DocumentVersion:  UPLOADED -> SCANNING -> SCAN_FAILED
                            -> PARSING  -> PARSE_FAILED
                            -> PARSED -> CLASSIFIED -> INDEXED -> ACTIVE
                                                                -> SUPERSEDED (by DocumentRelation)

Precedence inputs per document version: issue_date, version_label, document_type,
  explicit supersession (DocumentRelation: AMENDS | CLARIFIES | SUPERSEDES | REPLACES_CLAUSE),
  affected_clause_refs, customer_stated_precedence (text extracted from the tender itself).

Resolution: for each clause reference the resolver produces a ClauseResolution
  { clause_ref, winning_document_version_id, competing_version_ids,
    method = EXPLICIT_SUPERSESSION | CUSTOMER_STATED_RULE | ISSUE_DATE | VERSION_LABEL | MANUAL,
    confidence, resolved_by, resolved_at }
Unresolvable or low-confidence -> ClarificationIssue, and the affected requirements are
flagged NEEDS_CLIENT_CLARIFICATION rather than silently choosing a winner.
```

**Bid**

```
DRAFT -> INTAKE_IN_PROGRESS -> UNDERSTANDING_COMPLETE -> ASSESSMENT_COMPLETE
      -> BID_NO_BID_DECIDED -> IN_PROGRESS -> ASSEMBLY -> VALIDATION
      -> RELEASE_PENDING -> RELEASED -> ARCHIVED
any state -> NO_BID | CANCELLED
amendment at any point after INTAKE -> scoped revalidation, bid returns to IN_PROGRESS
```

**Requirement (deliberately thin — correction 10)**

Requirement carries identity, content and applicability only. It does not mirror downstream workflow.

```
lifecycle_state:   DRAFT_EXTRACTED -> CONFIRMED -> (SUPERSEDED | WITHDRAWN)
applicability:     ACTIVE | NOT_APPLICABLE | CONDITIONAL_PENDING
                   (conditional requirements stay CONDITIONAL_PENDING until their
                    condition is evaluated; NOT_APPLICABLE never counts as a gap)
versioning:        an amendment that changes wording creates RequirementVersion N+1 and marks
                   version N superseded. The Requirement itself stays CONFIRMED — same identity,
                   same code, same task/artifact/assessment links. The Requirement is only
                   SUPERSEDED when its identity is replaced (split or merged), and WITHDRAWN
                   when the clause is removed.
```

Coverage and readiness are **derived**, and live in `RequirementAssessment` (+ the tasks and artifacts that reference the requirement). A requirement may participate in several tasks and several artifacts at once without contradiction.

**RequirementAssessment (two orthogonal axes — correction 4)**

```
evidence_status:  CURRENT_VERIFIED | HISTORICAL_VERIFIED | PARTIAL | CONFLICTING | NONE | EXPIRED
bid_readiness:    READY | DRAFTABLE | NEEDS_REFRESH | NEEDS_COMPANY_CONFIRMATION |
                  NEEDS_CLIENT_CLARIFICATION | REQUIRES_SIGNATURE | BLOCKED | NOT_APPLICABLE
assessment_state: PENDING -> COMPUTED -> HUMAN_ADJUSTED -> STALE (input changed) -> recomputed
```

**Task**

```
NOT_STARTED -> AI_READY_FOR_REVIEW | NEEDS_INPUT | BLOCKED
            -> IN_PROGRESS  <-> WAITING_FOR_DEPENDENCY
                            <-> WAITING_FOR_ACCESS
            -> READY_FOR_APPROVAL -> REWORK_REQUIRED -> IN_PROGRESS
            -> APPROVED -> COMPLETED
any state (amendment) -> REVALIDATION_REQUIRED -> IN_PROGRESS
```

**CrossDepartmentRequest**

```
PENDING -> ACKNOWLEDGED -> ANSWERED | EVIDENCE_SHARED | ACCESS_GRANTED
PENDING -> REJECTED | CANCELLED | EXPIRED
ANSWERED creates no grant. EVIDENCE_SHARED creates one narrow task-scoped READ AccessGrant per
shared evidence item. ACCESS_GRANTED creates the broader requested task-scoped grant.
Resolution updates the origin task and appends the result to that task's authorized context.
```

**AccessGrant (correction 7)**

```
expires_at and revoked_at are authoritative; effective state is evaluated live on every
permission check (revoked_at IS NULL AND expires_at > now()) rather than read from a stored
column, so a revocation takes effect on the next query with no job latency.
An expiry sweep may materialize a state value for listing screens only.
Scope is always (grantee_user, task, resource) — never a blanket user-level grant.
```

**ResponseArtifact**

```
DRAFT -> SUBMITTED_FOR_REVIEW -> IN_REVIEW -> APPROVED (immutable version N)
                                          -> REWORK_REQUIRED -> DRAFT
APPROVED -> REVALIDATION_REQUIRED (amendment/evidence change) -> new version N+1
APPROVED -> SUPERSEDED when version N+1 is approved; N is retained forever
```

**Package / Release (correction 12)**

```
ASSEMBLING -> REVIEW_MODE_READY -> VALIDATION_RUNNING -> VALIDATION_COMPLETE
VALIDATION_COMPLETE evaluates findings:
   any NON_WAIVABLE finding open  -> BLOCKED_HARD   (no override path exists in the product)
   only WAIVABLE findings open    -> BLOCKED_PENDING_EXCEPTION
                                     -> EXCEPTION_REQUESTED -> EXCEPTION_APPROVED (authorized role)
   no blocking findings           -> RELEASE_PENDING_APPROVALS
RELEASE_PENDING_APPROVALS -> RELEASED (immutable) -> ARCHIVED
post-release change -> new Package version; the released one is never mutated
```

Non-waivable classes (no exception workflow, ever): cross-tenant data leakage, permission-model violation, malware detection, corrupted or unreadable generated file, and unauthorized disclosure of restricted content (personal data, restricted pricing, privileged legal material) in an outgoing package. Everything else — unanswered mandatory requirement, unsupported claim, missing signature, missing required file, price appearing in a technical response — is blocking but waivable through a recorded, authorized exception, per BRULE-06 and the BRD's release rules.

## 7. AI vs Deterministic Responsibilities

**7.1 The LLM may:** classify and extract from text, decompose requirements, evaluate conditions, detect ambiguity and contradictions, propose evidence matches and rerank candidates, explain gaps, recommend department ownership, propose task groupings, draft response text, summarize conversations, and assist validation as an advisory signal.

**7.2 Application logic must own:** identity and authentication, RBAC and object permissions, tenant isolation, all state transitions, evidence and knowledge approval status, temporal validity and expiry, versioning and immutability, precedence resolution outcomes of record, hard release blockers, audit logging, retention, and — critically — *what context is permitted to reach any model call*. The model returns schema-validated proposals; a deterministic service accepts or rejects them through normal write paths. The model never writes to the system of record.

**7.3 Four drafting input classes (correction 8).** Drafting is not restricted to approved evidence. Every statement in a draft is typed, and the type determines what proof it needs and how it is governed:

| Statement class | Meaning | Required backing | Governance |
|---|---|---|---|
| `EVIDENCE_BACKED_FACT` | A claim about the company as it is | ≥1 `Evidence` via `EvidenceLink`, current and approved | Blocks release if unsupported or expired |
| `TENDER_DERIVED` | Restating or responding to something the tender says | `SourceLocator` into the winning tender clause | Must cite the clause that survived precedence resolution |
| `PROPOSED_COMMITMENT` | Something we undertake to do | Named owner + approval by the authorized role | Becomes a `Commitment`; unapproved commitments block release |
| `ASSUMPTION` / `HUMAN_PROVIDED_INPUT` | Bid-specific input or a stated assumption | Attributed author, timestamp, optional approval | Becomes an `Assumption`; surfaced in review mode, and either resolved or explicitly carried into the submission |

The generator is instructed and structurally constrained to emit statements with a class and provenance. Untyped or unprovenanced output fails schema validation and is repaired or rejected. This is what makes "no unsupported claims" a deterministic check rather than a hope: validation walks the statements, not the prose.

**7.4 Named AI call types** (each with its own prompt, schema and version): tender fact extraction; requirement decomposition; requirement classification; conditional-condition extraction; ambiguity analysis; precedence-hint extraction; knowledge entity/fact/relation extraction; evidence-match proposal; retrieval reranking; assessment rationale; department assignment; task grouping; task copilot chat; draft statement generation; conversation summarization; cross-document consistency check; validation assistance. No omnibus prompt.

## 8. Permission Model

Three layers, evaluated in one place:

1. **Role-based** — the roles in Sec 2, mapped to action permissions.
2. **Object-level** — a document's `confidentiality_class`, an evidence item's confidentiality and applicability, a task's `retrieval_scope`, a knowledge entity's classification (HR records, pricing, privileged legal).
3. **Grant-based** — `AccessGrant(grantee_user, task, resource_type, resource_id, permission, granted_by, reason, granted_at, expires_at, revoked_at, revoked_by)`. Grants are always task-scoped and time-bounded, are revocable, and are themselves audited. They are the only mechanism by which a user sees something their role alone would not permit.

`PermissionEvaluator(actor, action, resource, context)` is the single implementation. UI reads, exports, and AI retrieval all call it, so "what the assistant can see" cannot drift from "what the user can see". Least privilege is the default: the request flow prefers *answer* over *evidence share* over *access grant*.

## 9. Context Isolation and Retrieval (rewritten — correction 6)

The previous revision restricted a task conversation to evidence already attached to the task. That is too narrow: a security engineer asking "which government VAPT references do we have?" must be able to search company knowledge. The corrected design is **fixed base context plus permission-aware query-time retrieval, bounded by an explicit task retrieval scope**.

**Conversation ownership.** `Task 1—* TaskConversation`, keyed `(task_id, user_id)`. Each contributor has their own conversation on a task; conversations never merge. The shared, authoritative output of a task is the approved `ResponseArtifact`, not anyone's chat.

**Base context** (assembled deterministically every turn): the task definition and instructions; the requirements the task covers, each with its winning tender clause text and `SourceLocator`; the current draft and its typed statements; evidence already attached to the task; resolved dependency answers and shared evidence from cross-department requests; active `AccessGrant`s for this task; and the conversation's own history (recent turns verbatim plus a stored rolling summary of older turns — full history always retained in the database).

**Query-time retrieval.** On each user turn the Retrieval Service runs a hybrid search:

```
candidate generation
  pgvector similarity over DocumentChunk embeddings
+ PostgreSQL full-text search over chunk text and block headings
+ structured query over KnowledgeFact / KnowledgeRelation
        |
hard filters (non-negotiable, applied in SQL, never post-hoc)
  org_id = actor.org_id                         <- tenant isolation
  PermissionEvaluator allows actor to read      <- role + object + grant
  within task.retrieval_scope                   <- domain/classification bounds
  knowledge status and validity window admissible for the current use
        |
reranking (LLM call type: retrieval_rerank) over the surviving candidates only
        |
top-N passed into the prompt, each carrying its SourceLocator for citation
```

**`retrieval_scope`** is set when the task is created, from the assigned department plus the categories of the requirements the task covers, and is re-evaluated on every message rather than cached. It bounds retrieval to authorized, task-relevant knowledge — broader than "attached evidence", far narrower than "the whole company database".

**Out-of-scope handling.** If the user's question needs information outside their permission or the task's scope, the Copilot does not silently return nothing and does not guess. The Context Builder detects the out-of-scope intent, tells the user which department owns the information, and offers the cross-department request flow (answer / evidence / access). Rejected requests are logged with a reason, and the assistant explains what can still be completed without the restricted information.

**Why isolation holds.** Conversations are separate rows, so there is no shared session to leak through. Every retrieval result passes tenant, permission and scope filters in the query itself, so another tenant's or another department's material is never a candidate in the first place. Cross-task information enters only through an explicit, audited resolution attached to *this* task. And the model holds no state between calls — every prompt is rebuilt from the database, so isolation is a property of what we send, not of the model's forgetting.

---
Continues in `02_data_model_and_phases.md` (entities and roadmap), then `03_phase_b_architecture.md`, `04_phase_b_wireframes.md`, `05_phase_c_data_model.md`, `06_phase_d_api_contract.md`.
