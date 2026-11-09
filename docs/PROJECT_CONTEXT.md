# BidReady — Project Context and Current Handoff

This document is the durable working memory for contributors and coding agents.
It summarizes why the product exists, the decisions that are already settled,
what has been implemented, and where development continues. Detailed schemas and
contracts remain authoritative in documents 01-15 and in the current code.

## 1. Product purpose

BidReady is an AI-assisted Bid / RFP Operating System for preparing complete,
compliant, traceable response packages while keeping humans in control of facts,
commitments, approvals, and release.

It is deliberately more than an AI writing tool. The defensible product is the
combination of:

- governed organizational knowledge and reusable evidence;
- tender-package understanding and atomic requirement traceability;
- eligibility, compliance, readiness, and clarification analysis;
- department work packages and persistent task-specific copilots;
- least-privilege cross-department requests;
- human editing, review, approval, and immutable response artifacts;
- deterministic validation, packaging, audit, and release gates.

The long-term platform supports technical RFPs generally. The recommended first
commercial vertical is UAE cybersecurity services—especially VAPT, GRC,
security assessments, audits, and consulting—because these bids contain highly
reusable methodologies, certifications, CVs, references, and compliance evidence
without requiring the product to design a large bespoke system on day one.

## 2. Lessons from the original evaluation

The first evaluated prototype already extracted tender facts, company evidence,
source locations, compliance structures, and proposal artifacts. Evidence
traceability itself existed and was a strength.

The sample nevertheless exposed four systemic risks:

1. Historical company proposal scope contaminated interpretation of a new
   tender.
2. Relevant evidence existed but retrieval selected unrelated material, such as
   CV or training content for customer-reference requirements.
3. Historical or incomplete evidence was interpreted as current, missing, or
   fully compliant without the necessary nuance.
4. A contradictory and incomplete tender did not trigger a sufficiently strong
   clarification and scope-confidence gate before solution drafting.

Those findings produced the central authority rule:

> Tender documents define what the customer asks for. Company documents prove
> what the bidder knows, has, did, or may safely reuse. Company evidence never
> creates tender scope.

They also motivated explicit evidence currentness, hybrid retrieval, structured
knowledge, ambiguity analysis, multiple readiness views, and deterministic
generation gates.

## 3. Locked architecture decisions

- Multi-tenant PostgreSQL 16 with forced RLS is the system of record.
- pgvector plus PostgreSQL full-text and structural signals provide hybrid
  retrieval. There is no Qdrant or separate graph database for the MVP.
- Every tenant relationship is protected at database level. Composite
  `(id, org_id)` foreign keys are preferred; polymorphic links use an equivalent
  tenant guard.
- Tenant context uses transaction-local `SET LOCAL` semantics and must not remain
  on pooled connections.
- A user currently belongs to one organization. Normalized email is globally
  unique so pre-tenant login is unambiguous. A future multi-organization model
  requires global identity plus organization membership, not weaker lookup
  semantics.
- The pre-tenant login lookup is a narrow `SECURITY DEFINER` function with a
  fixed search path, no `PUBLIC` execution, and execution limited to the
  application role.
- The LLM provider and embedding provider are separate abstractions. DeepSeek is
  behind `LLMProvider`; the local multilingual embedding model is behind
  `EmbeddingProvider`.
- AI may extract, classify, retrieve, rerank, draft, and explain. Deterministic
  application logic owns permissions, state transitions, approvals, release
  decisions, and authoritative writes.
- Task conversations are isolated by bid, task, user, permissions, requirements,
  and current draft. The application persists this context; it does not rely on
  model memory.
- Share Evidence creates a narrow task-scoped read grant for specific evidence;
  Request Answer creates none; Grant Access creates the broader explicitly
  approved task-scoped grant.
- Requirement wording amendments supersede requirement versions while normally
  preserving the underlying requirement identity.
- Scope Confidence measures the clarity of the tender only. Company evidence
  readiness is a separate assessment.
- Editable task content uses a DRAFT artifact lifecycle; approved versions become
  immutable response artifacts.
- Untrusted text may influence model output, but it cannot grant permissions,
  invoke tools, widen retrieval scope, mutate workflow state, or write directly
  to the system of record.

## 4. Implemented state

### Phases A-D — locked design baseline

Documents 01-06 define the product workflow, component and sequence
architecture, UI wireframes, data model, enums, state machines, tenancy rules,
and API contract.

### Phase E — foundation implemented and locked

Implemented capabilities include identity, authentication, RBAC, centralized
permission evaluation, task-scoped access grants, forced RLS, transaction-local
tenant context, append-only audit, S3-compatible storage, tenant-aware Celery
workers, separate model-provider interfaces, migrations, and the foundation API.

### Phase F — ingestion and company knowledge implemented and locked

Implemented capabilities include:

- content-based upload validation, active-content rejection, and pluggable
  malware scanning;
- PDF, DOCX, XLSX, CSV, text, and Markdown parsing;
- `Document → DocumentVersion → DocumentBlock → DocumentChunk → Embedding`;
- automatic post-commit Celery dispatch for COMPANY uploads and bounded,
  structured DeepSeek `knowledge_extraction` calls over parsed chunks;
- provenance-preserving `SourceLocator` values;
- the entity/fact/relation/evidence knowledge model with temporal validity,
  approval, supersession, conflicts, and a review queue;
