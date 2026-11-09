# BidReady — Phase A (Revised): Entity Model Overview, Roadmap, Decisions, Risks

Revision 2. The detailed schema lives in `05_phase_c_data_model.md`; this document is the
orientation map and the roadmap.

## 10. Entity Model Overview

Every entity carries `org_id` (tenant), `created_at`, `created_by`, `updated_at`, `updated_by`.
Arrows: `1—*` one-to-many, `*—*` many-to-many via a named join table.

### 10.1 Identity and tenancy
```
Organization 1—* Department
Organization 1—* User
User *—* Role            (UserRole)
User *—* Department      (UserDepartment, with is_primary)
Role *—* Permission      (RolePermission)
AccessGrant(grantee_user, task, resource_type, resource_id, permission,
            granted_by, reason, granted_at, expires_at, revoked_at, revoked_by)
```

### 10.2 Document ingestion and search (correction 2)
One pipeline serves tender-side and company-side documents; `Document.side` distinguishes them.
```
Document 1—* DocumentVersion 1—* DocumentBlock 1—* DocumentChunk 1—1 Embedding
DocumentVersion 1—1 DocumentClassification
DocumentVersion *—* DocumentVersion  (DocumentRelation: AMENDS | CLARIFIES | SUPERSEDES |
                                      REPLACES_CLAUSE | ATTACHMENT_OF)
SourceLocator  — embedded on DocumentBlock and DocumentChunk, and referenced by
                 Evidence, Requirement, KnowledgeFact, KnowledgeRelation and
                 ArtifactStatement so every citation resolves to an exact location
```
`DocumentBlock` preserves document structure: `block_type` (HEADING, PARAGRAPH, LIST_ITEM,
TABLE, TABLE_ROW, TABLE_CELL, FIGURE_CAPTION, FORM_FIELD), `heading_path` (the ancestor
heading chain), `table_name`, `row_index`, `col_index`, `page_from`, `page_to`, ordering.
`DocumentChunk` is the retrieval unit built from one or more blocks, retaining the same
provenance plus `char_start`/`char_end`, and carries the token count and embedding model id.

### 10.3 Company knowledge graph (correction 1)
```
KnowledgeEntity(entity_type, canonical_name, aliases[], classification, status, ...)
   entity_type: PERSON | PROJECT | CERTIFICATION | CLIENT | PRODUCT | METHODOLOGY |
                POLICY | LEGAL_ENTITY | OFFICE | RATE_CARD | PARTNER | ACCREDITATION_BODY

KnowledgeFact(entity_id, predicate, value_text, value_number, value_date, value_json, unit,
              valid_from, valid_to, status, confidence, supersedes_fact_id, ...)
   e.g. (Ahmed, years_experience, 9), (OSCP, issuer, "Offensive Security"),
        (Project Alpha, sector, "Government")

KnowledgeRelation(subject_entity_id, predicate, object_entity_id, attributes JSONB,
                  valid_from, valid_to, status, confidence, supersedes_relation_id, ...)
   predicate: HAS_CERTIFICATION | WORKED_ON | EMPLOYED_BY | DELIVERED_FOR | HOLDS_ROLE |
              ISSUED_BY | SUBCONTRACTOR_OF | REFERENCES | PART_OF | SUPERSEDED_BY
   e.g. Ahmed --HAS_CERTIFICATION--> OSCP   {certificate_number, issue_date, expiry_date}
        Ahmed --WORKED_ON--> Project Alpha  {role: "Lead Tester", from, to}

Evidence(source_document_version_id, source_locator, excerpt, evidence_type,
         source_owner_user_id, approval_status, confidentiality_class,
         effective_date, expiry_date, applicable_region, applicable_product,
         applicable_service, version, last_verified_at, approved_for_reuse,
         status, supersedes_evidence_id)

EvidenceLink(evidence_id, target_type, target_id, link_role, confidence, created_by)
   target_type: KNOWLEDGE_FACT | KNOWLEDGE_RELATION | KNOWLEDGE_ENTITY |
                REQUIREMENT_ASSESSMENT | ARTIFACT_STATEMENT
   link_role:   SUPPORTS | PARTIALLY_SUPPORTS | CONTRADICTS | CONTEXT
```
Temporal validity (`valid_from`/`valid_to`), currentness (`status`), provenance
(`EvidenceLink`), approval (`approval_status`) and supersession (`supersedes_*_id`) are
carried on facts, relations and evidence alike. Shared status vocabulary:
`CURRENT_VERIFIED | HISTORICAL_VERIFIED | PROPOSED | NEEDS_CONFIRMATION | CONFLICTING |
MISSING | EXPIRED | REJECTED | SUPERSEDED`.

