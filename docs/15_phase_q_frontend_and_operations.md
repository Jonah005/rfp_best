# BidReady — Phase Q: Ten-Screen Frontend and Operations

The end-to-end operator surface is implemented.

## Frontend

The Next.js 15, React 19, TypeScript and TanStack Query application implements all ten locked
wireframe destinations. Seven of them own dedicated components because a generic table cannot
express what they do; three (company knowledge, bid dashboard, my work) are genuinely list
views and use the shared data panel.

1. **Opportunity and tender intake** — create an opportunity and promote it.
2. **Company knowledge** — the governed entity list.
3. **Bid dashboard** — active bids and their workflow state.
4. **Requirement matrices** — four separate matrices (eligibility, technical, submission,
   contract register), each row carrying its clause reference, its evidence count and both
   axes as distinct columns.
5. **My work** — owned tasks across bids.
6. **Task workspace and Copilot** — the task's bounded context (retrieval ceiling, covered
   requirements with their current assessment, open requests) beside a real chat: explicit
   conversation creation, `POST` → 202 → SSE stream, per-message citation counts and a link
   to each answer's retrieval trace.
7. **Incoming requests** — inbox plus the three resolutions in least-privilege order, each
   labelled with what it grants, and rejection with a mandatory reason. A resolved request
   shows exactly which grants it created.
8. **Approvals** — ordered chains; only the first pending step is actionable, and the rest
   say what they are waiting on.
9. **Bid readiness** — eight headline figures, six of which open into the requirements behind
   them, plus the two axes rendered as separate breakdowns that are themselves drill-through
   controls.
10. **Release and package** — derive, assemble, package, validate; findings split by
    waivability, with an exception control on waivable findings and **no control at all** on
    non-waivable ones; submit and approve as separate actions.

Two design rules carry from the backend into the UI. Readiness numbers open — the whole point
of the drill-through endpoint is defeated by a dashboard that only displays totals. And
`evidence_status` and `bid_readiness` are never merged into one indicator, because a single
bar would recreate in pixels the collapse the data model refuses to make.

Permission enforcement remains in the backend; the UI is not treated as a security boundary.
It shows a disabled control where an action is merely out of sequence, and omits the control
entirely where the route does not exist — offering a greyed-out "request exception" on a
non-waivable finding would imply a path that is deliberately absent.

## Configuration and startup

- `backend/.env.example` documents every setting; the ignored `backend/.env` is the local place
  for `DEEPSEEK_API_KEY`.
- `frontend/.env.local.example` documents the public API base URL.
- Docker Compose starts PostgreSQL/pgvector, Redis, MinIO, migrations, seed, API, Celery worker,
  Celery beat and the frontend.
- The frontend image runs type-check, lint and production build before producing its standalone
  runtime image.

## Verification

Frontend dependency installation, strict TypeScript (`pnpm typecheck`), ESLint (`pnpm lint`) and
the **full `pnpm build`** all pass on Linux with pnpm 10.15 and Node 22: the standalone production
build compiles, lints, collects page data and emits all three routes.

The earlier Windows sandbox could not run the final stage of `next build`, because that stage
spawns child processes the sandbox blocked; that environment therefore verified the production
compiler in compile mode only. That gap is now closed — the ordinary full build has been run and
passes, so it is no longer a Docker/CI-only gate.

Note that `next.config.ts` sets `typescript.ignoreBuildErrors: true` deliberately: type checking
is the separate `pnpm typecheck` gate rather than a duplicated child process inside the container
build. Both gates must pass; neither is optional.

The UI has **no automated tests**. TypeScript, ESLint and a successful production build say the
code compiles and the API paths type-check against their response shapes; they say nothing
about whether a screen behaves. That is the largest remaining gap in the repository, and it is
recorded here rather than implied away.

A static check confirms the UI now calls 30 distinct API paths, including every readiness
drill-through, all four matrices, the copilot conversation and stream, the request resolutions,
approval decisions and the release gates.
