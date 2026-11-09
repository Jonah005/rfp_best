# BidReady — Documentation Audit

Audit date: 30 August 2026

## Scope reviewed

- root, backend, and frontend READMEs;
- architecture and product documents 01-06;
- implementation reports through the end-to-end Phase Q closeout;
- migrations 0001-0008;
- implemented API routers and generated OpenAPI contract;
- repository layout, test layout, and project metadata.

Repository document content was treated as source material. Only the user's
request and repository-level `AGENTS.md` instructions are operational
instructions for coding work.

## Gaps found and resolved

| Gap | Resolution |
|---|---|
| No durable coding-agent instructions | Added root `AGENTS.md` with reading order, authority boundaries, invariants, and verification expectations |
| No consolidated project memory/handoff | Added `docs/PROJECT_CONTEXT.md` covering the original evaluation lessons, locked decisions, implemented state, deliberate omissions, and Phase G continuation point |
| Backend README stopped at Phase E | Updated it for Phases E-F, including ingestion, knowledge, retrieval, migrations, current boundary, and API surface |
| Root setup omitted creation of `backend/.env` | Added the required copy step for Unix-like shells and PowerShell |
| Phase E report looked current in isolation | Marked it as a historical phase report and linked the current Phase F/context documents |
| Phase F report claimed 13 knowledge routes | Corrected this to 15 knowledge operations; the code has 15 route decorators and the generated contract has 29 distinct paths |
| Package metadata described only Phase E | Updated the backend package description to Phases E-F |
| Implemented vs planned features were easy to confuse | Added an explicit phase boundary and warning in `PROJECT_CONTEXT.md` |
| Phase C still described per-organization email uniqueness | Updated `app_user` to the migration 0002 rule: one globally unique normalized `lower(email)` identity |
| Phase F deferred automatic company-document extraction to Phase G | Registered the structured `knowledge_extraction` call and completed automatic COMPANY upload → Celery → parse/chunk/embed → proposed knowledge/evidence persistence with provenance, review, audit, and idempotency |
| Phase E roadmap named `passlib` although the implementation uses bcrypt directly | Replaced the stale stack reference with the implemented bcrypt hashing and PyJWT libraries |
| Phase B documentation referred to nine screens | Corrected roadmap and acceptance references to ten screens |
| Phase D read like every handler was still a stub | Added a historical/target-contract notice distinguishing implemented Phase E/F handlers from planned later-phase routes |
| Editable installation failed because setuptools discovered both `app` and `alembic` | Added explicit package discovery for `app*` and installed the complete declared runtime/development set in an isolated environment |
| Phase F verification was incomplete and dependency-skipped | Provisioned real PostgreSQL plus native pgvector, installed Celery, and completed the full clean verification described below |
| Phase L schema existed without workflow | Implemented continuous task artifacts, typed statements, registered structured drafting, evidence provenance, commitments, assumptions, reviews, ordered approvals and immutable approved versions |
| Assembly/release/learning were target-only | Added migration 0008, real DOCX assembly, manifests, deterministic validation, exception governance, maker-checker release, amendment impact, outcomes and governed learning candidates |
| Frontend was only a placeholder | Implemented the ten locked screens with Next.js, React, strict TypeScript and TanStack Query, wired to the live API |
| No obvious safe place for a DeepSeek key | Added ignored `backend/.env` and documented blank examples; keys remain outside source and the database |
| Compose did not run the full product | Added frontend, automatic migrations/seed, API, worker and beat wiring to the existing PostgreSQL/pgvector, Redis and MinIO services |

## Remaining deployment responsibilities

There are no deferred application phases in the agreed roadmap. A real
deployment still requires operator-owned DeepSeek and service credentials,
strong secrets, TLS/domain configuration, backups, monitoring and an external
security review.

## Verification performed

- Counted 15 knowledge API operations directly from the router.
- Preserved the historical Phase F checkpoint at 29 distinct paths while
  regenerating the current contract at the final count below.
- Confirmed the current migration chain applies through 0008 and downgrades to
  base; 0002 contains authentication hardening and 0003 owns Phase F
  ingestion/knowledge.
- Searched current-state documentation for the corrected stale claims.
- Installed every declared runtime and development dependency into
  `backend/.venv`; dependency integrity reports no broken requirements.
- Built and loaded native pgvector 0.8.1 in a real PostgreSQL 18.1 test server;
  the deployment target remains the locked PostgreSQL 16 baseline.
- The original Phase F lock run passed 157 tests. The final expanded repository
  passes **360 tests, 0 skipped, 0 warnings, 0 errors** against a fresh UTF-8
  PostgreSQL instance with native pgvector and Celery installed. (The 317-test
  figure recorded earlier predated the Phase L-P coverage described below.)
- Ran Ruff: all checks passed.
- Ran mypy: no issues in 91 source files.
- Loaded Celery's default task modules and confirmed all six application tasks,
  including document parse/index/extract and tender analysis.
