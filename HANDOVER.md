# BidReady — handover

Project: `D:\rfpproject` (Windows). Backend FastAPI + PostgreSQL 18 with RLS FORCE.
Test bid: Jadwa RFP, `0e20677e-9cc3-434e-ad88-c953d6676028`.

Paste this into a new session as the opening message.

---

## Constraints that do not change

- **No hardcoded credentials.** The DeepSeek key comes from `DEEPSEEK_API_KEY` only.
- **All model calls go through the model gateway**, as named call types with a
  prompt version. `detected_by` / `extracted_by` / `equivalence_by` carry
  `llm:<name>@<version>`.
- **The model never decides**: permissions, task states, approvals, evidence
  access, workflow transitions, versioning, release, audit logs, file isolation,
  mandatory blockers, identity, tenancy. Those are code. The model judges
  meaning; code records the verdict with a version stamp.
- **Uploaded documents are untrusted.** Document text must never be able to
  override platform instructions.
- Not a generic RAG chatbot. The two axes — `evidence_status` and
  `bid_readiness` — stay orthogonal and are never collapsed.

## Working rules that earned their place

1. **Measure before changing.** Every wrong diagnosis in this project has been
   corrected by pasted output, never by re-reading the code. Write a read-only
   diagnostic, run it, then change code.
2. **The asymmetry rule.** A duplicate row is visible and a person can merge it.
   A merged or dropped obligation is unrecoverable and nothing downstream shows
   it is gone. When in doubt, keep both.
3. **Fixtures come from the database, not from imagination.** Two tests once
   passed against invented text while the bug they covered was untouched.
4. **Beware the optimisation that becomes a silent skip.** Five separate bugs
   have had this shape — a numbered-clause exemption, a decomposition fallback,
   a DELETE with no tenant context, a retirement rule, a 25-character minimum
   that deleted "Working days". Any cache, exemption, threshold or default
   argument deserves the question "what does this hide when it is wrong?"
5. **Diagnostics must not reimplement their subject**, and must be updated in
   the same breath as the thing they measure. Two checkers reported pre-fix
   numbers after the fix landed because they did not know what a restatement
   was.
6. **Absence is only evidence when the reader was complete.** A model capped at
   N results per batch that returns N has not finished; its silence means
   nothing. This cost 44 real customer questions before it was caught.

## Current state (verified from exports, 14 Sep, end of day)

| | |
|---|---:|
| Requirements | 89 (extractor v9) |
| BLOCKED | 59 |
| NEEDS_CLIENT_CLARIFICATION | 21 |
| NEEDS_COMPANY_CONFIRMATION | 9 |
| DRAFTABLE / READY | 0 |
| Distinct open customer questions | 38 |
| Duplicate questions | 0 open |
| Open COVERAGE_GAP findings | 0 |
| Stale requirement references | 0 |
| Clarification links | 417, all resolving |
| Unit tests | 737 passing |

Latest migration: `0020_clarification_equivalence`.
Decomposition prompt: `requirement_decomposition@v9`.

## Read this before planning

**The defect-fixing phase is finished.** Extraction, clarification linking,
deduplication, readiness precedence and referential integrity are all measured
and sound. What remains on the review lists is mostly NOT defects:

| | |
|---|---|
| Company evidence all typed OTHER | never built -- no document classifier exists |
| No requests, no drafted statements, 32 unassigned tasks | never built |
| Deliverables have no owner/deadline/format/filename | never built |

Re-auditing will keep listing these. They are a backlog, not a regression, and
the next export will say the same thing. **Zero DRAFTABLE rows is caused by the
missing evidence classifier, not by clarifications** -- 122 of 125 attachments
are judged "related, not proof", so no row can reach draftable however clean the
questions are.

Two things that ARE wrong and are export presentation only: closed coverage
findings keep their original "must be reviewed manually" text (the resolution is
in the audit trail), and `Same question as` renders canonical question TEXT
though the database stores `restates_clarification_id` correctly.

## What is finished

The clarification pipeline. It went from invisible (zero links, no row ever
reported a question) through over-linking (1,317 links, every row flagged) to
correct:

- Links are **exact** — every finding reaches precisely the requirements of the
  blocks it cites; measured, not assumed.
- Fragmented requirements were coalesced (129 → 104), which removed the fan-out
  at its source: a block held nine rows where it holds three.
- Duplicate questions are judged by `question_equivalence@v1` — "would one reply
  from the customer answer both?" — and marked, not deleted.
- An open question **demotes** a row rather than overruling it. It yields to
  BLOCKED and NEEDS_REFRESH, the states meaning we still owe proof.
- Rationales say a question "has been identified and not yet sent", because no
  request record exists.

## Lesson that cost two rounds: fix the pattern, not the instance

Withdrawing a requirement without moving its clarification links produced 25
dangling requirement codes in an export. It was fixed in
`coalesce_enumerated_fragments.py`, and `retire_superseded_extractions.py` --
which does the same thing -- was not looked at, so the identical failure
reappeared a day later with 130 occurrences. The move helper now lives in the
retire script and is imported by the repair script rather than written twice.

**Any script that withdraws a requirement must move its clarification links and
record `superseded_by_requirement_id`.** The same applies to deliverables, which
have their own join table.

## Done: obligations the extractor dropped

Five obligations are in the tender and absent from the matrix:

