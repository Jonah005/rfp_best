# BidReady — Phase B: Architecture and Sequence Design

Diagrams are Mermaid, kept in-repo and versioned with the code.

## 1. Component Architecture

```mermaid
flowchart TB
  subgraph FE["Frontend — Next.js / TypeScript"]
    UI1[Company Knowledge]
    UI2[Bid Dashboard & Matrices]
    UI3[My Work / Task Workspace + Copilot]
    UI4[Requests & Approvals]
    UI5[Readiness / Release]
  end

  subgraph API["API Layer — FastAPI"]
    MW["AuthN -> Tenant Scope -> RBAC -> Request ID"]
    R1[Knowledge routers]
    R2[Bid & Tender routers]
    R3[Task & Chat routers]
    R4[Request & Approval routers]
    R5[Assembly & Release routers]
  end

  subgraph DOM["Domain Services — deterministic"]
    PE[PermissionEvaluator]
    AG[AccessGrantService]
    ING[IngestionOrchestrator]
    PR[PrecedenceResolver]
    RR[RequirementRegistry]
    KG[KnowledgeGraphService]
    EG[EvidenceGovernance]
    RET[RetrievalService]
    AE[AssessmentEngine]
    TW[TaskWorkflow]
    CDR[CrossDeptRequestService]
    AP[ApprovalEngine]
    RAS[ResponseArtifactStore]
    AI2[AmendmentImpact]
    ASM[AssemblyAndGeneration]
    VAL[ValidationEngine]
    REL[PackageReleaseManager]
    AUD[AuditLogger]
  end

  CB[AI Context Builder]

  subgraph MG["Model Gateway"]
    LLM["LLMProvider -> DeepSeekProvider"]
    EMB["EmbeddingProvider -> LocalEmbeddingProvider"]
    REG["Call-type registry · prompt versions · schema validate+repair · retries · usage log"]
  end

  subgraph WRK["Celery Workers + Redis — write through tenant-aware domain services"]
    W1[scan / parse / block / chunk / embed]
    W2[extract knowledge / requirements]
    W3[assess / amendment impact]
    W4[assemble / validate / generate / export]
  end

  subgraph DATA["Persistence"]
    PG[("PostgreSQL 16 + pgvector")]
    OS[("Object Storage — S3 compatible")]
  end

  FE --> MW --> R1 & R2 & R3 & R4 & R5
  R1 --> KG & EG & ING
  R2 --> RR & PR & AE & ING
  R3 --> TW & CB
  R4 --> CDR & AG & AP
  R5 --> ASM & VAL & REL

  CB --> RET
  RET --> PE
  KG & EG & AE & ASM --> PE
  RET --> EMB
  CB --> LLM
  AE & RR & PR & AI2 & VAL --> LLM
  W1 --> EMB
  W2 & W3 & W4 --> LLM

  DOM --> AUD
  DOM --> PG
  ING --> OS
  ASM & REL --> OS
  WRK --> PG
  WRK --> OS
  MG --> PG

  classDef det fill:#eef4ff,stroke:#4a6fa5
  classDef ai fill:#fff4e6,stroke:#c47f2a
  class DOM,CB det
  class MG ai
```

**Reading the diagram.** Everything blue is deterministic and authoritative. The Model Gateway
(amber) is reached only from named call sites. Note that `RetrievalService` sits *between* the
Context Builder and the data, and that it consults `PermissionEvaluator` — the same one the UI
routers use. There is no path from a chat message to the database that bypasses it.

## 2. Ingestion Pipeline