`CONFLICTING` is set by the graph service, not by an LLM: two admissible facts with the same
`(entity, predicate)` and overlapping validity windows, or two relations with the same
`(subject, predicate, object)` and contradictory attributes, are flagged for a Knowledge
Administrator rather than silently ranked.

### 10.4 Opportunity, bid, tender intelligence
```
Opportunity 1—1 Bid
Bid 1—* Document (side = TENDER)
Bid 1—* Requirement 1—* RequirementVersion
Bid 1—* ClauseResolution        (per-clause precedence outcome, correction 9)
Bid 1—* ClarificationIssue      *—* Requirement
Bid 1—* Deliverable             *—* Requirement
Bid 1—1 ScopeConfidence         (level, per-category detail, blocking flags)
Bid 1—1 BidNoBidDecision        (GREEN/AMBER/RED, factors, decided_by)
```

### 10.5 Assessment (correction 4)
```
RequirementAssessment(requirement_id, requirement_version_id, evidence_status,
                      bid_readiness, rationale, assessment_state, computed_at,
                      computed_by_run_id, human_adjusted_by, human_adjustment_reason,
                      version, is_current)
RequirementAssessment 1—* AssessmentEvidence(evidence_id, role, weight, contribution_note)
   role: PRIMARY | SUPPORTING | PARTIAL | CONTRADICTING | REFERENCE
```
One requirement may draw on six evidence items; the readiness verdict belongs to the
assessment, not to any evidence pair. Assessments are versioned and recomputed when their
inputs change (`STALE` marks them for recomputation without losing the prior verdict).

### 10.6 Tasks and collaboration (correction 5)
```
Task 1—* TaskParticipant       (one accountable owner on Task; contributors, reviewers,
                                approvers and observers here)
Task *—* Requirement           (TaskRequirement, with coverage_note)
Task *—* Task                  (TaskDependency: BLOCKS | WAITS_FOR)
Task 1—* TaskConversation      (keyed (task_id, user_id) — per-user isolation)
TaskConversation 1—* ChatMessage
TaskConversation 1—* ConversationSummary   (rolling summaries; raw history retained)
Task 1—* CrossDepartmentRequest (as origin)
CrossDepartmentRequest 1—0..1 AccessGrant
Task 1—* AccessGrant
```

### 10.7 Statements, artifacts, approvals (correction 8)
```
ResponseArtifact 1—* ArtifactVersion       (the newest version in state DRAFT *is* the task's
                                            working draft — there is no separate TaskDraft)
ArtifactVersion 1—* ArtifactStatement(statement_class, text, order_index,
                                      source_locator, author_user_id, approval_state)
   statement_class: EVIDENCE_BACKED_FACT | TENDER_DERIVED | PROPOSED_COMMITMENT |
                    ASSUMPTION | HUMAN_PROVIDED_INPUT
ArtifactStatement *—* Evidence     (via EvidenceLink)
ArtifactStatement 1—0..1 Commitment
ArtifactStatement 1—0..1 Assumption
ResponseArtifact *—* Requirement   (ArtifactRequirement)
Review / Approval — generic, keyed (entity_type, entity_id), reused for
                    Task, ResponseArtifact, KnowledgeUpdateCandidate, Package, Exception
```
`ArtifactStatement` is the unit validation walks. It is what makes "no unsupported claims",
"no unapproved commitments" and "no expired evidence presented as current" mechanical checks.

### 10.8 Assembly, generation, release (corrections 11 and 12)
```
Bid 1—1 ProposalStructure 1—* ProposalSection   (self-referencing tree, ordered,
                                                 derived from the tender's requested structure)
ProposalSection *—* ResponseArtifact            (SectionArtifactMap, with order_index and
                                                 inclusion_mode — one artifact may serve
                                                 several sections)
ProposalStructure 1—* GeneratedDocument 1—* GeneratedDocumentVersion
Package 1—* PackageItem —> GeneratedDocumentVersion
Package 1—1 Manifest 1—* ManifestEntry(filename, version, size, checksum,
                                       confidentiality, approval_status, destination)
Package 1—* ValidationRun 1—* ValidationFinding(check_code, severity, waivability,
                                                entity_ref, message, detected_at)
ValidationFinding 1—0..1 ReleaseException(requested_by, justification, approved_by,
                                          approved_at, scope)   -- only when WAIVABLE
```

