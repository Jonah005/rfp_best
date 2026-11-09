# BidReady backend — complete governed workflow

FastAPI + SQLAlchemy 2 + PostgreSQL/pgvector. The implemented backend includes
the Phase E foundation; Phase F document ingestion with governed company
knowledge extraction and permission-filtered hybrid retrieval; Phase G tender
intelligence; Phase H assessment/readiness; Phases I/K task and request
workflows; the permission-bounded task copilot; typed response artifacts and
DeepSeek drafting; proposal assembly; deterministic validation; maker-checker
release; amendment impact; outcomes; and governed learning. Phases E–P are
implemented and locked.

## Quick start

```bash
cp .env.example .env          # set BIDREADY_SECRET_KEY and DEEPSEEK_API_KEY
docker compose up --build     # from the repository root; includes the UI
cd backend && pip install -e ".[dev]"
alembic upgrade head
python -m app.db.seed
uvicorn app.main:app --reload # docs at http://localhost:8000/api/v1/docs
```

Seeded sign-in: any address in `app/db/seed.py` with password `ChangeMe-Dev-2026`.

## Layout

```
app/
  core/        config, logging, errors, execution context, security, permissions
  db/          declarative base with tenant mixins, session + SET LOCAL, seed,
               auth_lookup.sql (the one pre-tenant query)
  ingestion/   upload safety, structure-preserving parsers, clause-aware chunking
  models/      identity, documents, knowledge, tender, assessment, tasks,
               artifacts, assembly, package, release and learning
  services/    identity, permissions, grants, audit, storage, ingestion,
               automatic knowledge extraction/governance and hybrid retrieval
  gateway/     LLMProvider -> DeepSeekProvider, EmbeddingProvider -> local,
               call-type registry, schema validation and repair
  workers/     Celery app with the tenant-aware task base and registered tasks
  api/         dependencies and v1 routers
alembic/       0001 foundation through 0008 assembly/release/learning
tests/         unit + integration (integration needs PostgreSQL; pgserver is
               used automatically when available)
```

## Core invariants

1. **Cross-tenant rows are unrepresentable.** Every tenant table carries
   `UNIQUE (id, org_id)`; every foreign key between tenant tables is composite
   and reuses the child's own `org_id`. Polymorphic evidence links receive an
   equivalent trigger guard. RLS is `ENABLE`d and `FORCE`d on every tenant
   table. The application connects as a non-superuser role without `BYPASSRLS`.
2. **Tenant context is transaction-local.** `SET LOCAL app.current_org` inside
   each transaction, never a session-level `SET` that could ride a pooled
   connection into the next request. A pool `reset` hook asserts this.
3. **Authorization is decided in one place.** `PermissionEvaluator` serves API
   reads and the retrieval path. Grant state is computed live
   from `expires_at`/`revoked_at`; `materialized_state` is a listing convenience
   the authorization path never reads.
4. **Nothing writes without an audit row.** `AuditEvent` is written in the same
   transaction as the change, and is append-only by trigger and by privilege.
5. **Tender scope and company evidence remain separate.** Company documents are
   uploaded through the knowledge routes and are organization-wide; tender
   documents are uploaded through the bid routes and must belong to a bid. The
   database enforces it (`fk_document_bid_org`, `ck_document_side_bid`), the
   eligible-clause query is tender-side only, and no tender-side model call is
   given company text. Company evidence proves what we can do; it never creates
   tender scope.
6. **Retrieval filters before ranking.** Tenant, permission, confidentiality,
   side, and task-grant constraints are part of candidate selection; restricted
   chunks are never passed to the ranker or model.

## Implemented API surface

The application currently exposes 125 OpenAPI paths and 143 operations. Fifteen
knowledge operations cover company document upload/indexing/browsing, entities,
facts, relations, evidence approval, review-queue resolution and hybrid search.
Twenty tender operations cover opportunities, bids and their guarded state
machine, tender-package upload, analysis, precedence listing and override,
requirements and their versions, applicability and condition evaluation,
clarifications, deliverables and scope confidence. Later implemented routes
cover assessment/readiness, task and request workflows, the task copilot,
response artifacts, assembly, validation, release, amendment impact and learning.
See `openapi.json` for the generated contract and
`docs/06_phase_d_api_contract.md` for the historical target API.

## Current boundary

Phases E–P are implemented and locked. Production deployment still requires
operator-owned secrets, infrastructure, TLS, backups, monitoring and security
review. See `docs/PROJECT_CONTEXT.md` and implementation reports 13–15.

## Commands

`make test` · `make lint` · `make fmt` · `make typecheck` · `make migrate` ·
`make seed` · `make run` · `make worker`