```mermaid
flowchart LR
  U[Upload] --> V{Type + size valid?}
  V -- no --> RJ[Reject, audit]
  V -- yes --> S[Store raw in object storage]
  S --> AV{Malware scan}
  AV -- infected --> Q[Quarantine · NON_WAIVABLE finding · audit · notify]
  AV -- clean --> SB[Sandboxed parse: no macro exec, OLE stripped, resource limits]
  SB --> B[DocumentBlock extraction<br/>headings · paragraphs · tables · rows · cells · forms]
  B --> C[DocumentChunk assembly<br/>carries SourceLocator]
  C --> E[EmbeddingProvider · local multilingual]
  E --> IDX[(pgvector + FTS index)]
  B --> CLS[Classification call type]
  CLS --> D{Document side}
  D -- TENDER --> TP[Precedence inputs · requirement extraction · deliverables · ambiguity]
  D -- COMPANY --> KX[Knowledge extraction:<br/>entities · facts · relations · evidence]
  KX --> GOV[Governance: validity · approval · conflict detection · entity resolution]
  GOV --> PGDB[(PostgreSQL)]
  TP --> PGDB
  IDX --> PGDB
```

**What we can and cannot promise about prompt injection.** Uploaded content is passed to the
model inside delimited data blocks, with a system instruction that content within them is data.
That reduces but does not eliminate influence: text inside a document *may* affect what the
model writes, and no delimiter or instruction makes a language model immune to that. The
guarantee we actually make is architectural, and it is about consequences rather than influence.
Injected text cannot widen any permission, because permissions are evaluated in SQL by the
`PermissionEvaluator` against the acting user, not decided by the model. It cannot reach a tool
or perform an action, because the model is given no tools and no side effects. It cannot change
workflow state, because every transition runs through a guarded domain service invoked by
application code. It cannot write to the system of record, because model output is a
schema-validated proposal that a deterministic service accepts or rejects through the normal
write path. And it cannot widen its own context, because the Context Builder — not the model —
decides what is retrieved. The residual risk is therefore *content* risk: a malicious document
could try to skew an extraction or a draft. That is mitigated by schema validation, by human
approval gates before anything is published, by provenance on every statement, and by adversarial
injection fixtures in the security test suite — not by claiming the channel does not exist.

## 3. Knowledge and Retrieval Architecture

```mermaid
flowchart TB
  subgraph SRC["Sources"]
    DC[DocumentChunk + Embedding]
    KF[KnowledgeFact]
    KR[KnowledgeRelation]
  end
  Q["User turn or assessment query"] --> QP[Query planning:<br/>intent · entity hints · predicate hints]
  QP --> C1[Vector search over chunks]
  QP --> C2[Full-text search over chunk text + heading path]
  QP --> C3[Structured query over facts/relations]
  DC --> C1
  DC --> C2
  KF --> C3
  KR --> C3
  C1 & C2 & C3 --> F["HARD FILTERS in SQL:<br/>org_id · PermissionEvaluator · task.retrieval_scope ·<br/>status admissible · validity window"]
  F --> FUSE[Reciprocal-rank fusion + metadata boosts<br/>document_type · heading · table title · recency · approval]
  FUSE --> RRK[LLM rerank call type<br/>over survivors only]
  RRK --> TOPN[Top-N with SourceLocator for citation]
  F -.->|nothing admissible| OOS[Out-of-scope path -> cross-department request offer]
```

The metadata boost is what makes "customer references" retrieve the *Referenceable Clients*
table rather than an unrelated CV: block type, heading path and table name are first-class
ranking signals, not an afterthought, because the ingestion layer preserved them.

## 4. AI Architecture

```mermaid
flowchart LR
  subgraph CALLERS["Named call sites only"]
    A1[tender_fact_extraction]
    A2[requirement_decomposition]
    A3[requirement_classification]
    A4[condition_extraction]
    A5[ambiguity_analysis]
    A6[precedence_hint_extraction]
    A7[knowledge_extraction]
    A8[evidence_match_proposal]
    A9[retrieval_rerank]
    A10[assessment_rationale]
    A11[department_assignment]
    A12[task_grouping]
    A13[copilot_chat]
    A14[draft_statement_generation]
    A15[conversation_summarization]
    A16[consistency_check]
    A17[validation_assist]
  end
  CALLERS --> GW[Model Gateway]
  GW --> PV[Prompt template + version]
  PV --> SCH[Response schema Pydantic]
  SCH --> CALL[DeepSeekProvider · model/temp/timeout from config]
  CALL --> VD{Schema valid?}
  VD -- no --> RP[Repair prompt · bounded retries]
  RP --> VD
  VD -- still no --> FAIL[Fail the job · audit · never write partial output]
  VD -- yes --> OUT[Typed object returned to the caller]
  OUT --> LOGU[Usage log: tokens · latency · prompt version · model version · request id]
  GW --- EMBI["EmbeddingProvider — separate interface, separate config, separate failure path"]
```