### 10.9 Actions, audit, outcome
```
Action(canonical register) *—* Requirement, *—* Task
AuditEvent(actor, action, entity_type, entity_id, before_state, after_state,
           reason, request_id, occurred_at)   -- append-only, indexed by bid and entity
Outcome(bid_id, result, scores, feedback) 1—* KnowledgeUpdateCandidate
KnowledgeUpdateCandidate -> Knowledge Administrator approval -> canonical
                            KnowledgeFact / KnowledgeRelation / Evidence
```

## 11. Roadmap and Technology Stack per Phase

Baseline: **Python 3.12 / FastAPI / Pydantic v2 / SQLAlchemy 2 / Alembic**; **PostgreSQL 16 +
pgvector**; **Redis + Celery**; **S3-compatible object storage** (MinIO in dev, provider-
abstracted for production); **Next.js + React + TypeScript**; **DeepSeek** behind `LLMProvider`;
**local multilingual embedding model** behind `EmbeddingProvider`. Everything runs under
**Docker Compose** for development.

| Phase | Deliverable | Stack introduced |
|---|---|---|
| **A** Understanding | This document set | none |
| **B** Architecture | Component + sequence diagrams, **UI wireframes and user flows** for the ten key screens, security/deployment architecture | Mermaid + in-repo wireframes, versioned with the code |
| **C** Data model | ERD, full schema, enums, state machines, migrations, seed | SQLAlchemy 2, Alembic, Pydantic v2, Postgres 16 + pgvector |
| **D** API contract | Route signatures, request/response models, generated OpenAPI; handlers stubbed | FastAPI, OpenAPI 3.1 |
| **E** Foundation | Auth, orgs, users, departments, RBAC, `AccessGrant` evaluation, object storage, audit, jobs, **Model Gateway with both provider interfaces** | bcrypt (SHA-256/base64 pre-hash) + PyJWT (OIDC-ready seams), Redis + Celery, boto3/minio, structlog |
| **F** Ingestion & knowledge | Upload, malware scan, sandboxed parse, blocks, chunks, embeddings, knowledge extraction, graph governance, hybrid search | python-magic, ClamAV, pdfplumber/PyMuPDF, python-docx, openpyxl, sentence-transformers (multilingual, local), pgvector, Postgres FTS |
| **G** RFP intelligence | Tender classification, per-clause precedence resolution, requirement extraction, conditions, ambiguity, deliverables, scope confidence | Celery pipelines, DeepSeek structured call types |
| **H** Assessment | Hybrid retrieval + rerank, `RequirementAssessment`/`AssessmentEvidence`, matrices, readiness, bid/no-bid | pgvector + FTS + structured graph query; DeepSeek rerank |
| **I** Task workflow | Ownership, work packages, task states, dependencies, dashboards; **frontend build begins against Phase B wireframes** | Next.js, TanStack Query, Tailwind + shadcn/ui |
| **J** Copilot | Per-user task conversations, Context Builder, query-time retrieval, summarization, typed statement drafting | FastAPI SSE, DeepSeek chat + draft call types |
| **K** Collaboration | Answer / evidence / access requests, `AccessGrant` lifecycle, inbox, dependency resolution | in-app notifications (SSE + Postgres); email deferred |
| **L** Artifacts | Typed statements, approval chains, immutable versioning, evidence binding | Postgres append-only versioning |
| **M** Assembly | Structure parsing, section tree, artifact mapping, DOCX/XLSX/PDF generation, technical/commercial separation | python-docx (template population), openpyxl, LibreOffice headless for PDF |
| **N** Validation | Rule engine, findings with severity + waivability, exception workflow, hard blockers | Pydantic validators; DeepSeek advisory only |
| **O** Release | Maker-checker, manifest + checksums, immutability, archive, retention | hashlib, object-storage versioning, Celery beat |
| **P** Outcome & learning | Win/loss, knowledge update candidates, portfolio analytics | Postgres aggregation, Next.js dashboards |