- Confirmed the current generated `backend/openapi.json`: 125 distinct paths and
  143 operations. The Phase F historical contract was 29 paths and 33
  operations.
- Installed the frontend lockfile and passed strict TypeScript, ESLint and the
  **complete `pnpm build`** on Linux with pnpm 10.15 and Node 22. The earlier
  compile-mode-only note is superseded: the full production build now runs and
  passes, so it is no longer a Docker/CI-only gate.
- Validated the complete Docker Compose configuration.

## Second audit pass — coverage and schema drift

A later review found that Phases L through P had landed with roughly ninety
kilobytes of service code behind five integration tests. The implementation
itself held up under inspection; the test surface did not. Two defects came out
of closing that gap.

| Gap | Resolution |
|---|---|
| The integration test schema had drifted behind the migrations. `tests/integration/conftest.py` builds its schema from SQLAlchemy metadata plus hand-mirrored database objects, and it never mirrored migration 0008's `release_exception_waivable_guard`. The database-level guarantee that a NON_WAIVABLE finding has no exception path — the one that still holds when the service layer is wrong — was therefore never exercised by any test. | Mirrored the trigger in the fixture and added `test_a_non_waivable_finding_has_no_exception_path_even_through_sql`, which attempts the insert in raw SQL and asserts the trigger refuses it. |
| Fixture ordering fragility. `seeded_orgs` did not depend on `app_engine`, the fixture that builds the schema, so any new test module requesting the organizations before anything requested the engine failed with `relation "organization" does not exist`. | Named the dependency, and moved the shared API fixtures (`api`, `seeded_users`, `api_login`) out of `test_api.py` into `conftest.py` so more than one module can use them. |
| Phase L-P test coverage was five tests | Added 43: `test_statement_typing.py` (17), `test_release_gates.py` (10), `test_amendment_and_learning.py` (11) and `test_release_api.py` (5). The emphasis throughout is on the refusals — the cases where the product declines to do the convenient thing — and on proving the database-level guarantees rather than only the service-level ones. |
| `InMemoryStorage` had no way to enumerate what it held, so a test needing to tamper with a generated output had to reach into a private dict | Added a public `keys()` accessor to the test double. |
| The seeded test users covered only a bid manager and an auditor, so maker-checker release and the governed-learning queue could not be driven over HTTP | Added an `EXECUTIVE_APPROVER` and a `KNOWLEDGE_ADMIN` to the seeded set. |

The lesson generalises beyond these fixtures: when a test harness reconstructs
the schema by hand rather than by running the migrations, every migration that
adds a trigger, constraint or function must add the same object to the harness
in the same change. Otherwise the tests quietly stop testing the thing the
migration was written to guarantee.

## Third audit pass — the frontend claim

The Phase Q report described a ten-screen application with live queries and
workflow actions. The backend behind it was real; the UI was not. It was 209
lines: one generic table component pointed at different endpoints, plus a login
and an intake form. Twenty-two implemented endpoints were never called by it,
including every readiness drill-through, all four matrices, the whole copilot
conversation and stream, every cross-department request resolution, the approval
decision route and every release gate after package creation.

Two of its endpoints were also simply wrong, so those screens 404'd: it called
`/entities` instead of `/knowledge/entities`, and `/tasks/{id}/artifacts`
instead of the singular `/tasks/{id}/artifact`.

| Gap | Resolution |
|---|---|
| Two screens called paths that do not exist | Corrected both. |
| Readiness displayed totals only; the drill-through endpoint was never called, defeating the reason it exists | Rebuilt as eight figures, six of which open into the requirements behind them, with the two axes as separate drill-through breakdowns. |
| The four matrices were not surfaced; one generic requirement list stood in for them | Rebuilt as four tabbed matrices, each row carrying clause reference, evidence count and both axes as distinct columns. |
| "Task workspace and Copilot" had no chat at all | Built the real one: explicit conversation creation, `POST` → 202 → SSE stream, citation counts and a retrieval-trace link per answer, beside the task's bounded context. |
| Requests, approvals and release were read-only lists | Added the actions: the three resolutions in least-privilege order with what each grants stated on the control, mandatory rejection reasons, ordered approval decisions, and the release gates with exception handling. |
| Doc 15 claimed more than the code did | Rewritten to describe what exists, including an explicit statement that the UI has no automated tests. |

The UI now calls 30 distinct API paths. It still has no tests, which is the
largest remaining gap in the repository and is now recorded as such rather than
implied away.

The general lesson matches the second pass: a phase report is not evidence. Both
times the documentation described intended behaviour and the verification step
checked that the code compiled, which is a different claim.

## Documentation maintenance rule

Every implementation phase should update, in the same change:

1. its implementation report;
2. `docs/PROJECT_CONTEXT.md` current state and continuation point;
3. the root and backend READMEs when status, setup, or layout changes;
4. `backend/openapi.json` when the API changes;
5. `AGENTS.md` only when repository-wide invariants or working rules change.