```
Back Office Checker                    role 6 of 8
Working days                           a timing parameter
"Get applicant related more information"   Al-Elm purpose 2 of 2, missing from R-018
"Develop engine to prompty send SMS"   missing from R-115
"Qoute Separate"                       the separate-pricing instruction
```

`scripts/check_uncovered_obligations.py` finds them two ways: a span check with
a coverage threshold, and a **word diff** with no threshold at all — the words a
block uses that no requirement from it repeats. Trust the word diff; the span
check has been mis-calibrated three times.

**The pattern**: every one of these sits in a clause shaped *lead-in + bullets*,
where the model produced ONE requirement carrying the first bullet and dropped
the rest. It is the exact mirror of the v6 fragmentation bug (one row per
bullet, lead-in copied onto each). The same clause shape breaks both ways.

All five are now extracted, and by the extractor rather than by hand:
Back Office Checker is in R-010, Working days in R-011, the second Al-Elm
purpose in R-018, the SMS engine is R-157.

`scripts/preview_decomposition.py` is the harness that made this safe -- it runs
the registered prompt against stored blocks and prints KEEPS / NEW / ABSORBED /
LOST per block, writing nothing. **Use it before every prompt change.** It
caught v7 emitting subjectless rows and v8 attributing bidder duties to the
customer, neither of which would have been visible until an export.

Three prompt versions in one sitting (v7, v8, v9) is the honest record. v7 and
v8 each fixed one half of the lead-in/bullets problem and broke the other, and
the oscillation is evidence of a structural limit: **one prompt cannot reliably
hold both "group these items" and "split that one, with its own actor".** The
architecture that would work is the one already used for `obligation_owner` --
let decomposition split generously, then a second small call decides grouping
and actor per item with its own version stamp. That is a design change and it is
the right next move on extraction quality, not a tenth prompt revision.

Known and accepted: the SMS block yields terse rows (R-155 "To send validation
SMS"). "Qoute Separate" is still dropped -- and it is NOT a parser problem, the
words are in the block text and the model discards them, because the obligation
test does not recognise a pricing instruction as an obligation.

## Backlog, in the order that actually blocks a bid response

1. **Deliverables consolidation** -- 32 rows are about 13 real submissions (six
   financial-proposal variants, four terms-and-conditions, three CV). Plus
   owner, deadline, format and filename, all blank. Half a day. This is the
   sheet someone assembles the submission pack from.
2. **Evidence typing / document classifier** -- 610 records all typed OTHER and
   all claiming to prove capability. **Until this exists nothing reaches
   DRAFTABLE.** One to two days, and the highest-value item on this list.
3. **Requests and tasks** -- the workflow that turns 59 BLOCKED rows into
   somebody's job. Nothing has ever been sent to a department or a customer.

## Also open, not started

- **Integration namespace** — ~20 integration clauses filed under
  `Business Requirements / N`, colliding with genuine business clauses on the
  same numbers. 16 requirements carry no clause ref at all. Probably the same
  parser/heading cause.
- **Deliverables** — 30 rows for about 12 real deliverables (five
  financial-proposal variants, three CV variants). Needs the coalescing
  treatment requirements got, plus owner/deadline/format/filename, all blank.
- **No requests, no drafted statements, 33 unassigned tasks.** Nothing has been
  sent to a department or a customer.
- **Company corpus** — 28 document records for 14 files, all classifications
  blank, all 610 evidence records typed `OTHER`, entities/facts/relations all
  still PROPOSED. No document classifier exists.
- **Evidence precision** — 144 attachments, 3 judged supporting. R-004's
  supporting attachment contradicts its own rationale.
- **Export presentation** — closed coverage findings keep their original "must
  be reviewed manually" text with no resolution note; the reason is in the audit
  trail only. `Same question as` renders canonical question TEXT rather than a
  stable id (the database stores `restates_clarification_id` correctly).

## Commands

```
cd D:\rfpproject\backend
python -m pytest tests\unit -q

python scripts\check_uncovered_obligations.py     # dropped obligations
python scripts\check_clarification_links.py       # link / question health
python scripts\check_crowded_blocks.py --min 8    # block vs its requirements
python scripts\dedupe_questions.py                # dry run, no model calls
python scripts\close_false_coverage_gaps.py
python scripts\coalesce_enumerated_fragments.py
python scripts\repair_withdrawn_links.py
python scripts\export_matrix.py --bid 0e20677e-9cc3-434e-ad88-c953d6676028
```

Every mutating script is dry-run by default and needs `--apply` (or named
codes). `run_local.py` runs `alembic upgrade head` itself with owner
credentials. **Restart it after any change under `app/`** — a stale process
silently produced two identical exports during one session. `backend\.env` holds
a stale DB password, so hand-run alembic fails; use `run_local.py`.

Import a new migration file directly before trusting it (`python -c "import
importlib.util…"`): nothing in the unit suite executes migrations, and
`sa.dialects.postgresql` does not resolve from `import sqlalchemy as sa`.

## Repo

Git initialised, remote `Jonah005/rfp_system`, **not yet pushed**. `.gitignore`
excludes the company document pack, `.tools/`, venvs, caches and logs. Secret
scan clean; both `.env.example` files are tracked deliberately with
`DEEPSEEK_API_KEY=` empty.