`generate_structured` is the default; free-text generation exists only for `copilot_chat` and
`draft_statement_generation`, and the latter still returns a typed statement list.

## 5. Sequence — RFP Intake through Assessment

```mermaid
sequenceDiagram
  actor BM as Bid Manager
  participant API
  participant ING as Ingestion
  participant W as Worker
  participant PR as PrecedenceResolver
  participant RR as RequirementRegistry
  participant AE as AssessmentEngine
  participant RET as RetrievalService
  participant GW as Model Gateway

  BM->>API: POST /bids/{id}/documents (RFP, BOQ, appendices, amendment)
  API->>ING: register Document + DocumentVersion
  ING->>W: enqueue scan/parse/index per version
  W->>W: scan -> sandboxed parse -> blocks -> chunks
  W->>GW: embed chunks (EmbeddingProvider)
  W->>GW: classify document (LLM)
  W-->>API: versions INDEXED + CLASSIFIED
  API->>PR: resolve precedence for the bid
  PR->>GW: precedence_hint_extraction (customer-stated rules, affected clauses)
  PR->>PR: apply explicit supersession > customer rule > issue date > version label
  PR-->>API: ClauseResolution rows (+ ClarificationIssue where confidence low)
  API->>RR: extract requirements from WINNING clauses only
  RR->>GW: requirement_decomposition + classification + condition_extraction
  RR->>RR: structural coverage cross-check over clause blocks
  RR-->>API: Requirements + RequirementVersions + Deliverables + ScopeConfidence
  API->>AE: assess all ACTIVE requirements
  loop per requirement
    AE->>RET: hybrid retrieve (scope = requirement categories)
    RET->>GW: retrieval_rerank
    RET-->>AE: candidates + SourceLocators
    AE->>GW: evidence_match_proposal + assessment_rationale
    AE->>AE: deterministic verdict from evidence status, validity, approval
    AE-->>API: RequirementAssessment + AssessmentEvidence
  end
  API-->>BM: matrices, readiness, bid/no-bid inputs
```

Note the ordering: requirements are extracted from the clauses that *won* precedence, so an
amendment cannot be silently ignored, and a superseded clause cannot generate a phantom
requirement.

## 6. Sequence — Task Copilot with Query-Time Retrieval

```mermaid
sequenceDiagram
  actor SE as Security Engineer
  participant API
  participant TW as TaskWorkflow
  participant CB as ContextBuilder
  participant PE as PermissionEvaluator
  participant RET as RetrievalService
  participant GW as Model Gateway
  participant DB as PostgreSQL

  SE->>API: POST /tasks/SEC-03/conversations/{me}/messages<br/>"Which government VAPT references do we have?"
  API->>DB: persist user ChatMessage
  API->>CB: build context(task, user, message)
  CB->>DB: base context - task, requirements + winning clauses,<br/>current draft statements, attached evidence,<br/>resolved dependencies, active AccessGrants, history + summary
  CB->>RET: query-time retrieve(query, task.retrieval_scope, actor)
  RET->>PE: filter predicate for actor
  PE-->>RET: SQL predicate (org, role, object class, grants)
  RET->>DB: vector + FTS + graph query WITH filters inline
  DB-->>RET: admissible candidates only
  RET->>GW: retrieval_rerank
  RET-->>CB: top-N + SourceLocators
  CB->>GW: copilot_chat (base context + retrieved + history)
  GW-->>CB: answer with citations
  API->>DB: persist assistant ChatMessage + cited evidence ids
  API-->>SE: answer with clickable sources

  Note over SE,RET: If the question needs data outside scope/permission
  RET-->>CB: zero admissible candidates + owning department hint
  CB-->>API: out-of-scope response
  API-->>SE: "Resource scheduling belongs to HR."<br/>[Request Answer] [Request Evidence] [Request Access]
```

