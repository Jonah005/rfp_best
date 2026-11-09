# BidReady — Phase E: Foundation (implemented)

> Historical phase report. Phase F has since been implemented. For the current
> repository state and continuation point, read
> `08_phase_f_ingestion_knowledge.md` and `PROJECT_CONTEXT.md`.

Phases A–D are locked. This document records what Phase E actually built, what it
guarantees, and the three design decisions taken during implementation that were
not settled by the design documents.

## 1. Scope delivered

| Area | Delivered |
|---|---|
| Configuration | Environment-only settings; secrets never defaulted to a usable value in staging or production; `DEEPSEEK_API_KEY` read from the bare vendor variable |
| Identity | Organizations, users, departments, roles, the permission catalogue, and the role→permission map for all fifteen roles |
| Authentication | Password hashing, JWT access and refresh tokens, an OIDC seam (`external_idp_subject`, identity adapter, reserved route) |
| Authorization | `PermissionEvaluator` — role layer, object-classification layer, grant layer — used by every read path |
| Access grants | Task-scoped, time-bounded, revocable; the three cross-department outcomes with their different consequences |
| Tenancy | Composite tenant-aware foreign keys, RLS enabled and forced on all eleven tenant tables, transaction-local context |
| Audit | Append-only `AuditEvent`, written in the same transaction as the change, enforced by trigger and by privilege |
| Storage | S3-compatible adapter with per-tenant key namespacing; the only provider-specific code in the application |
| Background jobs | Celery with a tenant-aware task base that refuses to run without an org |
| Model gateway | `LLMProvider`→`DeepSeekProvider` and `EmbeddingProvider`→local, separate interfaces; call-type registry; schema validation with bounded repair; usage accounting |
| API | Auth, admin, grants, audit and health routers; RFC 7807 errors with the `remediation` block |
| Migrations | One Alembic revision creating the schema, the RLS policies, the audit trigger, the effective-grant view and the login lookup |
| Tests | 72 tests: unit plus integration against a real PostgreSQL, including the full migration → seed → downgrade path |

## 2. Decisions taken during implementation

Three questions the design documents did not settle came up while building, and
each was resolved in favour of the stricter option.

### 2.1 Authentication happens before a tenant is known

RLS is forced on `app_user`, so the login query — which has only an email and no
tenant — correctly returns nothing. Weakening the policy for login would have
undone the guarantee it exists to provide.

The resolution is a single `SECURITY DEFINER` function, `auth_lookup_user(email)`,
with a fixed minimal projection (`user_id, org_id, password_hash, is_active`). It
is owned by a dedicated `NOLOGIN` role that holds `BYPASSRLS` and `SELECT` on one
table; `EXECUTE` is granted only to the application role. The application role
still cannot read `app_user` across tenants by any other route — a property the
test suite asserts directly. Login therefore has exactly two steps: resolve the
tenant through that function, then do everything else inside the user's own
tenant context.

### 2.2 Passwords are pre-hashed before bcrypt

bcrypt truncates its input at 72 bytes. Feeding it a raw password means anything
past byte 72 is ignored, so two different long passphrases can authenticate
against the same hash. Passwords are therefore SHA-256'd and base64'd first,
giving bcrypt a fixed 44-byte input in which every byte of the original is
significant. A regression test covers it.

`passlib` was dropped in favour of using `bcrypt` directly: passlib's bcrypt
backend is broken against bcrypt ≥ 4.1 and the project is no longer actively
maintained.

### 2.3 Filter columns are denormalized deliberately

`confidentiality_class` and `side` will be copied onto `document_chunk` in Phase F
so that tenant, permission and scope filters run inside the same statement that
touches the vector index. A join to reach those values would work today and get
refactored into post-ranking filtering later. This is a correctness decision, not
an optimization, and it is recorded here so it is not "tidied up" in review.

## 3. What the tests actually prove

The tenant-isolation suite runs against a real PostgreSQL as the constrained
application role, because RLS does not apply to superusers and a test that ran as
one would prove nothing.

- RLS hides other tenants' rows, and `WITH CHECK` rejects a forged `org_id` — reads and writes both.
- A query with no tenant context matches no rows: the system fails closed.
- Tenant context does not survive its transaction, and a pooled connection carries none into the next request.
- A composite foreign key rejects a cross-tenant join row **when written as the owner**, proving the constraint alone is sufficient without relying on RLS.
- `audit_event` rejects `UPDATE` and `DELETE`.
- A revoked grant stops working on the very next permission check — no cache, no job latency.
- An expired grant denies even though the materialization sweep has never run, and a deliberately stale `materialized_state` cannot resurrect it.
- A grant for one task does not open the resource inside another task, or outside any task.
- Malformed model output is repaired within bounds and otherwise fails loudly, with nothing written.
- An unregistered call type is refused: there is no inline-prompt path.
- The migration produces RLS on every tenant table, a correctly owned `SECURITY DEFINER` lookup, the audit trigger, and a clean downgrade.

Run them with `make test`. Integration tests use `pgserver` when available and
skip otherwise; set `BIDREADY_TEST_DATABASE_URL` to point at your own instance.

## 4. Deliberately not built in Phase E

Per the roadmap and the "do not overbuild" rule: no document ingestion, no
knowledge graph, no requirement extraction, no retrieval, no task workflow, no
copilot, no assembly, no validation, no release. The `task` table exists as a
minimal stub only because `access_grant` and `task_participant` need a real
parent; it carries the final table name so Phase I extends rather than replaces
it.

## 5. Next at the end of Phase E: Phase F

Ingestion and company knowledge — upload, malware scan, sandboxed parse,
`DocumentBlock`/`DocumentChunk`/`Embedding`, knowledge entity/fact/relation
extraction, evidence governance, and hybrid search. That phase introduces
pgvector to the schema, at which point the embedding dimension is fixed per
`docs/05` section 14.4.