- deterministic persistence of model proposals as `PROPOSED` /
  `PENDING_APPROVAL` entities, facts, relations, evidence, and evidence links,
  with audit-backed idempotency and no direct model writes;
- entity-resolution safeguards that never silently merge uncertain identities;
- permission-filtered hybrid retrieval with citations and structural boosts;
- migration 0002 for authentication hardening and migration 0003 for Phase F;
- 15 knowledge API operations across 13 Phase F tenant tables. At the Phase F
  lock checkpoint, the generated OpenAPI contract contained 29 distinct paths;
  the current expanded contract contains 91.

The Phase F lock checkpoint installed the complete declared runtime and
development dependency set and ran the entire suite against a fresh real
PostgreSQL 18.1 database with native pgvector 0.8.1 and Celery installed. It
closed at 157 passing tests with warnings treated as errors, zero skips, clean
Ruff, clean mypy across 56 source files, dependency integrity clean, and
successful Celery registration checks. The suite includes an explicit contract
test that COMPANY uploads dispatch the tenant-aware parse/index/extract task.
After Phases G-K were added, a full current-state revalidation against a new
database on the same real stack passed **311 tests with zero skips**, Ruff,
mypy across 81 source files, dependency integrity, and registration of all six
application Celery tasks. The deployment target remains the locked PostgreSQL
16 baseline. See `DOCUMENTATION_AUDIT.md`.

### Phase G — RFP intelligence implemented and locked

Implemented capabilities include:

- opportunity creation, promotion to a bid, and a guarded bid state machine;
- tender-package intake reusing the Phase F pipeline with a mandatory bid and a
  schema-enforced tender/company boundary (`fk_document_bid_org` and
  `ck_document_side_bid`);
- per-clause precedence resolution recording winner, competitors, method,
  confidence and rationale, which declines to choose and raises a blocking
  clarification when nothing discriminates;
- human precedence override with actor, reason and retained history;
- tender fact extraction into the bid record;
- atomic requirement extraction from winning clauses only, with a deterministic
  structural coverage cross-check that turns model silence about a clause into a
  counted gap and a clarification;
- requirement versioning that preserves requirement identity across wording
  amendments;
- conditional requirements with deterministic evaluation that stays pending
  rather than guessing;
- the ambiguity engine with fingerprinted, non-duplicating clarification issues
  that respect human-owned statuses;
- the deliverable register;
- the tender-only Scope Confidence gate, structurally unable to read company
  evidence;
- six new tender call types, all tender-scoped;
- migration 0004, the `analyze_tender` worker task, and 20 new API operations.

Verification: 217 tests pass with warnings treated as errors and zero skips;
Ruff clean across `app`, `tests` and `alembic`; mypy clean across 65 source
files; the migration chain 0001-0004 applies and downgrades cleanly; 37 tables
with 34 under forced RLS; `openapi.json` regenerated at 49 paths and 55
operations. See `docs/09_phase_g_rfp_intelligence.md`.

### Phases H, I/K and J — implemented and locked

Phase H implements requirement-to-evidence assessment, the independent evidence
status and bid-readiness axes, review matrices, bid/no-bid records and readiness
drill-through. Phases I/K implement task generation and ownership, participants,
dependencies, private per-user task conversations and least-privilege
cross-department requests. Phase J implements the permission-bounded task
copilot, context previews, durable message/citation traces, conversation rollup,
and asynchronous ask/stream behavior. Their detailed, verified reports are
documents 10-12.

### Phases L–P — implemented and locked

Phase L implements continuous task response artifacts, typed statements,
registered structured DeepSeek drafting, evidence links, commitments,
assumptions, reviews, ordered role-based approvals and immutable approved
versions. Phases M–P implement proposal structures, approved-artifact mapping,
real DOCX generation, immutable manifests, deterministic validation,
non-waivable hard blockers, scoped exceptions, maker-checker release, amendment
impact, outcome capture and human-governed learning candidates. Details are in
documents 13 and 14.

### Phase Q — end-to-end frontend and operations implemented

The ten-screen Next.js/React/TypeScript/TanStack Query frontend is wired to the
implemented API. Docker Compose now covers the frontend, API migration/seed,
Celery worker and beat, PostgreSQL/pgvector, Redis and MinIO. Ignored local env
files provide explicit DeepSeek and browser API configuration. See document 15.

## 5. Remaining deployment work

The planned product implementation is complete in this repository. Production
deployment still requires operator-owned values and infrastructure: a real
DeepSeek key, strong secrets, production database/object-store credentials,
TLS/domain configuration, backups, monitoring and an external security review.
Those are deployment responsibilities rather than deferred application phases.

## 6. Current continuation point

Phases A–Q are implemented and locked. Future work should begin from measured
user feedback, deployment findings or a specifically approved new phase. Do not
weaken tenancy, provenance, approval, release or human-governance invariants to
add convenience behavior.

## 7. Documentation precedence

When documents differ:

1. The user's current request controls the work.
2. Current tested implementation and migrations describe what the system does.
3. The latest implementation report describes the intended completed behavior.
4. Documents 01-06 remain the locked target design for later phases.
5. This context document summarizes the above but does not replace detailed
   schemas, contracts, or tests.