## 7. Sequence — Cross-Department Request and Access Grant

```mermaid
sequenceDiagram
  actor SE as Security (requester)
  actor HR as HR (target)
  participant API
  participant CDR as CrossDeptRequestService
  participant AG as AccessGrantService
  participant TW as TaskWorkflow
  participant AUD as Audit

  SE->>API: POST /requests {type, task, requirement_ids, reason, resource?}
  API->>CDR: create request (PENDING)
  CDR->>TW: origin task -> WAITING_FOR_DEPENDENCY (or WAITING_FOR_ACCESS)
  CDR->>AUD: request_created
  API-->>HR: appears in Incoming Requests

  alt Provide Answer (no access granted)
    HR->>API: POST /requests/{id}/answer {text, optional evidence refs}
    CDR->>TW: attach answer to origin task context; task -> IN_PROGRESS
  else Share Evidence (task-scoped visibility)
    HR->>API: POST /requests/{id}/share-evidence {evidence_ids}
    CDR->>AG: create AccessGrant(scope=task, resource=evidence, permission=READ, expires_at)
    CDR->>TW: attach evidence to task; task -> IN_PROGRESS
  else Grant Access (broader resource)
    HR->>API: POST /requests/{id}/grant-access {resource, permission, expires_at}
    AG->>AG: AccessGrant ACTIVE, task-scoped, time-bounded, revocable
    CDR->>TW: task -> IN_PROGRESS
  else Reject
    HR->>API: POST /requests/{id}/reject {reason}
    CDR->>TW: task stays BLOCKED with logged reason
    CDR-->>SE: notified; Copilot explains what can still be completed
  end
  CDR->>AUD: resolution recorded with actor, reason, before/after
```

Least privilege is enforced by ordering: the UI presents *Provide Answer* first, and *Grant
Access* last. Every grant expires; grants are revoked automatically when the task completes or
the bid closes, and revocation takes effect on the next retrieval because permission filters
are evaluated per query rather than cached.

## 8. Sequence — Amendment Impact

```mermaid
sequenceDiagram
  actor BM as Bid Manager
  participant API
  participant ING as Ingestion
  participant PR as PrecedenceResolver
  participant AI2 as AmendmentImpact
  participant AE as AssessmentEngine
  participant TW as TaskWorkflow
  participant RAS as ArtifactStore

  BM->>API: upload Amendment_2.pdf
  API->>ING: ingest + classify (type=AMENDMENT, affected clauses extracted)
  ING->>PR: re-resolve precedence for affected clause refs only
  PR-->>AI2: changed ClauseResolutions
  AI2->>AI2: diff old vs new winning clause text per requirement
  AI2->>AI2: create RequirementVersion for changed requirements;<br/>prior version SUPERSEDED, requirement identity preserved
  AI2->>AE: mark dependent RequirementAssessments STALE
  AI2->>TW: tasks covering changed requirements -> REVALIDATION_REQUIRED
  AI2->>RAS: artifacts citing changed clauses/requirements -> REVALIDATION_REQUIRED
  AI2-->>BM: impact report: requirements changed, tasks and artifacts to revalidate
  Note over AI2: v1 is deliberately coarse - flag everything linked to the touched<br/>clause range. Over-flagging is recoverable; under-flagging is not.
```

## 9. Sequence — Validation and Release