Testing from Phase C onward: `pytest` + `pytest-asyncio`, FastAPI `TestClient`/`httpx`,
`testcontainers` or a Compose-managed Postgres for integration tests, `Playwright` for
end-to-end once the UI exists, and dedicated suites for permissions, tenant isolation,
workflow transitions, retrieval quality and LLM schema conformance. CI runs lint, type-check
and tests on every change.

## 12. Decisions (previously open questions — now settled)

| Topic | Decision |
|---|---|
| Development environment | Local Docker Compose: Postgres+pgvector, Redis, MinIO, API, worker, frontend |
| Production deployment | Provider-abstracted, UAE-ready; no provider-specific SDK outside the storage adapter; region and residency are configuration |
| Authentication | Simple credential auth for development, structured so an enterprise OIDC/SSO provider drops in later: identity is an adapter, `User` carries `external_idp_subject`, and no business logic reads passwords |
| LLM provider | DeepSeek only via `LLMProvider`; credentials exclusively from `DEEPSEEK_API_KEY`; model id, temperature, timeouts and token limits are configuration, never literals in code |
| Embeddings | Separate `EmbeddingProvider`, local multilingual model, vectors in pgvector. No Qdrant, no graph database |
| Language | English is the MVP language. Architecture stays Arabic-compatible: multilingual embeddings, UTF-8 and RTL-safe storage, `language` on documents/chunks/statements, no English-only assumptions in parsing or generation |
| Scale | Small initial scale — single Postgres, single Redis, a few workers. No sharding or read replicas in v1 |
| Test fixtures | Synthetic and public cybersecurity RFP material for the golden dataset until real historical bid packs are available |

## 13. Risks

- **Requirement recall.** Missing a mandatory requirement is the worst failure mode (target ≥95% recall). Mitigation: never a single LLM pass — structural enumeration of numbered clauses from `DocumentBlock`s, then extraction, then a deterministic coverage cross-check that every clause block produced at least one requirement or an explicit "no requirement here" decision.
- **Unsupported claims.** Mitigated structurally by typed `ArtifactStatement`s: an `EVIDENCE_BACKED_FACT` without a live `EvidenceLink` is a validation finding by construction, not a judgement call.
- **Permission and tenant leakage through retrieval.** The Retrieval Service is the single high-blast-radius component. Mitigation: filters applied inside the SQL query rather than after ranking; one `PermissionEvaluator` shared by UI and AI; permission and tenant-isolation tests treated as first-class, including negative tests that assert a restricted chunk is not even a candidate.
- **Knowledge graph quality.** Entity resolution (is this "Ahmed H." the same person as "A. Hassan"?) is genuinely hard. Mitigation: extraction proposes entities with candidate matches and confidence; merges above a threshold are auto-linked, everything else queues for the Knowledge Administrator. Never silently merge two people.
- **Precedence resolution correctness.** Getting the winning clause wrong is silently catastrophic. Mitigation: the resolver records its method and confidence, low confidence raises a `ClarificationIssue`, and the resolution is visible and overridable in the UI rather than buried.
- **Amendment cascade.** Semantic impact analysis is hard. Mitigation: ship coarse first (amendment touches clause range → flag every assessment, task and artifact linked to those requirements), refine to clause-level later. Over-flagging is recoverable; under-flagging is not.
- **Document generation fidelity.** Reproducing client templates exactly is the messiest real-world part. Mitigation: template population over synthesis, per-template adapters where needed, and extra time budgeted in Phase M.
- **Scope discipline.** The specification is large. No portal submission, mobile app, CRM/ERP integration, tender scraping or BI suite before the core workflow is proven.

## 14. Acceptance Criteria for Phases B/C/D

- **B complete:** component and sequence diagrams cover ingestion, precedence, assessment, copilot retrieval, cross-department access, amendment impact and release; wireframes and user flows exist for all ten screens; security and deployment architecture documented.
- **C complete:** every entity in Sec 10 has a table definition with columns, types, keys, indexes and constraints; every enum is enumerated; every state machine has its legal transitions and guards; tenancy and audit strategy are specified; migrations run clean against a fresh database.
- **D complete:** every screen in the wireframes and every workflow transition in C is reachable through a named endpoint with typed request and response models; auth, tenancy, errors, pagination and long-running-job conventions are defined; OpenAPI generates without stubs missing.
- **Phase E does not begin until B, C and D are reviewed and approved.**
