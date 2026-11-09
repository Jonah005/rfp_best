# BidReady

An AI-assisted Bid / RFP Operating System. Tender documents define requirements;
company documents provide evidence; the LLM assists inside a deterministic
workflow that owns permissions, state, approvals and release gates.

## Status

| Phase | Scope | State |
|---|---|---|
| A | Product understanding, architecture, workflow | Locked |
| B | Component and sequence diagrams, UI wireframes | Locked |
| C | ERD, schema, enums, state machines | Locked |
| D | API contract | Locked |
| E | Foundation: identity, tenancy, RBAC, grants, audit, storage, jobs, model gateway | Implemented, locked |
| **F** | **Document ingestion, automatic governed company knowledge extraction, hybrid retrieval** | **Implemented, locked** |
| **G** | **RFP intelligence: precedence, requirements, ambiguity, scope confidence** | **Implemented, locked** |
| H | Assessment, readiness matrices, bid/no-bid | Implemented, locked |
| I/K | Tasks, participants, dependencies, cross-department requests | Implemented, locked |
| J | Permission-bounded task copilot and traceable conversations | Implemented, locked |
| L | Typed statements, DeepSeek drafting, artifacts, reviews and approvals | Implemented, locked |
| M–P | Assembly, validation, release, amendments, outcomes and learning | Implemented, locked |
| Q | Ten-screen frontend and end-to-end operations | Implemented, locked |

## Documents

| File | Contents |
|---|---|
| `docs/01_understanding_and_architecture.md` | Product understanding, actors, workflow, subsystems, logical architecture, state machines, AI vs deterministic split, permissions, context isolation |
| `docs/02_data_model_and_phases.md` | Entity model overview, roadmap and stack per phase, settled decisions, risks |
| `docs/03_phase_b_architecture.md` | Component architecture, ingestion, retrieval, AI architecture, sequence diagrams, security, deployment |
| `docs/04_phase_b_wireframes.md` | Wireframes and user flows for the ten key screens |
| `docs/05_phase_c_data_model.md` | Full schema, enums, constraints, state machines, tenancy and indexing |
| `docs/06_phase_d_api_contract.md` | API surface, conventions, error model, async jobs |
| `docs/07_phase_e_foundation.md` | What Phase E built, how to run it, what it guarantees |
| `docs/08_phase_f_ingestion_knowledge.md` | What locked Phase F built: ingestion, automatic governed knowledge extraction, retrieval |
| `docs/09_phase_g_rfp_intelligence.md` | What Phase G built: precedence, requirements, ambiguity, scope confidence |
| `docs/10_phase_h_assessment_readiness.md` | What Phase H built: evidence assessment, readiness and bid/no-bid |
| `docs/11_phase_i_k_tasks_and_requests.md` | What Phases I/K built: task workflow and least-privilege requests |
| `docs/12_phase_j_task_copilot.md` | What Phase J built: bounded context, conversations, citations and streaming |
| `docs/13_phase_l_response_artifacts.md` | Typed drafting, response artifacts, reviews and approvals |
| `docs/14_phases_m_p_assembly_release_learning.md` | Assembly, validation, release, amendments, outcomes and learning |
| `docs/15_phase_q_frontend_and_operations.md` | Ten-screen frontend, environment and startup |
| `docs/PROJECT_CONTEXT.md` | Durable product history, locked decisions, implemented state, and continuation point |
| `docs/DOCUMENTATION_AUDIT.md` | Documentation gaps found, corrections made, and verification status |
| `AGENTS.md` | Repository-wide instructions and invariants for coding agents |

## Run it

Two ways. Docker Compose is the full stack; `run_local.py` is one Python process
for a walkthrough.

### Option A — one Python process (no Docker for the app, no Node at runtime)

```bash
pip install -e "backend[dev]"

# Linux/macOS: PostgreSQL is embedded, nothing else to install.
python run_local.py

# Windows: pgserver has no Windows build, so supply a PostgreSQL.
#   docker run -d --name bidready-pg -p 5432:5432 ^
#     -e POSTGRES_PASSWORD=bidready -e POSTGRES_DB=bidready pgvector/pgvector:pg16
python run_local.py --database-url postgresql://postgres:bidready@localhost:5432/bidready

# If you already have the compose stack up (Option B), its database is already
# there -- but compose sets POSTGRES_USER, so the superuser is `bidready` and
# `postgres` has no password. Point run_local.py at the compose credentials:
python run_local.py --database-url postgresql://bidready:bidready@localhost:5432/bidready
```

If `frontend/ui-dist.zip` is present it unpacks that prebuilt UI and **no Node
toolchain is needed at all**. Otherwise it builds the UI once with pnpm or npm.
Either way FastAPI serves the result, on the same origin as the API, so the
build is port-independent. `--no-ui` runs the API alone with its own docs. MinIO becomes a directory
under `backend/.local/objects`, Redis and Celery are replaced by running queued
work inline, and data persists between runs. `--reset` starts clean;
`--rebuild-ui` rebuilds the frontend.

Nothing about the security model is relaxed: both database roles are created,
row level security is enabled and forced, migrations run as the owner while the
application connects as the constrained role, and the same seed runs under the
same policies.