```mermaid
sequenceDiagram
  actor BM as Bid Manager
  actor EX as Executive Approver
  participant API
  participant ASM as Assembly
  participant VAL as ValidationEngine
  participant REL as ReleaseManager

  BM->>API: POST /bids/{id}/assemble
  ASM->>ASM: ProposalStructure -> sections -> SectionArtifactMap -> GeneratedDocumentVersion
  BM->>API: POST /packages/{id}/validate
  VAL->>VAL: deterministic checks over ArtifactStatements and package files
  VAL->>VAL: LLM-assisted checks (contradictions, claim support) as ADVISORY findings
  VAL-->>API: ValidationFinding[] {check_code, severity, waivability}

  alt any NON_WAIVABLE open
    API-->>BM: BLOCKED_HARD - no override path exists
  else only WAIVABLE open
    BM->>API: POST /findings/{id}/exception {justification}
    EX->>API: POST /exceptions/{id}/approve
    API-->>BM: BLOCKED_PENDING_EXCEPTION -> cleared
  end

  BM->>API: POST /packages/{id}/submit-for-release  (maker)
  EX->>API: POST /packages/{id}/approve-release     (checker, different user enforced)
  REL->>REL: generate submission-mode files, manifest with checksums, lock immutability
  REL-->>BM: RELEASED - further change requires a new package version
```

## 9a. Background Workers and Invariants

Workers are a scheduling mechanism, not a privilege boundary. Every Celery task resolves
`(org_id, actor)` from its payload, opens a transaction, sets the tenant context with
`SET LOCAL`, and performs all writes through the same repositories and domain services the API
uses. RLS policies, composite tenant foreign keys, permission checks, state-transition guards
and audit writes therefore apply identically to background work. Workers act as an explicit
`WORKER` actor (or as the initiating user where the job carries one), and a transition a worker
is not permitted to make fails in the worker exactly as it would in a request handler. No task
opens a raw session or issues plain SQL against tenant tables.

## 10. Security Architecture

| Layer | Control |
|---|---|
| Transport | TLS everywhere; HSTS at the edge |
| Identity | Credential auth in dev behind an identity adapter; OIDC/SSO + MFA drop-in later; sessions short-lived with refresh; no business logic touches credentials |
| Authorization | One `PermissionEvaluator`: role → object class → `AccessGrant`. Called by UI reads, exports and retrieval alike |
| Tenancy | `org_id` on every table; composite `(id, org_id)` foreign keys make cross-tenant rows unrepresentable; RLS enabled and FORCEd on every tenant table including join tables; tenant context set with `SET LOCAL` inside each transaction so nothing leaks through a pooled connection; every vector and FTS query carries the tenant predicate inside the query |
| Upload safety | Type/size validation, malware scan, sandboxed parse, macros and OLE stripped, no embedded code execution, document text handled strictly as data |
| Prompt-injection | Uploaded content delimited as untrusted data; structured outputs only. Injected text may influence model output; it cannot widen permissions, reach tools, mutate workflow state, or write to the system of record — those are all outside the model's reach by construction. Adversarial fixtures in the security suite |
| Data at rest | Encrypted volumes and buckets; object keys namespaced per tenant; separate bucket prefixes for raw, generated and released artifacts |
| Secrets | Environment only (`DEEPSEEK_API_KEY` and friends); never in code, never in the database, never logged |
| Confidential classes | CVs and personal data, pricing, security architecture, privileged legal — object-level classification enforced on read, retrieval and export |
| Audit | Append-only `AuditEvent` with before/after state and request id; auditor role has read access to everything and write access to nothing |
| Retention | Configurable per tenant; legal hold overrides deletion; Celery beat runs retention and grant-expiry sweeps |

## 11. Deployment Architecture

```mermaid
flowchart TB
  subgraph DEV["Development — Docker Compose"]
    A[api: FastAPI]
    WK[worker: Celery]
    BT[beat: Celery beat]
    PGx[(postgres:16 + pgvector)]
    RDx[(redis)]
    MN[(minio)]
    FEx[web: Next.js]
    EMBx[embedding model — multilingual-e5-base, 768d, in-process or sidecar]
  end
  FEx --> A --> PGx
  A --> RDx --> WK --> PGx
  WK --> MN
  A --> MN
  WK --> EMBx
  A --> EMBx
  BT --> RDx
```

Production keeps the same topology with managed equivalents. The only provider-specific code
is the object-storage adapter; region and residency are configuration. Nothing in the
application assumes a cloud vendor, so a UAE-hosted target — or a fully private deployment
with a local model behind `LLMProvider` — is a configuration and adapter change, not a rewrite.