**pgvector is preferred, not required.** The launcher detects it and says which
mode it picked:

| Mode | Storage | Search | When |
|---|---|---|---|
| `pgvector` | `vector(768)` + HNSW index | Indexed cosine, fast at scale | pgvector available |
| `portable` | `real[]` | Cosine computed in Python, unindexed scan | stock PostgreSQL |

Both store vectors **inside PostgreSQL**, so row level security, the permission
predicate and atomic writes apply identically — the admissibility filter is still
applied in the query, so an inadmissible chunk is never fetched, let alone
ranked. Semantic search works in both: the portable mode returns the same
ordering pgvector would. What it lacks is the index, so it is a scan and suits
modest corpora. Point the same code at a database with pgvector and the indexed
path returns with no other change.

### Option B — the full stack with Docker Compose

```bash
# 1. Configuration. The file is git-ignored; it is where your key lives.
cp backend/.env.example backend/.env      # PowerShell: Copy-Item backend\.env.example backend\.env

# 2. Optional: paste a DeepSeek key into backend/.env as DEEPSEEK_API_KEY=...
#    Without one the app runs; only drafting and the Copilot will fail.

# 3. Bring the stack up.
docker compose up --build
```

Then open **http://localhost:3000** and sign in with a seeded development account:

| Account | Role | Can do |
|---|---|---|
| `sara.k@acme-cyber.example.com` | Bid manager | **The whole bid workflow.** Start here. |
| `ahmed.h@acme-cyber.example.com` | Security | Contributes to security tasks |
| `nadia.r@acme-cyber.example.com` | HR | Answers cross-department requests |
| `auditor@acme-cyber.example.com` | Auditor | Reads the whole trail, changes nothing |
| `admin@acme-cyber.example.com` | Org admin | Users, roles, departments — **not** bid work |

The org admin deliberately cannot create opportunities or approve releases.
Separation of duties is the point of the product: an administrator who silently
held every permission would make the audit trail meaningless. Sign in as the
**bid manager** to walk the workflow.

The seeded password for all of them is `ChangeMe-Dev-2026`. The login screen
prefills the admin account. **Change every credential before any shared
deployment.**

The API is on http://localhost:8000, with interactive docs at
http://localhost:8000/api/v1/docs and the MinIO console on
http://localhost:9001 (`minioadmin` / `minioadmin`).

### What the first boot does

`docker compose up` starts PostgreSQL 16 with pgvector, Redis, MinIO, the API,
a Celery worker, Celery beat and the frontend. On start the API runs
`alembic upgrade head`, then `python -m app.db.seed`, then serves.

Two details matter and are easy to get wrong:

- **Migrations connect as the schema owner, the application does not.** The app
  role is `NOSUPERUSER`, holds no `CREATE` on schema `public` and has no
  `BYPASSRLS` — that is precisely what makes `FORCE ROW LEVEL SECURITY` bind it.
  Compose therefore passes `BIDREADY_MIGRATION_DATABASE_URL` for the Alembic
  step only. Running migrations as the app role fails with
  `permission denied for schema public`.
- **The first build downloads PyTorch**, because the locked embedding model
  (`intfloat/multilingual-e5-base`, 768 dimensions) needs sentence-transformers.
  Expect a large, slow first build. For a quick walkthrough instead, set
  `BIDREADY_EMBEDDING_PROVIDER=null` in `backend/.env` and build with
  `docker compose build --build-arg INSTALL_EMBEDDINGS=0`; retrieval then uses a
  hashing embedder, which is fine for clicking through the workflow and useless
  for real retrieval quality.

### Running without Docker

```bash
cd backend
pip install -e ".[dev]"                 # add ,embeddings for the real embedder
# Point BIDREADY_DATABASE_URL at your PostgreSQL, then:
alembic upgrade head && python -m app.db.seed
uvicorn app.main:app --reload

cd ../frontend && pnpm install && pnpm dev
```

You still need PostgreSQL 16 with pgvector, Redis and an S3-compatible store;
`backend/scripts/init_db.sql` creates the two database roles the design
depends on.

### If it does not come up

| Symptom | Cause |
|---|---|
| `permission denied for schema public` | Migrations ran as the app role. `BIDREADY_MIGRATION_DATABASE_URL` is missing. |
| API waits forever on `minio` | The MinIO healthcheck never passed. Check `docker compose logs minio`. |
| `NoSuchBucket` on upload or assembly | The API creates the bucket at startup and logs `storage.bucket_unavailable` if it could not. Check the API logs. |
| Copilot or draft generation errors | No `DEEPSEEK_API_KEY` in `backend/.env`. Everything else still works. |

`backend/openapi.json` is the generated contract for the implemented surface.
The default development credentials and service settings live in
`backend/.env.example`; never commit real credentials.

## Stack

FastAPI · Pydantic v2 · SQLAlchemy 2 · Alembic · PostgreSQL 16 + pgvector ·
Redis + Celery · S3-compatible object storage (MinIO in development) ·
Next.js 15 + React 19 + TypeScript + TanStack Query · DeepSeek behind `LLMProvider` · a local multilingual model
behind `EmbeddingProvider`. No Qdrant, no graph database.
